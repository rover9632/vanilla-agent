"""Trust-boundary enforcement tests (docs/plan.md T1.7; §8.7).

Plan column: "用 os.chmod/tmpdir 构造可写目录 → 拒绝；dev 开关下仅告警" —
plus the hook-script rules (§8.7: existing, executable, non-world-writable,
no shell metacharacters) and the bootstrap-vs-reload wiring ("启动与 reload
都执行"). The service uid is injected by monkeypatching
``app.config.trust._current_uid`` — the production probe point — so both
"service owns the tree" (deploy sin) and "service is a different account"
(deploy posture) are exercised deterministically on tmp trees.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.config.check import run_config_check
from app.config.loader import load_config_files, load_local_overlays
from app.config.snapshot import ConfigLoadError, ConfigStore, assemble_snapshot
from app.config.trust import apply_trust, check_config_trust

SETTINGS = """provider:
  base_url: https://llm.example.com/v1
  api_key: plain-value-not-referenced
  model_name: m
"""


def make_config(tmp_path: Path, extra: dict[str, str] | None = None) -> Path:
    config = tmp_path / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "settings.yaml").write_text(SETTINGS, encoding="utf-8")
    for name, body in (extra or {}).items():
        target = config / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return config


def snapshot_for(config: Path, tmp_path: Path):
    loaded = load_config_files(repo_root=tmp_path, config_dir=config, environ={})
    overlays = load_local_overlays(repo_root=tmp_path, config_dir=config, environ={})
    return assemble_snapshot(
        loaded,
        overlays,
        contributions=(),
        repo_root=tmp_path,
        config_dir=config,
        version=1,
    )


# ---------------------------------------------------------------------------
# Detector semantics
# ---------------------------------------------------------------------------


def test_world_writable_directory_and_file_are_flagged(tmp_path: Path) -> None:
    config = make_config(tmp_path, {"tools.yaml": "tools: {}\n"})
    assert check_config_trust(config, service_uid=-1) == []
    os.chmod(config, 0o777)
    findings = check_config_trust(config, service_uid=-1)
    assert any(
        "world-writable" in i.message for i in findings if i.source == str(config)
    )
    os.chmod(config, 0o755)
    os.chmod(config / "tools.yaml", 0o666)
    findings = check_config_trust(config, service_uid=-1)
    assert len(findings) == 1
    assert findings[0].source == str(config / "tools.yaml")
    assert "world-writable" in findings[0].message


def test_service_account_ownership_is_flagged(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    owner_uid = os.stat(config).st_uid
    assert check_config_trust(config, service_uid=owner_uid - 1) == []
    findings = check_config_trust(config, service_uid=owner_uid)
    assert findings  # every entry belongs to "the service" now
    assert all("owned by the service account" in i.message for i in findings)
    # both violation classes must name the deploy remedy, not just die
    assert "read-only" in findings[0].message


def test_missing_config_directory_reports(tmp_path: Path) -> None:
    findings = check_config_trust(tmp_path / "nope", service_uid=-1)
    assert len(findings) == 1
    assert "config directory is missing" in findings[0].message


def test_hook_script_rules(tmp_path: Path) -> None:
    hooks_yaml = """hooks:
  - {event: PreToolUse, matcher: bash, kind: command,
     target: config/hooks/guard.sh}
"""
    config = make_config(tmp_path, {"hooks.yaml": hooks_yaml})
    snap = snapshot_for(config, tmp_path)

    # missing script
    findings = check_config_trust(
        config, hooks=snap.hooks.hooks, repo_root=tmp_path, service_uid=-1
    )
    assert any("does not exist" in i.message for i in findings)

    # present but not executable
    script = config / "hooks" / "guard.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/bin/sh\nexit 0\n")
    os.chmod(script, 0o644)
    findings = check_config_trust(
        config, hooks=snap.hooks.hooks, repo_root=tmp_path, service_uid=-1
    )
    assert any("not executable" in i.message for i in findings)

    # world-writable exec is fatal regardless
    os.chmod(script, 0o777)
    findings = check_config_trust(
        config, hooks=snap.hooks.hooks, repo_root=tmp_path, service_uid=-1
    )
    assert any(
        "hook script" in i.message and "world-writable" in i.message for i in findings
    )

    # exec metacharacters in the declared target: refused even though
    # argv-passing would never interpret them (config-mistake posture)
    sneaky = hooks_yaml.replace(
        "config/hooks/guard.sh", "config/hooks/guard.sh; curl evil"
    )
    config2 = make_config(
        tmp_path / "t2", {"hooks.yaml": sneaky, "settings.yaml": SETTINGS}
    )
    snap2 = snapshot_for(config2, tmp_path / "t2")
    findings = check_config_trust(
        config2, hooks=snap2.hooks.hooks, repo_root=tmp_path / "t2", service_uid=-1
    )
    assert any("shell metacharacters" in i.message for i in findings)


# ---------------------------------------------------------------------------
# apply_trust: severity split (refuse vs loud dev warning)
# ---------------------------------------------------------------------------


def test_apply_trust_refuses_then_dev_mode_warns(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    snap = snapshot_for(config, tmp_path)
    os.chmod(config, 0o777)

    strict = apply_trust(
        snap, config_dir=config, repo_root=tmp_path, allow_local=False, service_uid=-1
    )
    assert strict.errors and strict.warnings == ()

    dev = apply_trust(
        snap, config_dir=config, repo_root=tmp_path, allow_local=True, service_uid=-1
    )
    assert dev.errors == ()
    assert dev.warnings
    assert all(
        i.message.startswith("DEV MODE (CONFIG_ALLOW_LOCAL=1):") for i in dev.warnings
    )


# ---------------------------------------------------------------------------
# Wiring: bootstrap refuses, reload re-probes, dev mode boots loud
# ---------------------------------------------------------------------------


async def test_bootstrap_refuses_world_writable_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(tmp_path)
    monkeypatch.setattr("app.config.trust._current_uid", lambda: -1)
    store = await ConfigStore.bootstrap(
        repo_root=tmp_path, config_dir=config, environ={}
    )
    assert store.current.version == 1  # clean baseline boots
    os.chmod(config, 0o777)
    with pytest.raises(ConfigLoadError) as exc:
        await ConfigStore.bootstrap(repo_root=tmp_path, config_dir=config, environ={})
    assert any("world-writable" in i.message for i in exc.value.issues)


async def test_reload_refuses_after_config_was_opened_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(tmp_path)
    monkeypatch.setattr("app.config.trust._current_uid", lambda: -1)
    store = await ConfigStore.bootstrap(
        repo_root=tmp_path, config_dir=config, environ={}
    )
    original = store.current
    (config / "settings.yaml").write_text(
        SETTINGS.replace("model_name: m", "model_name: v2"), encoding="utf-8"
    )
    os.chmod(config, 0o777)  # attacker posture appearing AFTER boot
    report = await store.reload(repo_root=tmp_path, config_dir=config, environ={})
    assert not report.ok
    assert any("world-writable" in i.message for i in report.issues)
    assert store.current is original  # old snapshot survives, unswapped...
    assert store.current.settings.provider.model_name == "m"
    # ...even though the NEW bytes on disk say v2 — reload refused, not
    # partially applied.
    assert "v2" in (config / "settings.yaml").read_text()


async def test_dev_mode_boots_with_loud_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(tmp_path)
    owner = os.stat(config).st_uid
    monkeypatch.setattr("app.config.trust._current_uid", lambda: owner)
    # Without the dev flag this is exactly the "service owns its
    # capability tree" deploy sin — refused:
    with pytest.raises(ConfigLoadError):
        await ConfigStore.bootstrap(repo_root=tmp_path, config_dir=config, environ={})
    # With it: boots, and the snapshot carries the DEV MODE warnings so
    # dump/logs make the compromise visible (§8.7 "醒目告警").
    store = await ConfigStore.bootstrap(
        repo_root=tmp_path, config_dir=config, environ={}, allow_local=True
    )
    dev_warnings = [i for i in store.current.issues if i.message.startswith("DEV MODE")]
    assert dev_warnings
    assert all(i.level == "warning" for i in dev_warnings)


# ---------------------------------------------------------------------------
# config-check aggregation (audit stays non-blocking, one line)
# ---------------------------------------------------------------------------


def test_config_check_aggregates_trust_as_single_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(
        tmp_path,
        {
            "tools.yaml": "tools: {}\n",
            "permissions.yaml": "",
            "hooks.yaml": "",
            "mcp.yaml": "",
            "plugins.yaml": "",
        },
    )
    monkeypatch.setattr("app.config.trust._current_uid", lambda: os.stat(config).st_uid)
    issues, snapshot = run_config_check(
        repo_root=tmp_path, environ={"OPENAI_API_KEY": "x"}, listen_addr="127.0.0.1"
    )
    assert snapshot is not None  # audit never blocks
    trust = [i for i in issues if i.source == "<trust>"]
    assert len(trust) == 1
    assert trust[0].level == "warning"
    assert (
        "startup/reload with CONFIG_ALLOW_LOCAL unset would REFUSE" in trust[0].message
    )
    assert "owned by the service account" in trust[0].message
    # one line, not a wall: N findings summarized with a sample source
    assert "×" in trust[0].message
