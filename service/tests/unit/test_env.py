"""Unit tests for app.core.env (docs/plan.md T0.2).

Coverage per the plan's verification column: missing key errors, defaults,
path expansion, dev-only switch recognition — plus whitelist guards that pin
"deployment env only, capabilities belong in config/*.yaml".
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.env import (
    DEFAULT_SANDBOX_IMAGE,
    DEFAULT_SESSION_TTL_DAYS,
    DEPLOY_ENV_VARS,
    REPO_ROOT,
    EnvError,
    EnvSettings,
    load_env,
)

VALID_KEY = "sk-test-not-a-real-key"


def load_fresh() -> EnvSettings:
    """Re-read through `load_env`, which is `lru_cache`d process-wide.

    Failed loads are not memoized (lru_cache only caches successes), so every
    test that changes the environment and expects *fresh* values — including
    second-phase assertions inside a single test — must go through here.
    """
    load_env.cache_clear()
    return load_env()


@pytest.fixture(autouse=True)
def _isolated_environ(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Strip every deployment var and disable the repo-root `.env`.

    Without this, the developer's machine environment (or an untracked
    `.env`) would leak into the expectations below. The cache is cleared on
    both sides so a success cached by one test can never answer for another.
    """
    for name in DEPLOY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(EnvSettings.model_config, "env_file", None)
    load_env.cache_clear()
    yield
    load_env.cache_clear()


@pytest.fixture
def setenv(monkeypatch: pytest.MonkeyPatch):
    def _set(**vars: str) -> None:
        for k, v in vars.items():
            monkeypatch.setenv(k, v)

    return _set


# --- cache semantics (lru_cache on load_env) -------------------------------------


def test_load_env_is_a_process_singleton(setenv) -> None:
    """Intended semantics: the deployment env is read once per process.

    Mutating the environment at runtime is not a supported reload path —
    SIGHUP swaps ConfigSnapshots (T1.3), never EnvSettings — so every caller
    must receive the same cached instance.
    """
    setenv(OPENAI_API_KEY=VALID_KEY)
    first = load_env()
    setenv(SESSION_TTL_DAYS="7")
    assert load_env() is first  # memoized: later env edits are invisible
    assert load_fresh().session_ttl_days == 7  # an explicit clear re-reads


def test_settings_instance_is_immutable(setenv) -> None:
    """The cached EnvSettings is shared process-wide, so it is frozen."""
    setenv(OPENAI_API_KEY=VALID_KEY)
    env = load_env()
    with pytest.raises(ValidationError):
        env.session_ttl_days = 1


def test_failed_loads_are_not_cached(setenv) -> None:
    """lru_cache memoizes successes only: fixing the env recovers without a clear."""
    with pytest.raises(EnvError):
        load_env()
    setenv(OPENAI_API_KEY=VALID_KEY)
    assert load_env().openai_api_key == VALID_KEY


# --- fail-fast on missing / invalid key --------------------------------------


def test_missing_api_key_fails_fast() -> None:
    with pytest.raises(EnvError) as excinfo:
        load_fresh()
    assert "OPENAI_API_KEY" in str(excinfo.value)


def test_blank_api_key_rejected(setenv) -> None:
    setenv(OPENAI_API_KEY="   ")
    with pytest.raises(EnvError, match="OPENAI_API_KEY"):
        load_fresh()


def test_all_problems_reported_at_once(setenv) -> None:
    setenv(SESSION_TTL_DAYS="0", SANDBOX_IMAGE=" ", OPENAI_API_KEY="")
    with pytest.raises(EnvError) as excinfo:
        load_fresh()
    msg = str(excinfo.value)
    assert "SESSION_TTL_DAYS" in msg
    assert "SANDBOX_IMAGE" in msg
    assert "OPENAI_API_KEY" in msg


def test_error_message_never_echoes_values(setenv) -> None:
    setenv(OPENAI_API_KEY=VALID_KEY, SESSION_TTL_DAYS="0")
    with pytest.raises(EnvError) as excinfo:
        load_fresh()
    assert VALID_KEY not in str(excinfo.value)


# --- defaults ------------------------------------------------------------------


def test_defaults_with_only_the_key_set(setenv) -> None:
    setenv(OPENAI_API_KEY=VALID_KEY)
    env = load_fresh()
    assert env.openai_api_key == VALID_KEY
    assert env.repo_root == REPO_ROOT
    assert env.data_dir == REPO_ROOT / "data"
    assert env.config_dir == REPO_ROOT / "config"
    assert env.database_path == REPO_ROOT / "data" / "vanilla.db"
    assert env.session_ttl_days == DEFAULT_SESSION_TTL_DAYS
    assert env.session_cookie_secure is True
    assert env.allow_local_sandbox is False
    assert env.config_allow_local is False
    assert env.sandbox_image == DEFAULT_SANDBOX_IMAGE


def test_cookie_secure_defaults_to_true_for_production_safety(setenv) -> None:
    setenv(OPENAI_API_KEY=VALID_KEY)
    assert load_fresh().session_cookie_secure is True
    setenv(SESSION_COOKIE_SECURE="0")
    assert load_fresh().session_cookie_secure is False


# --- path expansion ---------------------------------------------------------------


def test_relative_paths_anchor_at_repo_root(setenv, tmp_path: Path) -> None:
    setenv(
        OPENAI_API_KEY=VALID_KEY,
        REPO_ROOT=str(tmp_path),
        DATA_DIR="mydata",
        CONFIG_DIR="myconfig",
        DATABASE_PATH="my.db",
    )
    env = load_fresh()
    assert env.repo_root == tmp_path
    assert env.data_dir == tmp_path / "mydata"
    assert env.config_dir == tmp_path / "myconfig"
    assert env.database_path == tmp_path / "my.db"


def test_tilde_is_expanded(setenv) -> None:
    setenv(OPENAI_API_KEY=VALID_KEY, DATA_DIR="~/vanilla-data")
    env = load_fresh()
    assert env.data_dir == Path.home() / "vanilla-data"


def test_absolute_paths_are_kept(setenv, tmp_path: Path) -> None:
    db = tmp_path / "elsewhere" / "vanilla.db"
    setenv(OPENAI_API_KEY=VALID_KEY, DATABASE_PATH=str(db))
    assert load_fresh().database_path == db


def test_database_path_follows_data_dir_override(setenv, tmp_path: Path) -> None:
    setenv(OPENAI_API_KEY=VALID_KEY, DATA_DIR=str(tmp_path / "d"))
    env = load_fresh()
    assert env.database_path == tmp_path / "d" / "vanilla.db"


# --- dev-only switches ----------------------------------------------------------


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on"])
def test_dev_switches_recognized_when_enabled(setenv, raw: str) -> None:
    setenv(OPENAI_API_KEY=VALID_KEY, ALLOW_LOCAL_SANDBOX=raw, CONFIG_ALLOW_LOCAL=raw)
    env = load_fresh()
    assert env.allow_local_sandbox is True
    assert env.config_allow_local is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off"])
def test_dev_switches_recognized_when_disabled(setenv, raw: str) -> None:
    setenv(OPENAI_API_KEY=VALID_KEY, ALLOW_LOCAL_SANDBOX=raw, CONFIG_ALLOW_LOCAL=raw)
    env = load_fresh()
    assert env.allow_local_sandbox is False
    assert env.config_allow_local is False


def test_dev_switch_rejects_garbage(setenv) -> None:
    setenv(OPENAI_API_KEY=VALID_KEY, ALLOW_LOCAL_SANDBOX="maybe")
    with pytest.raises(EnvError, match="ALLOW_LOCAL_SANDBOX"):
        load_fresh()


# --- other constraints ------------------------------------------------------------


def test_session_ttl_bounds(setenv) -> None:
    setenv(OPENAI_API_KEY=VALID_KEY, SESSION_TTL_DAYS="365")
    assert load_fresh().session_ttl_days == 365
    setenv(SESSION_TTL_DAYS="366")
    with pytest.raises(EnvError, match="SESSION_TTL_DAYS"):
        load_fresh()
    setenv(SESSION_TTL_DAYS="0")
    with pytest.raises(EnvError, match="SESSION_TTL_DAYS"):
        load_fresh()


def test_sandbox_image_override(setenv) -> None:
    setenv(OPENAI_API_KEY=VALID_KEY, SANDBOX_IMAGE="example.invalid/sandbox:dev")
    assert load_fresh().sandbox_image == "example.invalid/sandbox:dev"


# --- surface guards (pin the T0.2 boundary) ----------------------------------------


def test_env_surface_is_exactly_the_deployment_vars() -> None:
    """The env surface is a closed whitelist (plan T0.2, ADR-015).

    If this test fails, someone either added a deployment variable (update
    DEPLOY_ENV_VARS and §11) — or moved a capability knob into env, which
    belongs in config/*.yaml instead.
    """
    assert {name.upper() for name in EnvSettings.model_fields} == DEPLOY_ENV_VARS


def test_no_session_secret_signing_key_exists() -> None:
    """§11: sessions are DB-lookup tokens; a signing secret would contradict ADR-004."""
    assert "session_secret" not in EnvSettings.model_fields
    assert "SECRET_MASTER_KEY" not in DEPLOY_ENV_VARS


def test_capability_params_are_not_env_tunable() -> None:
    """§13-D2/ADR-015: models/limits/etc. come from config/*.yaml, never env."""
    banned = {
        "model_name",
        "base_url",
        "max_turns",
        "temperature",
        "network_mode",
        "max_live_containers",
        "daily_token_budget",
        "global_hourly_tokens",
    }
    assert not banned & set(EnvSettings.model_fields)


def test_unrelated_env_vars_are_ignored(setenv) -> None:
    """SECRET_* etc. stay untouched here — they are ${env:} refs for T1.2."""
    setenv(OPENAI_API_KEY=VALID_KEY, SECRET_SOMETHING="x", PATH_EXTRA="y")
    assert load_fresh().openai_api_key == VALID_KEY
