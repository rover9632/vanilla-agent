"""Deployment-environment settings (docs/plan.md T0.2).

This module is the *only* place that reads process environment variables.
It deliberately covers **deployment-scoped values only** (paths, the provider
API key, session cookie policy, dev-only switches, the sandbox image ref).
Capability parameters (models, limits, tools, permissions, MCP servers, ...)
live in ``config/*.yaml`` and are loaded by ``app/config`` (ADR-015) — never
as environment variables. The field set below is a closed whitelist; the unit
test pins it so new capability knobs cannot creep in here.

Path semantics: ``REPO_ROOT`` defaults to the repository root and, when set,
must expand (``~``) to an **absolute** path — it anchors everything else.
The other path variables are ``~``-expanded and, if relative, anchored at
``REPO_ROOT`` — the same rule config files use (plan §5). Present-but-blank
values are rejected rather than silently falling back to defaults. The
repo-root ``.env`` is resolved at *call* time from the runtime ``REPO_ROOT``,
not at import time, so relocating the deployment relocates the dotenv file
with it.

Notes:
- ``SESSION_SECRET`` intentionally does not exist: sessions are opaque
  tokens verified by sha256 lookup in the DB (ADR-004, plan §11), so there is
  no signing key to configure.
- Error messages produced here never echo variable *values*, only names and
  problems (plan §8.6: secrets must not leak into logs).
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import ValidationError, field_validator, model_validator
from pydantic_core import PydanticCustomError  # re-export home in pydantic v2
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root as seen from a normal checkout: service/app/core/env.py -> up 3.
# Overridable via REPO_ROOT for packaging layouts that move the data dir.
REPO_ROOT: Path = Path(__file__).resolve().parents[3]

# Default layout below the repo root (plan §11 `./data/`; §5 `config/`).
# Single source of truth for both the field defaults (documentation/mypy) and
# the before-validator that actually fills them (review finding #4).
DEFAULT_DATA_DIRNAME = "data"
DEFAULT_CONFIG_DIRNAME = "config"
DEFAULT_DATABASE_FILENAME = "vanilla.db"

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
        # Consumer: config-check's §8.11 open-registration listen gate
        # (T1.6). Deployment declares its bind address here (systemd unit,
        # T13.6, exports it next to uvicorn's --host) so the check can be
        # deterministic instead of guessing.
        "LISTEN_ADDR",
    }
)


class EnvError(ValueError):
    """Fail-fast error for the deployment environment.

    Carries one human-readable line per invalid/missing variable. Variable
    values are never included.
    """


def _env_error(msg: str) -> PydanticCustomError:
    """Model-level validation error carrying an exact, prefixed message."""
    return PydanticCustomError("deployment_env", "{msg}", {"msg": msg})


def _safe_expanduser(raw: str | Path, name: str) -> Path:
    """`Path.expanduser` that reports an unresolvable HOME as a validation
    error (raising RuntimeError out of load_env() would break its contract —
    e.g. systemd units without User= have no HOME)."""
    try:
        return Path(raw).expanduser()
    except RuntimeError as exc:
        raise _env_error(f"{name}: cannot resolve '~' ({exc})") from None


def _expand(raw: str | Path, root: Path, name: str) -> Path:
    """Expand `~` in *raw*, then anchor relative results at *root*.

    Rejects present-but-blank values: an empty deployment path is a typo in
    `.env`, not a request for the default (that is what unsetting is).
    """
    if not str(raw).strip():
        raise _env_error(f"{name} must not be empty")
    p = _safe_expanduser(raw, name)
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
    # type-checkers accept the no-arg constructor that env-magic performs, and
    # they are built from the same DEFAULT_* constants the validator uses.
    repo_root: Path = REPO_ROOT
    data_dir: Path = Path(DEFAULT_DATA_DIRNAME)
    config_dir: Path = Path(DEFAULT_CONFIG_DIRNAME)
    database_path: Path = Path(DEFAULT_DATA_DIRNAME) / DEFAULT_DATABASE_FILENAME

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

    # --- listen surface (consumer: T1.6 config-check §8.11 gate) ---------------
    # Absent = "not declared": config-check warns (open registration is only
    # safe behind loopback or an explicit declaration). A present-but-blank
    # value is a typo, rejected like every other path/flag here.
    listen_addr: str | None = None

    @field_validator("listen_addr")
    @classmethod
    def _listen_addr_non_blank(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise _env_error(
                "LISTEN_ADDR must not be empty (unset it to skip the listen check)"
            )
        return v

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
        if "repo_root" in values:
            raw_root = values["repo_root"]
            # Check the raw string: Path("") collapses to Path(".") and would
            # otherwise be misreported as "relative" instead of "empty".
            if not str(raw_root).strip():
                raise _env_error("REPO_ROOT must not be empty")
            root = _safe_expanduser(raw_root, "REPO_ROOT")
            if not root.is_absolute():
                # It anchors every other relative path; anchoring it at CWD
                # would silently move the whole deployment with the launch dir.
                raise _env_error(
                    f"REPO_ROOT must be an absolute path (got relative {str(root)!r})"
                )
        else:
            root = REPO_ROOT
        values["repo_root"] = root
        values["data_dir"] = _expand(
            values.get("data_dir", DEFAULT_DATA_DIRNAME), root, "DATA_DIR"
        )
        values["config_dir"] = _expand(
            values.get("config_dir", DEFAULT_CONFIG_DIRNAME), root, "CONFIG_DIR"
        )
        db = values.get("database_path")
        values["database_path"] = (
            _expand(db, root, "DATABASE_PATH")
            if db is not None
            else values["data_dir"] / DEFAULT_DATABASE_FILENAME
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


class _Auto:
    """Sentinel: resolve the dotenv location at call time (see load_env)."""


AUTO_DOTENV: Any = _Auto()


def read_dotenv(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE audit view of a dotenv file (stdlib only).

    Used by config-check (T1.6) to detect near-miss variable names — a
    *file-shape audit*, not the loader of truth: the real environment is
    built by pydantic-settings from the same file. Unreadable file returns
    ``{}`` (absence is the normal repo state); malformed lines are skipped;
    values are best-effort raw text (only key names and blankness matter
    downstream). Never print the returned values: they may be secrets.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].lstrip()
        key, sep, value = stripped.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        out[key] = value.strip().strip("\"'")
    return out


def default_dotenv_path() -> Path:
    """The `.env` lives next to the *runtime* REPO_ROOT, not the import-time one.

    Resolution is best-effort by design: an unreadable/odd REPO_ROOT yields no
    dotenv, and the real diagnosis comes from field validation, not here.
    """
    raw = os.environ.get("REPO_ROOT", "")
    if raw.strip():
        try:
            root = Path(raw).expanduser()
        except RuntimeError:
            return REPO_ROOT / ".env"
        if root.is_absolute():
            return root / ".env"
    return REPO_ROOT / ".env"


@lru_cache(maxsize=1)
def load_env(*, dotenv_path: Path | None | _Auto = AUTO_DOTENV) -> EnvSettings:
    """Build :class:`EnvSettings` from the process environment, fail-fast.

    All problems are reported at once (pydantic collects them), matching the
    collect-don't-fail-on-first style used across the config layer (T1.2).
    Args:
        dotenv_path: ``AUTO_DOTENV`` (default) reads ``<runtime REPO_ROOT>/.env``;
            ``None`` disables the dotenv source (tests); a ``Path`` pins it.
    Raises:
        EnvError: one line per problem; never contains variable values.
    """
    resolved = default_dotenv_path() if isinstance(dotenv_path, _Auto) else dotenv_path
    try:
        # openai_api_key is env-provided at runtime; mypy cannot see the
        # pydantic-settings source, so it flags the no-data call. Keeping the
        # field required (rather than defaulting it) is the fail-fast itself.
        return EnvSettings(_env_file=resolved)  # type: ignore[call-arg]
    except ValidationError as exc:
        lines = []
        for err in exc.errors():
            loc = ".".join(str(part) for part in err["loc"])
            # Model-level errors (path anchoring) carry the variable name in
            # their message already; field errors get the NAME: prefix.
            lines.append(f"{loc.upper()}: {err['msg']}" if loc else err["msg"])
        raise EnvError(
            "invalid deployment environment:\n  " + "\n  ".join(lines)
        ) from None
