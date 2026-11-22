"""Structured JSON logging with request-id correlation (docs/plan.md T0.3).

One JSON object per line on stdout — the deploy log pipeline (and future
log shipping, explicitly out of v1 scope) is the only consumer; humans read
it through a pager that pretty-prints.

Record shape (stable keys):

    ts          ISO-8601 UTC, ms precision, from app.core.clock
    level       logging level name
    logger      dotted logger name
    msg         the message ("event-style": short literal identifiers,
                everything variable goes in fields)
    request_id  present iff a request context is bound (see below)
    exception   formatted traceback, present iff exc_info was passed

Everything passed via ``extra={...}`` is merged as a top-level field.
Attributes that would collide with the stable keys are renamed with an
``x_`` prefix rather than silently overwriting them; values must be
JSON-serializable (anything else is stringified — don't log objects).

Correlation: a ContextVar carries the active request id.
:class:`RequestContextMiddleware` binds it per HTTP request (honoring a
clean inbound ``X-Request-ID``, otherwise minting one) and emits the access
record. Background work without a request context (run tasks, reconcile)
may bind one explicitly with :func:`bound_request_id`.

Redaction (§8.6): the *serialized line* passes through
:func:`redact_secrets` — OpenAI-style ``sk-`` keys, Bearer tokens, JWTs and
PEM private-key blocks are replaced regardless of which field they arrived
in (including tracebacks). Event payloads reuse the same function from the
event recorder later; there is no opt-out for a "trusted" log line.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, TextIO

from app.core import clock, ids

# --- request-id context -------------------------------------------------------

request_id_var: ContextVar[str | None] = ContextVar("vanilla_request_id", default=None)

# Scope key mirror of the contextvar. Starlette's ServerErrorMiddleware (the
# `Exception` handler) lives *outside* this middleware: by the time it runs,
# the contextvar reset is done. The scope dict survives the whole stack, so
# the unhandled-error handler reads the id back from here (errors.py).
REQUEST_ID_SCOPE_KEY = "vanilla_request_id"


def get_request_id() -> str | None:
    """Active request id for this task/context, or None outside a request."""
    return request_id_var.get()


def new_request_id() -> str:
    """Compact (32 hex) id for the X-Request-ID header.

    UUIDv7-based so incident timelines can be ordered by eyeballing an id.
    """
    return ids.new_id().hex


@contextmanager
def bound_request_id(request_id: str) -> Generator[str]:
    """Bind *request_id* for the duration of the block (background tasks)."""
    token = request_id_var.set(request_id)
    try:
        yield request_id
    finally:
        request_id_var.reset(token)


# --- secret redaction (plan §8.6) ----------------------------------------------

# (label, pattern) pairs, applied in order to already-serialized text. The
# character classes deliberately stop at quotes/whitespace so a match never
# crosses JSON string boundaries and cannot corrupt the envelope.
_REDACTIONS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private_key",
        # \1 backreference ties the END label to the BEGIN label. The body is
        # capped at [^"] so a lone "-----BEGIN ... PRIVATE KEY-----" mention
        # (config-check findings do contain them) can never eat past its own
        # JSON string value and destroy the line envelope.
        re.compile(r'-----BEGIN ([A-Z0-9 ]*PRIVATE KEY)-----[^"]*'),
    ),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}"),
    ),
    # The token class excludes space: real bearer secrets contain none, and
    # letting spaces in would redact prose ("Bearer tokens are required...").
    ("bearer", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)),
    ("api_key", re.compile(r"\bsk-[A-Za-z0-9_-]{6,}")),
)


def redact_secrets(text: str) -> str:
    """Replace secret-shaped substrings with ``[redacted:<label>]``.

    Public so the event recorder (T2.3) and tool-output handling (§8.6) run
    the *same* patterns — a secret redacted in logs but shipped in an event
    payload is not redacted.
    """
    for label, pattern in _REDACTIONS:
        text = pattern.sub(f"[redacted:{label}]", text)
    return text


# --- JSON formatter --------------------------------------------------------------

# Attribute names a fresh LogRecord always carries — anything *else* on the
# record came from `extra=` and belongs in the JSON payload.
_PROBE_RECORD = logging.LogRecord("", logging.INFO, "", 0, "", None, None)
_RECORD_STANDARD = frozenset(vars(_PROBE_RECORD)) | {"asctime", "message", "taskName"}

# Keys a consumer may rely on; `extra` values colliding with them are prefixed.
# "stack" belongs here even though it is only set *after* the extras loop (like
# "exception"): reserving it is what stops an extra named "stack" from being
# silently overwritten by the formatted stack instead of moved to x_stack.
_CORE_KEYS = frozenset(
    {"ts", "level", "logger", "msg", "request_id", "exception", "stack"}
)

_LEVEL_BY_NAME = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


class JsonFormatter(logging.Formatter):
    """Render each record as one redacted JSON line. See module docstring."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": clock.now().isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        request_id = get_request_id()
        if request_id is not None:
            payload["request_id"] = request_id
        for key, value in vars(record).items():
            if key in _RECORD_STANDARD:
                continue
            # "exception"/"stack" may not be in payload yet when the record
            # has no exc_info — _CORE_KEYS reserves them either way.
            payload[
                key if key not in payload and key not in _CORE_KEYS else f"x_{key}"
            ] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        # default=str keeps a non-serializable extra from taking down the log
        # pipeline (it degrades the field, not the record).
        return redact_secrets(json.dumps(payload, ensure_ascii=False, default=str))


def get_logger(name: str) -> logging.Logger:
    """Project-convention accessor (kept so callers never sprinkle the
    stdlib call and a later binding change is one-file)."""
    return logging.getLogger(name)


def configure_logging(
    level: int | str = "INFO", *, stream: TextIO | None = None
) -> None:
    """Replace root handlers with the single JSON stream handler. Idempotent.

    Called from the app lifespan (M0 onwards) and by scripts — after
    uvicorn has configured its own logging, which matters: uvicorn's default
    LOGGING_CONFIG attaches plaintext StreamHandlers to ``uvicorn`` /
    ``uvicorn.error`` with ``propagate=False``, so "Started server process"
    and ASGI tracebacks would otherwise interleave non-JSON lines with our
    stream and break the one-object-per-line contract for the log pipeline.
    We detach those handlers and force propagation into root (records keep
    their logger name, so the JSON stays attributable). ``uvicorn.access``
    is additionally muted — :class:`RequestContextMiddleware` emits a richer
    access record per request; the plaintext one would be a second, worse
    source of truth.
    """
    if isinstance(level, str):
        # Own mapping rather than logging.getLevelName(str) (deprecated
        # string->int case) or getLevelByName (not in older typeshed).
        try:
            resolved = _LEVEL_BY_NAME[level.upper()]
        except KeyError:
            raise ValueError(f"unknown log level: {level!r}") from None
    else:
        resolved = level
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(resolved)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        for handler in list(uvicorn_logger.handlers):
            uvicorn_logger.removeHandler(handler)
        uvicorn_logger.propagate = True
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


# --- HTTP request context middleware --------------------------------------------

# Inbound X-Request-ID is honored only when it looks like an opaque id. This
# is a trust boundary, not friendliness: arbitrary client bytes in correlated
# log lines invite log injection and make grep-by-id unreliable.
_INBOUND_REQUEST_ID = re.compile(r"\A[A-Za-z0-9._~-]{1,128}\Z")


def _header(scope: dict[str, Any], name: bytes) -> str | None:
    # ASGI delivers scope headers as latin-1 *bytes* tuples (spec + Starlette
    # convention); decode for the contextvar/header round trip.
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return value.decode("latin-1")
    return None


class RequestContextMiddleware:
    """Bind a request id, echo it back, and log one access record.

    Pure ASGI on purpose (docs/plan.md §7): SSE responses stay open for the
    lifetime of a run, and Starlette's ``BaseHTTPMiddleware`` is documented
    to interfere with streaming and cancellation; a thin wrapper around
    ``send`` has neither problem.
    """

    def __init__(self, app: Any, *, logger: logging.Logger | None = None) -> None:
        self.app = app
        self.logger = logger or get_logger("vanilla.http")

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            # websocket/lifespan: nothing to correlate or measure here.
            await self.app(scope, receive, send)
            return

        inbound = _header(scope, b"x-request-id")
        request_id = (
            inbound
            if inbound is not None and _INBOUND_REQUEST_ID.match(inbound)
            else new_request_id()
        )
        token = request_id_var.set(request_id)
        scope[REQUEST_ID_SCOPE_KEY] = request_id
        started = clock.monotonic()
        # If the app dies before sending a response, the outer error layer
        # answers 500 — record that rather than a fictitious 200.
        status = 500

        rid_header = b"x-request-id"

        def _is_rid_header(raw: Any) -> bool:
            name = raw[0]
            if isinstance(name, bytes):
                name = name.decode("latin-1")
            return name.lower() == "x-request-id"

        async def send_wrapper(message: dict[str, Any]) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
                # Replace rather than append: an inner handler (or the error
                # handlers in core/errors.py, which also stamp the id) may
                # have set X-Request-ID already — two divergent values would
                # make header-vs-envelope agreement client-dependent luck.
                headers = [h for h in message["headers"] if not _is_rid_header(h)]
                headers.append((rid_header, request_id.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            client = scope.get("client") or ("", 0)
            self.logger.info(
                "http_request",
                extra={
                    "method": scope.get("method", ""),
                    "path": scope.get("path", ""),
                    "status": status,
                    "duration_ms": round((clock.monotonic() - started) * 1000, 1),
                    "client_ip": client[0],
                },
            )
            request_id_var.reset(token)
