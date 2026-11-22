"""Unit tests for app.core.logging — JSON fields, request id, redaction (T0.3).

The plan's verification gate calls out *log fields*: the stable-key contract
(ts/level/logger/msg/request_id/exception), extras merging with collision
renaming, contextvar correlation, secret redaction (§8.6), and the request
middleware's access record.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from collections.abc import Iterator
from datetime import datetime
from io import StringIO
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.clock import DEFAULT_FAKE_START, FakeClock, use_clock
from app.core.logging import (
    JsonFormatter,
    RequestContextMiddleware,
    bound_request_id,
    configure_logging,
    get_request_id,
    new_request_id,
    redact_secrets,
)

# --- helpers ----------------------------------------------------------------


def render(
    msg: str = "hello",
    *,
    name: str = "vanilla.test",
    level: int = logging.INFO,
    extra: dict[str, Any] | None = None,
    exc: Exception | None = None,
) -> dict[str, Any]:
    """Emit one record through JsonFormatter and parse the line back."""
    record = logging.LogRecord(name, level, "file.py", 1, msg, None, None)
    for key, value in (extra or {}).items():
        setattr(record, key, value)
    if exc is not None:
        try:
            raise exc
        except Exception:  # noqa: BLE001 - capture, never swallow
            record.exc_info = sys.exc_info()
    line = JsonFormatter().format(record)
    assert "\n" not in line  # one record, one line — always
    return json.loads(line)


# --- stable fields ---------------------------------------------------------------


def test_core_fields_are_present() -> None:
    payload = render("did a thing", name="vanilla.run", level=logging.WARNING)
    assert payload["level"] == "WARNING"
    assert payload["logger"] == "vanilla.run"
    assert payload["msg"] == "did a thing"
    assert datetime.fromisoformat(payload["ts"])  # parseable ISO-8601


def test_ts_comes_from_the_injectable_clock() -> None:
    with use_clock(FakeClock()):
        payload = render()
    assert payload["ts"] == DEFAULT_FAKE_START.isoformat(timespec="milliseconds")


def test_request_id_absent_outside_a_request_context() -> None:
    assert "request_id" not in render()


def test_request_id_bound_via_contextvar() -> None:
    with bound_request_id("cafe01"):
        payload = render()
    assert payload["request_id"] == "cafe01"
    assert get_request_id() is None  # restored on exit


# --- extras merging ---------------------------------------------------------------


def test_extras_become_top_level_fields() -> None:
    payload = render(extra={"run_id": "r1", "attempt": 3})
    assert payload["run_id"] == "r1"
    assert payload["attempt"] == 3


def test_extras_cannot_forge_core_keys() -> None:
    # Keys that stdlib makeRecord() rejects outright (msg/args/...) can never
    # reach the formatter; the ones it lets through ("level", "logger", "ts",
    # "request_id" are not LogRecord attributes) must still not overwrite the
    # envelope's stable keys — they arrive prefixed instead.
    payload = render(
        "real msg",
        extra={"level": 99, "logger": "x", "ts": "forged", "request_id": "spoofed"},
    )
    assert payload["level"] == "INFO"
    assert payload["logger"] == "vanilla.test"
    assert payload["ts"] != "forged"
    assert "request_id" not in payload  # no ambient context -> cannot be forged
    assert payload["x_level"] == 99
    assert payload["x_logger"] == "x"
    assert payload["x_ts"] == "forged"
    assert payload["x_request_id"] == "spoofed"


def test_stdlib_blocks_overwriting_record_msg_directly() -> None:
    handler = logging.StreamHandler(StringIO())
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("vanilla.test.reserve")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        with pytest.raises(KeyError, match="overwrite"):
            # msg is a LogRecord attribute; makeRecord refuses the extra.
            logger.info("x", extra={"msg": "sneaky"})  # noqa: G101
    finally:
        logger.removeHandler(handler)


def test_exception_extra_is_reserved_even_without_exc_info() -> None:
    payload = render(extra={"exception": "claim"})
    assert payload["x_exception"] == "claim"


def test_exc_info_lands_in_the_exception_field() -> None:
    payload = render("failed", exc=ValueError("bad value"))
    assert "ValueError: bad value" in payload["exception"]
    assert payload["exception"].startswith("Traceback")


def test_stack_info_lands_in_the_stack_field() -> None:
    payload = emit_capture("vanilla.test.stack", "with stack", stack_info=True)
    assert payload["msg"] == "with stack"
    assert "Stack" in payload["stack"]


def test_stack_extra_is_reserved_not_overwritten_by_the_real_stack() -> None:
    """Regression (code review): "stack" must be in _CORE_KEYS — an extra
    named "stack" is moved to x_stack instead of being silently clobbered
    by the formatter-emitted field (the documented collision contract)."""
    payload = emit_capture(
        "vanilla.test.stack2",
        "m",
        stack_info=True,
        extra={"stack": "user-value"},
    )
    assert payload["x_stack"] == "user-value"
    assert "Stack" in payload["stack"]


def emit_capture(
    name: str, msg: str, *, extra: dict | None = None, stack_info: bool = False
) -> dict:
    """Log through a *real* logger (makeRecord semantics) into a capture
    buffer and parse the emitted line."""
    buf = StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger(name)
    logger.addHandler(handler)
    previous, logger.level = logger.level, logging.INFO
    try:
        logger.info(msg, extra=extra or {}, stack_info=stack_info)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
    return json.loads(buf.getvalue())


def test_non_serializable_extra_degrades_to_str_not_crash() -> None:
    class Weird:
        def __str__(self) -> str:
            return "weird!"

    payload = render(extra={"thing": Weird()})
    assert payload["thing"] == "weird!"


def test_unicode_is_not_escaped() -> None:
    record = logging.LogRecord("t", logging.INFO, "f", 1, "héllo ✓", None, None)
    assert "héllo ✓" in JsonFormatter().format(record)


# --- redaction (§8.6) ---------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "needle"),
    [
        ("key is sk-abcdefghijklmnop1234 done", "sk-abcdefghijklmnop1234"),
        ("auth: Bearer dGhpcyBpcyBub3QgcmVhbA", "dGhpcyBpcyBub3QgcmVhbA"),
        (
            (
                "tok eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
                ".eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
            ),
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
        ),
    ],
)
def test_redact_secrets_hits_every_pattern_family(raw: str, needle: str) -> None:
    out = redact_secrets(raw)
    assert needle not in out
    assert "[redacted:" in out


def test_redact_secrets_leaves_prose_alone() -> None:
    plain = "Bearer scheme requires an Authorization header for the run loop"
    # "Bearer scheme" is followed by too few token-shaped chars to match.
    assert redact_secrets(plain) == plain


def test_redaction_applies_to_formatted_lines_including_tracebacks() -> None:
    payload = render(
        "config check found sk-abcdefghijklmno",
        extra={"path": "/etc/x"},
        exc=RuntimeError("while reading sk-zyxwvutsrqponmlk"),
    )
    line = json.dumps(payload)
    assert "sk-abcdefghijklmno" not in line
    assert "sk-zyxwvutsrqponmlk" not in line
    assert "[redacted:api_key]" in line


def test_pem_marker_cannot_eat_the_line_envelope() -> None:
    # A config-check finding may quote a header; redaction must stay inside
    # the string value (the rest of the record must still parse).
    payload = render(
        "found '-----BEGIN RSA PRIVATE KEY-----' marker",
        extra={"source": "config/keys.pem"},
    )
    assert (
        payload["source"].startswith("[redacted:private_key]")
        or payload["source"] == "config/keys.pem"
    )
    assert "[redacted:private_key]" in payload["msg"]


# --- configure_logging -----------------------------------------------------------


@pytest.fixture
def restore_root() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers.clear()
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)


def test_configure_logging_installs_single_json_handler(restore_root: None) -> None:
    buf = StringIO()
    configure_logging("INFO", stream=buf)
    configure_logging("INFO", stream=buf)  # idempotent: replaces, never stacks
    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, JsonFormatter)
    logging.getLogger("somewhere").info("line one")
    payload = json.loads(buf.getvalue().strip())
    assert payload["msg"] == "line one"


def test_configure_logging_level_filtering(restore_root: None) -> None:
    buf = StringIO()
    configure_logging(logging.WARNING, stream=buf)
    logging.getLogger("a").info("dropped")
    logging.getLogger("a").warning("kept")
    assert buf.getvalue().count("\n") == 1
    assert json.loads(buf.getvalue())["msg"] == "kept"


def test_configure_logging_rejects_unknown_level(restore_root: None) -> None:
    with pytest.raises(ValueError, match="unknown log level"):
        configure_logging("VERBOSE")


def test_configure_logging_mutes_uvicorn_access(restore_root: None) -> None:
    configure_logging(stream=StringIO())
    assert logging.getLogger("uvicorn.access").level == logging.WARNING


def test_configure_logging_detaches_uvicorn_plaintext_handlers(
    restore_root: None,
) -> None:
    """Regression (code review): uvicorn's own config puts StreamHandlers on
    the ``uvicorn``/``uvicorn.error`` loggers with propagate=False; leaving
    them there interleaves plaintext lines with the JSON stream and breaks
    the one-object-per-line contract the deploy pipeline relies on."""
    uvicorn_logger = logging.getLogger("uvicorn")
    plaintext = logging.StreamHandler(StringIO())
    uvicorn_logger.addHandler(plaintext)
    uvicorn_logger.propagate = False

    out = StringIO()
    configure_logging(stream=out)
    assert uvicorn_logger.handlers == []
    assert uvicorn_logger.propagate is True

    uvicorn_logger.info("Started server process [4242]")
    payload = json.loads(out.getvalue())
    assert payload["logger"] == "uvicorn"
    assert payload["msg"] == "Started server process [4242]"


async def test_middleware_replaces_inner_x_request_id_header() -> None:
    """Regression (code review): a response that already set X-Request-ID
    must not end up with two (potentially divergent) values — the wrapper
    replaces, it does not append."""
    captured: dict[str, Any] = {}

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        captured["rid"] = get_request_id()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"x-request-id", b"stale-upstream-id"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b"{}"})

    sent: list[dict[str, Any]] = []

    async def capture(message: dict[str, Any]) -> None:
        sent.append(message)

    await RequestContextMiddleware(inner)(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "path": "/x",
            "raw_path": b"/x",
            "query_string": b"",
            "scheme": "http",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("test", 80),
        },
        None,
        capture,
    )
    rid_values = [
        v.decode() for k, v in sent[0]["headers"] if k.lower() == b"x-request-id"
    ]
    assert rid_values == [captured["rid"]]  # exactly one, and it is ours
    assert "stale-upstream-id" not in str(sent[0]["headers"])


# --- request middleware --------------------------------------------------------


def build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/ping")
    async def ping() -> dict[str, Any]:
        return {"rid": get_request_id()}

    return app


@pytest.fixture
def access_lines() -> Iterator[list[dict[str, Any]]]:
    """Parsed access records, formatted *at emit time*.

    Formatting inside emit matters: the request_id contextvar is only alive
    in the emitting task (TestClient runs the app on a portal thread), which
    mirrors how production StreamHandler.emit behaves too.
    """
    seen: list[dict[str, Any]] = []
    formatter = JsonFormatter()

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(json.loads(formatter.format(record)))

    logger = logging.getLogger("vanilla.http")
    previous_level = logger.level
    handler = Collect()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    yield seen
    logger.removeHandler(handler)
    logger.setLevel(previous_level)


def test_middleware_binds_echoes_and_logs(access_lines: list[dict[str, Any]]) -> None:
    with TestClient(build_app()) as client:
        resp = client.get("/ping")
    rid = resp.json()["rid"]
    assert resp.headers["x-request-id"] == rid
    assert re.fullmatch(r"[0-9a-f]{32}", rid)

    (payload,) = access_lines
    assert payload["msg"] == "http_request"
    assert payload["request_id"] == rid
    assert payload["method"] == "GET"
    assert payload["path"] == "/ping"
    assert payload["status"] == 200
    assert payload["duration_ms"] >= 0.0


def test_middleware_logs_404_status_for_unknown_path(
    access_lines: list[dict[str, Any]],
) -> None:
    with TestClient(build_app()) as client:
        client.get("/nope")
    assert access_lines[0]["status"] == 404


def test_middleware_passes_non_http_scopes_through() -> None:
    calls: list[str] = []

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        calls.append(scope["type"])

    middleware = RequestContextMiddleware(inner)
    asyncio.run(middleware({"type": "lifespan"}, None, None))
    assert calls == ["lifespan"]


def test_new_request_id_shape() -> None:
    a, b = new_request_id(), new_request_id()
    assert re.fullmatch(r"[0-9a-f]{32}", a)
    assert a < b  # mint order = lex order (UUIDv7)
