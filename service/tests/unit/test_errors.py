"""Unit tests for app.core.errors — hierarchy, code mapping, handlers (T0.3).

The plan's verification gate calls out *error code mapping*: the registry
below is pinned in both directions (every subclass registered, every
registered name exists), so a code cannot be added, renamed, or dropped
without touching the docs/plan.md §7 story that T13.5 completes.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, field_validator

from app.core.errors import (
    AppError,
    AuthenticationError,
    ClientError,
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
    ProviderError,
    RateLimitedError,
    ServerError,
    install_error_handlers,
)
from app.core.logging import (
    RequestContextMiddleware,
    bound_request_id,
    get_logger,
)

# The pinned registry: name -> (code, http_status).
EXPECTED_REGISTRY: dict[str, tuple[str, int]] = {
    "AppError": ("internal_error", 500),
    "ClientError": ("client_error", 400),
    "InvalidRequestError": ("invalid_request", 400),
    "AuthenticationError": ("unauthenticated", 401),
    "PermissionDeniedError": ("permission_denied", 403),
    "NotFoundError": ("not_found", 404),
    "ConflictError": ("conflict", 409),
    "RateLimitedError": ("rate_limited", 429),
    "ServerError": ("server_error", 500),
    "ProviderError": ("provider_error", 502),
}


def _walk(cls: type[AppError]) -> Iterator[type[AppError]]:
    for sub in cls.__subclasses__():
        yield sub
        yield from _walk(sub)


# --- registry / hierarchy ---------------------------------------------------------


def test_code_and_status_mapping_is_exactly_the_registry() -> None:
    found = {
        cls.__name__: (cls.code, cls.http_status)
        for cls in (AppError, *_walk(AppError))
    }
    assert found == EXPECTED_REGISTRY


def test_error_codes_are_unique() -> None:
    codes = [cls.code for cls in _walk(AppError)]
    # ServerError shares 500 with the base but must own a distinct code;
    # base AppError itself is part of the surface (handlers emit it).
    assert len(set(codes)) == len(codes)


def test_hierarchy_shape() -> None:
    """4xx and 5xx hang off their own intermediate, both under AppError."""
    assert issubclass(InvalidRequestError, ClientError)
    assert issubclass(NotFoundError, ClientError)
    assert issubclass(ProviderError, ServerError)
    assert issubclass(ClientError, AppError)
    assert issubclass(ServerError, AppError)


def test_subclass_without_identity_is_rejected() -> None:
    with pytest.raises(TypeError, match="must define class-level"):

        class Sloppy(AppError):
            pass

    with pytest.raises(TypeError, match="must define class-level"):

        class HalfBaked(AppError):
            code = "half_baked"


# --- payload shape ------------------------------------------------------------------


def test_envelope_has_the_stable_keys() -> None:
    exc = NotFoundError("thread does not exist")
    assert exc.to_payload() == {
        "error": {
            "code": "not_found",
            "message": "thread does not exist",
            "request_id": None,
        }
    }


def test_details_present_only_when_given() -> None:
    assert "details" not in AppError("x").to_payload()["error"]
    body = ConflictError("dup", details={"client_msg_id": "abc"}).to_payload()
    assert body["error"]["details"] == {"client_msg_id": "abc"}


def test_envelope_picks_up_ambient_request_id() -> None:
    with bound_request_id("deadbeef"):
        assert AppError("x").to_payload()["error"]["request_id"] == "deadbeef"


def test_message_survives_str_of_exception() -> None:
    assert "gone" in str(NotFoundError("gone"))


def test_rate_limited_carries_retry_after() -> None:
    assert RateLimitedError("easy").retry_after_s is None
    assert RateLimitedError("easy", retry_after_s=3.5).retry_after_s == 3.5
    # non-HTTP consumers get the hint in the body, not just the header
    body = RateLimitedError("easy", retry_after_s=3.5).to_payload()["error"]
    assert body["details"]["retry_after_s"] == 3.5
    assert "details" not in RateLimitedError("easy").to_payload()["error"]


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
def test_non_finite_retry_after_is_no_hint(bad: float) -> None:
    """inf/NaN must degrade to "no hint", never crash the handler
    (math.ceil(inf) raises OverflowError -> a 429 would exit as a 500) and
    never emit JSON-invalid Infinity/NaN into the body."""
    exc = RateLimitedError("easy", retry_after_s=bad)
    assert exc.finite_retry_after_s is None
    assert "details" not in exc.to_payload()["error"]


# --- HTTP handlers ---------------------------------------------------------------


class EchoBody(BaseModel):
    name: str
    secret: str


RAISERS: dict[str, AppError] = {
    "invalid": InvalidRequestError("bad input"),
    "unauth": AuthenticationError("no session"),
    "denied": PermissionDeniedError("csrf mismatch"),
    "missing": NotFoundError("no such thread", details={"kind": "thread"}),
    "conflict": ConflictError("already seen"),
    "limited": RateLimitedError("slow down", retry_after_s=12),
    "limited_inf": RateLimitedError("locked out", retry_after_s=float("inf")),
    "provider": ProviderError("model upstream failed"),
}


class TokenBody(BaseModel):
    """The custom-validator shape: the message text interpolates the input —
    exactly what validators must not do with secret fields (errors.py notes
    the convention); here it proves the msg redactor is wired."""

    token: str

    @field_validator("token")
    @classmethod
    def _reject(cls, v: str) -> str:
        raise ValueError(f"token {v!r} rejected (ref sk-abcdefghijklmnop1234)")


def build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)

    @app.post("/echo")
    async def echo(body: EchoBody) -> dict[str, str]:
        return {"name": body.name}

    @app.post("/token")
    async def token(body: TokenBody) -> dict[str, str]:
        return {"ok": body.token}

    @app.get("/raise/{kind}")
    async def raise_(kind: str) -> None:
        raise RAISERS[kind]

    @app.get("/explode")
    async def explode() -> None:
        raise RuntimeError("sensitive internal state")

    @app.get("/rid")
    async def rid() -> dict[str, Any]:
        from app.core.logging import get_request_id

        return {"rid": get_request_id()}

    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    # raise_server_exceptions=False: the 500 path re-raises past the
    # handler by design (ServerErrorMiddleware), we want the response.
    with TestClient(build_app(), raise_server_exceptions=False) as c:
        yield c


@pytest.mark.parametrize("kind", sorted(RAISERS))
def test_each_error_maps_to_its_status_and_code(client: TestClient, kind: str) -> None:
    exc = RAISERS[kind]
    resp = client.get(f"/raise/{kind}")
    assert resp.status_code == exc.http_status
    body = resp.json()["error"]
    assert body["code"] == exc.code
    assert body["message"] == exc.message
    assert body["request_id"]  # middleware is installed -> non-null


def test_unknown_kind_becomes_generic_500_without_leaking(client: TestClient) -> None:
    resp = client.get("/raise/whatever")
    assert resp.status_code == 500
    body = resp.json()["error"]
    assert body["code"] == "internal_error"
    assert body["message"] == "internal server error"
    # the KeyError's message (and any other internal text) must not ride:
    assert "whatever" not in body["message"]


def test_unhandled_exception_envelope_keeps_request_id(client: TestClient) -> None:
    """Regression for the middleware ordering fact: ServerErrorMiddleware
    runs *outside* RequestContextMiddleware, so the handler must recover the
    request id from the scope mirror, not the (already-reset) contextvar.
    The same path never passes through the middleware's header stamping, so
    the handler must add X-Request-ID itself — header and body must agree."""
    resp = client.get("/explode")
    assert resp.status_code == 500
    request_id = resp.json()["error"]["request_id"]
    assert request_id is not None
    assert resp.headers["x-request-id"] == request_id


@pytest.mark.parametrize(
    ("method", "path", "json_body", "status"),
    [
        ("get", "/raise/missing", None, 404),
        ("get", "/raise/limited", None, 429),
        ("post", "/echo", {"name": 123, "secret": "s"}, 422),
    ],
)
def test_envelope_request_id_matches_header_on_every_error_path(
    client: TestClient, method: str, path: str, json_body: dict, status: int
) -> None:
    """The body/header must never disagree for *any* client — one stamping
    source (middleware replace, handler same value) pins that."""
    resp = client.request(method, path, json=json_body)
    assert resp.status_code == status
    assert resp.headers["x-request-id"] == resp.json()["error"]["request_id"]
    # exactly one x-request-id header (the middleware replaces, not appends)
    assert [k for k, _ in resp.headers.multi_items()].count("x-request-id") == 1


def test_validation_error_never_echoes_input(client: TestClient) -> None:
    resp = client.post("/echo", json={"name": 123, "secret": "sup3rs3cr3t"})
    assert resp.status_code == 422
    body = resp.json()["error"]
    assert body["code"] == "validation_failed"
    (field,) = body["details"]["fields"]
    assert field["loc"] == "body.name"
    assert field["msg"] == "Input should be a valid string"  # name given an int
    # The `secret` field validated fine and must still not ride anywhere:
    # starlette's default body would have echoed it under input/ctx.
    assert "sup3rs3cr3t" not in resp.text


def test_validator_message_with_secret_shape_is_redacted(client: TestClient) -> None:
    """Custom validators may interpolate their own field's value into the
    message (that text goes back only to the sender — documented limit).
    The guarantee the handler actually makes: §8.6 *pattern-shaped* secrets
    do not ride the wire, even inside validator prose."""
    resp = client.post("/token", json={"token": "hunter2-s3cr3t"})
    assert resp.status_code == 422
    (field,) = resp.json()["error"]["details"]["fields"]
    assert "sk-abcdefghijklmnop1234" not in resp.text
    assert "[redacted:api_key]" in field["msg"]


def test_invalid_request_and_validation_failed_are_distinct_codes(
    client: TestClient,
) -> None:
    """One code maps to exactly one status: the endpoint-raised 400 must not
    share its code with the framework-synthesized 422."""
    raised = client.get("/raise/invalid")
    framework = client.post("/echo", json={})
    assert raised.status_code == 400
    assert raised.json()["error"]["code"] == "invalid_request"
    assert framework.status_code == 422
    assert framework.json()["error"]["code"] == "validation_failed"


def test_rate_limited_sets_retry_after_header(client: TestClient) -> None:
    resp = client.get("/raise/limited")
    assert resp.headers["Retry-After"] == "12"
    assert resp.json()["error"]["details"]["retry_after_s"] == 12


def test_infinite_retry_after_stays_a_429_without_the_hint(
    client: TestClient,
) -> None:
    """Regression: math.ceil(inf) inside the handler raised OverflowError,
    which turned the designed 429 into a generic 500."""
    resp = client.get("/raise/limited_inf")
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "rate_limited"
    assert "retry-after" not in resp.headers
    assert "details" not in resp.json()["error"]


def test_server_side_app_errors_are_logged_with_traceback(
    client: TestClient,
) -> None:
    seen: list[logging.LogRecord] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record)

    logger = get_logger("vanilla.errors")
    handler = Collect()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        client.get("/raise/provider")  # 5xx class -> logged as incident
        client.get("/raise/missing")  # 4xx class -> not logged
    finally:
        logger.removeHandler(handler)
    incident = [r for r in seen if r.getMessage() == "app_error"]
    assert len(incident) == 1
    assert incident[0].exc_info is not None


def test_error_body_carries_details(client: TestClient) -> None:
    body = client.get("/raise/missing").json()["error"]
    assert body["details"] == {"kind": "thread"}


def test_request_id_header_and_body_agree(client: TestClient) -> None:
    resp = client.get("/rid")
    assert resp.json()["rid"] == resp.headers["x-request-id"]


def test_clean_inbound_request_id_is_honored(client: TestClient) -> None:
    resp = client.get("/rid", headers={"X-Request-ID": "trace-abc.123_XY"})
    assert resp.json()["rid"] == "trace-abc.123_XY"
    assert resp.headers["x-request-id"] == "trace-abc.123_XY"


def test_dirty_inbound_request_id_is_replaced(client: TestClient) -> None:
    junk = "has spaces" + "x" * 200  # whitespace + length both violate the shape
    resp = client.get("/rid", headers={"X-Request-ID": junk})
    rid = resp.json()["rid"]
    assert rid is not None and rid != junk
    assert set(rid) <= set("0123456789abcdef")  # minted hex, injection-free
