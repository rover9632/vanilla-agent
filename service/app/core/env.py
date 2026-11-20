"""Deployment-environment settings (docs/plan.md T0.2).

This module is the *only* place that reads process environment variables.
It deliberately covers **deployment-scoped values only** (paths, the provider
API key, session cookie policy, dev-only switches, the sandbox image ref).
Capability parameters (models, limits, tools, permissions, MCP servers, ...)
live in ``config/*.yaml`` and are loaded by ``app/config`` (ADR-015) — never
as environment variables. The field set below is a closed whitelist; the unit
test pins it so new capability knobs cannot creep in here.

Path semantics: ``REPO_ROOT`` defaults to the repository root; the other path
variables are ``~``-expanded and, if relative, anchored at ``REPO_ROOT`` —
the same rule config files use (plan §5).

Notes:
- ``SESSION_SECRET`` intentionally does not exist: sessions are opaque
  tokens verified by sha256 lookup in the DB (ADR-004, plan §11), so there is
  no signing key to configure.
- Error messages produced here never echo variable *values*, only names and
  problems (plan §8.6: secrets must not leak into logs).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root as seen from a normal checkout: service/app/core/env.py -> up 3.
# Overridable via REPO_ROOT for packaging layouts that move the data dir.
REPO_ROOT: Path = Path(__file__).resolve().parents[3]

# Default sandbox image, built by `make sandbox-build` (plan §11).
DEFAULT_SANDBOX_IMAGE = "vanilla-agent/sandbox:latest"

# Sliding session expiry in days (plan §8.1).
DEFAULT_SESSION_TTL_DAYS = 30

# The complete, closed set of deployment env vars (upper-case names).
DEPLOY_ENV_VARS: frozenset[str] = frozenset(
    {
        "REPO_ROOT",
        "DATA_DIR",
        "CONFIG_DIR",
        "DATABASE_PATH",
        "OPENAI_API_KEY",
        "SESSION_TTL_DAYS",
        "SESSION_COOKIE_SECURE",
        "ALLOW_LOCAL_SANDBOX",
        "CONFIG_ALLOW_LOCAL",
        "SANDBOX_IMAGE",
    }
)


class EnvError(ValueError):
    """Fail-fast error for the deployment environment.

    Carries one human-readable line per invalid/missing variable. Variable
    values are never included.
    """


def _expand(raw: str | Path, root: Path) -> Path:
    """`~`-expand *raw*; anchor relative paths at *root*."""
    p = Path(raw).expanduser()
    return p if p.is_absolute() else root / p


class EnvSettings(BaseSettings):
    """Process-environment view of the deployment. See module docstring."""

    # frozen: load_env() is lru_cached, so this instance is shared by every
    # consumer in the process — a stray attribute write would poison them all.
    # (The before-validator below is unaffected: it rewrites the *input dict*,
    # never the model.)
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )

    # --- deployment paths ---------------------------------------------------
    # The declared defaults below are documentation only: _resolve_path_defaults
    # (a before-validator) always injects fully-anchored values into the input,
    # so these literals never reach a constructed instance. They exist so
    # type-checkers accept the no-arg constructor that env-magic performs.
    repo_root: Path = REPO_ROOT
    data_dir: Path = Path("data")
    config_dir: Path = Path("config")
    database_path: Path = Path("data/vanilla.db")

    # --- provider credentials (the single global key, plan §13-D2) ----------
    openai_api_key: str

    # --- session policy (ADR-004; no signing secret by design) ---------------
    session_ttl_days: int = DEFAULT_SESSION_TTL_DAYS
    session_cookie_secure: bool = True

    # --- dev-only switches (plan §8.4, §8.7; production must leave these off)
    allow_local_sandbox: bool = False
    config_allow_local: bool = False

    # --- sandbox image reference ----------------------------------------------
    sandbox_image: str = DEFAULT_SANDBOX_IMAGE

    @model_validator(mode="before")
    @classmethod
    def _resolve_path_defaults(cls, data: Any) -> Any:
        """Anchor path vars at REPO_ROOT and derive DATABASE_PATH from DATA_DIR.

        Runs *before* field validation so the path fields keep a concrete
        ``Path`` type for consumers (no ``| None`` leaking out).
        """
        if not isinstance(data, dict):
            return data
        values = dict(data)
        root = Path(values.get("repo_root", REPO_ROOT)).expanduser()
        values["repo_root"] = root
        values["data_dir"] = _expand(values.get("data_dir", "data"), root)
        values["config_dir"] = _expand(values.get("config_dir", "config"), root)
        db = values.get("database_path")
        values["database_path"] = (
            _expand(db, root) if db else values["data_dir"] / "vanilla.db"
        )
        return values

    @field_validator("openai_api_key")
    @classmethod
    def _api_key_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be a non-empty string")
        return v

    @field_validator("session_ttl_days")
    @classmethod
    def _ttl_in_range(cls, v: int) -> int:
        if not 1 <= v <= 365:
            raise ValueError("must be between 1 and 365 days")
        return v

    @field_validator("sandbox_image")
    @classmethod
    def _image_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be a non-empty image reference")
        return v


@lru_cache(maxsize=1)
def load_env() -> EnvSettings:
    """Build :class:`EnvSettings` from the process environment, fail-fast.

    All problems are reported at once (pydantic collects them), matching the
    collect-don't-fail-on-first style used across the config layer (T1.2).
    Raises:
        EnvError: one line per problem; never contains variable values.
    """
    try:
        # openai_api_key is env-provided at runtime; mypy cannot see the
        # pydantic-settings source, so it flags the no-arg call. Keeping the
        # field required (rather than defaulting it) is the fail-fast itself.
        return EnvSettings()  # type: ignore[call-arg]
    except ValidationError as exc:
        lines = []
        for err in exc.errors():
            loc = ".".join(str(part) for part in err["loc"]) or "<input>"
            lines.append(f"{loc.upper()}: {err['msg']}")
        raise EnvError(
            "invalid deployment environment:\n  " + "\n  ".join(lines)
        ) from None
