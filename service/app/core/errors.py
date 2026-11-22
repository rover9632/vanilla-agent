"""AppError hierarchy and HTTP error plumbing (docs/plan.md T0.3).

One envelope for every failure on the API surface (§7):

    {"error": {"code": "...", "message": "...", "request_id": "...", "details": {...}}}

- ``code`` is a stable, machine-readable snake_case identifier — clients
  branch on it, never on ``message``. T13.5 (M13) completes the registry
  with run-level error codes; the set here is the M0 base every milestone
  from M3 on builds against, so *adding a code means adding a subclass*,
  which ``__init_subclass__`` forces to carry an explicit code+status.
  Invariant: one code maps to exactly one HTTP status — which is why the
  framework validation path owns ``validation_failed`` (422) instead of
  reusing ``invalid_request`` (400, raised from endpoints).
- ``message`` is safe to show a user: never embed provider payloads,
  secrets, or another user's identifiers in it. Structured additions go to
  ``details`` (same rule — details ride in the response body).
- ``request_id`` is always present as a key (null when no request context,
  e.g. CLI usage) so clients never probe for it.

Unexpected exceptions get the generic 500 treatment: the traceback is
logged (correlated with the request id via the ContextVar) but nothing of
it is serialized into the response — internals do not leak.

Deliberate gaps: Starlette's ``HTTPException`` is not wrapped (endpoints
raise AppError subclasses instead — translating a framework exception into
this envelope would legitimize raising it); tool-level permission *denials*
(M7) are results inside the run, not HTTP errors, and are not in this
hierarchy.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from contextlib import nullcontext
from typing import Any, ClassVar

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.responses import JSONResponse

from app.core.logging import (
    REQUEST_ID_SCOPE_KEY,
    bound_request_id,
    get_logger,
    get_request_id,
    redact_secrets,
)

_logger = get_logger("vanilla.errors")

# The one envelope code with no AppError class (synthesized by the
# RequestValidationError handler). Kept out of the class registry so the
# code↔status uniqueness invariant still holds across both sources.
VALIDATION_FAILED_CODE = "validation_failed"


def _envelope(
    code: str,
    message: str,
    *,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the response body; ``request_id`` is read from the ambient
    context here so every construction route (exceptions and synthesized
    framework-error bodies) shares one shape."""
    body: dict[str, Any] = {
        "code": code,
        "message": message,
        "request_id": get_request_id(),
    }
    if details:
        body["details"] = dict(details)
    return {"error": body}


class AppError(Exception):
    """Base class: code + HTTP status + user-safe message (+ details)."""

    code: ClassVar[str] = "internal_error"
    http_status: ClassVar[int] = 500

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Force every subclass to declare its own identity; inheriting the
        # base's ("internal_error", 500) by accident would silently turn a
        # designed failure into an opaque server error.
        if "code" not in cls.__dict__ or "http_status" not in cls.__dict__:
            raise TypeError(
                f"{cls.__name__}: subclasses must define class-level `code` "
                f"and `http_status`"
            )

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = dict(details) if details else {}

    def to_payload(self) -> dict[str, Any]:
        """The response envelope (ambient request_id included). Usable
        outside HTTP too — CLI callers get ``request_id: null``."""
        return _envelope(self.code, self.message, details=self.details or None)


# --- client-side (4xx) ----------------------------------------------------------


class ClientError(AppError):
    """A request that will never succeed as written."""

    code = "client_error"
    http_status = 400


class InvalidRequestError(ClientError):
    code = "invalid_request"
    http_status = 400


class AuthenticationError(ClientError):
    """No valid session principal (cookie missing/expired/revoked) — §7."""

    code = "unauthenticated"
    http_status = 401


class PermissionDeniedError(ClientError):
    """Authenticated, but not allowed to perform *this HTTP action* (CSRF
    mismatch, disabled account, registration closed). Distinct from M7's
    tool-permission decisions, which are run-internal results, not HTTP
    errors."""

    code = "permission_denied"
    http_status = 403


class NotFoundError(ClientError):
    code = "not_found"
    http_status = 404


class ConflictError(ClientError):
    """Current resource state forbids the action (duplicate idempotent
    submit, archived thread, ...)."""

    code = "conflict"
    http_status = 409


class RateLimitedError(ClientError):
    """Budget/limiter refusal (§8.10, §8.11): login & register buckets,
    per-user concurrency, the global hourly-token circuit breaker, and the
    container-capacity 429 all land here with a Retry-After hint."""

    code = "rate_limited"
    http_status = 429

    def __init__(
        self,
        message: str,
        *,
        retry_after_s: float | None = None,
        details: Mapping[str, Any] | None = None,
    ):
        super().__init__(message, details=details)
        self.retry_after_s = retry_after_s

    @property
    def finite_retry_after_s(self) -> float | None:
        """The retry hint *usable* in a header/body, or None.

        inf/NaN — from arithmetic slips like ``budget / rate == inf`` or a
        "never retry" sentinel — must not reach ``math.ceil`` (OverflowError
        inside the exception handler would turn the 429 into a 500) nor the
        JSON body (float('inf') serializes as the non-standard ``Infinity``,
        which browsers' JSON.parse rejects). Non-finite means "no hint".
        """
        if self.retry_after_s is None or not math.isfinite(self.retry_after_s):
            return None
        return max(0.0, self.retry_after_s)

    def to_payload(self) -> dict[str, Any]:
        # retry_after_s also rides in the body: the Retry-After *header*
        # exists only on the HTTP path, but limiters are consumed from the
        # agent loop and CLIs too ("usable outside HTTP" per AppError).
        details = dict(self.details)
        hint = self.finite_retry_after_s
        if hint is not None:
            details.setdefault("retry_after_s", hint)
        return _envelope(self.code, self.message, details=details or None)


# --- server-side (5xx) -----------------------------------------------------------


class ServerError(AppError):
    """A failure on our side; retry may or may not help, nothing about the
    request was wrong."""

    code = "server_error"
    http_status = 500


class ProviderError(ServerError):
    """The model provider failed (transport error, 4xx/5xx from upstream,
    malformed stream). The upstream body never goes into ``message``/
    ``details`` — it can echo prompt content (plan §8.5)."""

    code = "provider_error"
    http_status = 502


# --- FastAPI plumbing -------------------------------------------------------------


def install_error_handlers(app: FastAPI) -> None:
    """Attach the exception handlers that realize the envelope contract.

    Call once at app construction (with the API routers, in M5). Handlers
    are the *only* place this module touches the framework; the exception
    classes themselves are transport-agnostic so the agent loop and CLIs
    can raise them without FastAPI in the picture.
    """

    def _rid_headers() -> dict[str, str]:
        """Stamp X-Request-ID from the handler side. The middleware
        (re-)stamps the same value on its way out and *replaces* rather than
        appends, so there is never a duplicate; handlers used without the
        middleware still get the header."""
        rid = get_request_id()
        return {"X-Request-ID": rid} if rid else {}

    async def app_error(request: Request, exc: AppError) -> JSONResponse:
        headers = _rid_headers()
        if isinstance(exc, RateLimitedError):
            hint = exc.finite_retry_after_s
            if hint is not None:
                headers["Retry-After"] = str(max(1, math.ceil(hint)))
        if exc.http_status >= 500:
            # Server-side AppErrors are incidents, not client errors: one
            # ERROR-shaped log line each, response stays generic below the
            # fold of the traceback.
            _logger.error(
                "app_error",
                extra={"code": exc.code, "status": exc.http_status},
                exc_info=exc,
            )
        return JSONResponse(
            exc.to_payload(), status_code=exc.http_status, headers=headers
        )

    async def request_invalid(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Starlette's default body embeds `input`/`ctx` for every failing
        # field — including values of fields that *passed* — so we rebuild
        # it down to loc/msg/type and run msg through the §8.6 redactor.
        # Honest limit: a custom validator may interpolate its own field's
        # value into its message (the f-string is the validator's, not
        # pydantic's); that text goes back only to the client that sent it.
        # Project rule for validators (passwords, tokens): do not embed
        # submitted values in messages — the redactor is pattern-based, not
        # a general echo guard.
        fields = [
            {
                "loc": ".".join(str(part) for part in err["loc"]),
                "msg": redact_secrets(str(err["msg"])),
                "type": err["type"],
            }
            for err in exc.errors()
        ]
        return JSONResponse(
            _envelope(
                VALIDATION_FAILED_CODE,
                "request failed validation",
                details={"fields": fields},
            ),
            status_code=422,
            headers=_rid_headers(),
        )

    async def server_internal(request: Request, exc: Exception) -> JSONResponse:
        # Registered for Exception: runs under Starlette's ServerErrorMiddleware,
        # which sits *outside* RequestContextMiddleware — the ambient request-id
        # contextvar is already reset here. Recover it from the scope mirror and
        # rebind so both the traceback log line and the envelope stay correlated.
        # The outer layer sends this response directly to the client, so this
        # handler must stamp the X-Request-ID header itself (nothing re-stamps
        # it on this path).
        scope_id = request.scope.get(REQUEST_ID_SCOPE_KEY)
        context = (
            bound_request_id(scope_id) if isinstance(scope_id, str) else nullcontext()
        )
        headers = {"X-Request-ID": scope_id} if isinstance(scope_id, str) else {}
        # The generic message and the logged traceback are intentionally the
        # only two views of this event (§8.6: no internals over the wire).
        with context:
            _logger.exception("unhandled_error", exc_info=exc)
            return JSONResponse(
                _envelope(AppError.code, "internal server error"),
                status_code=500,
                headers=headers,
            )

    # Starlette types the handler parameter as `exc: Exception`; FastAPI's
    # documented convention is to narrow it to the registered class (the
    # lookup guarantees the argument type). The ignore is per-registration,
    # not in the signatures, so the bodies keep the precise types.
    app.add_exception_handler(AppError, app_error)  # type: ignore[arg-type]
    app.add_exception_handler(
        RequestValidationError,
        request_invalid,  # type: ignore[arg-type]
    )
    app.add_exception_handler(Exception, server_internal)
