"""Unit tests for app.config.snapshot (docs/plan.md T1.3).

The plan's verification column, taken literally:

- 两个 run 各持自己快照 — a held reference keeps the OLD snapshot (content,
  version, identity) across a successful reload that changes the store's
  current one (invariant #6, main-loop contract);
- swap 失败保留旧快照 — failed reloads (load errors, cross-file validation
  errors, version-guard violations) leave ``store.current`` untouched;
- version 单调 — versions of INSTALLED snapshots are strictly increasing and
  gapless; rejected candidates consume nothing.

Plus the §4 contract details: ``load_config`` refuses with the full
diagnosis, ``validate()`` collects the two cross-file mode conflicts without
raising, ``sources`` sha256 rides into the snapshot, ``loaded_at`` comes from
the injectable clock, and everything is deep-immutable.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config.issue import ConfigIssue as SharedConfigIssue
from app.config.schema import (
    HookConfig,
    MCPConfig,
    PermissionEffect,
    PermissionMode,
    PermissionRule,
    PermissionsConfig,
    PluginsConfig,
    Settings,
    ToolConfig,
)
from app.config.snapshot import (
    STORE_SOURCE,
    ConfigIssue,
    ConfigLoadError,
    ConfigSnapshot,
    ConfigStore,
    load_config,
)
from app.core.clock import FakeClock

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "configs"

FAKE_START = datetime(2026, 11, 23, 9, 0, 0, tzinfo=UTC)


def settings_body(model_name: str = "vanilla-mini") -> str:
    return f"""
provider:
  base_url: https://llm.example.com/v1
  api_key: sk-test-not-a-real-key
  model_name: {model_name}
"""


def make_tree(tmp_path: Path, files: dict[str, str]) -> Path:
    """Standard layout: repo root = tmp_path, config dir = tmp/config."""
    config = tmp_path / "config"
    config.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (config / name).write_text(body.lstrip("\n"), encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# load_config: assembly, refusal, clock
# ---------------------------------------------------------------------------


async def test_load_config_assembles_defaults_sources_and_clock(tmp_path: Path) -> None:
    repo = make_tree(tmp_path, {"settings.yaml": settings_body()})
    clock = FakeClock(FAKE_START)
    with clock.install():
        snap = await load_config(repo_root=repo, environ={})
    assert snap.version == 1
    assert snap.loaded_at == clock.now()  # exact: the injected clock speaks
    # The five optional files are absent: warnings survive, defaults apply.
    assert len(snap.issues) == 5
    assert all(i.level == "warning" for i in snap.issues)
    assert snap.tools.defaults.timeout_s == 60.0
    assert snap.plugins.plugins == []
    # sources carry the real sha256 of what was read.
    assert [name for name, _ in snap.sources] == ["config/settings.yaml"]
    raw = (repo / "config" / "settings.yaml").read_bytes()
    assert snap.sources[0][1] == hashlib.sha256(raw).hexdigest()
    # validate() is clean on an assembled-and-consistent snapshot.
    assert snap.validate() == []


async def test_load_config_honors_explicit_config_dir(tmp_path: Path) -> None:
    # CONFIG_DIR relocated *inside* the repo: repo-relative display works,
    # and the default <repo>/config layout is not consulted.
    config = tmp_path / "elsewhere"
    config.mkdir()
    (config / "settings.yaml").write_text(settings_body(), encoding="utf-8")
    snap = await load_config(repo_root=tmp_path, config_dir=config, environ={})
    assert snap.sources[0][0] == "elsewhere/settings.yaml"
    # CONFIG_DIR disjoint from REPO_ROOT: display falls back to
    # config-relative naming, never an absolute path.
    far = await load_config(
        repo_root=tmp_path / "nonrepo", config_dir=config, environ={}
    )
    assert far.sources[0][0] == "settings.yaml"


async def test_load_config_refuses_with_full_diagnosis(tmp_path: Path) -> None:
    repo = make_tree(tmp_path, {"settings.yaml": "provider: [unclosed\n"})
    with pytest.raises(ConfigLoadError) as excinfo:
        await load_config(repo_root=repo, environ={})
    assert any(i.level == "error" for i in excinfo.value.issues)
    assert "config load refused" in str(excinfo.value)


async def test_load_config_refuses_cross_file_mode_conflicts(tmp_path: Path) -> None:
    # permissions.yaml default outside settings.yaml's allowed set.
    repo = make_tree(
        tmp_path,
        {
            "settings.yaml": """
            provider:
              base_url: https://llm.example.com/v1
              api_key: sk-test-not-a-real-key
              model_name: m
            permissions:
              default_mode: default
              allowed_modes: [default, plan]
        """,
            "permissions.yaml": "default_mode: bypass\n",
        },
    )
    with pytest.raises(ConfigLoadError) as excinfo:
        await load_config(repo_root=repo, environ={})
    blocking = [i for i in excinfo.value.issues if i.level == "error"]
    assert len(blocking) == 1
    assert blocking[0].path == "default_mode"
    assert "allowed_modes" in blocking[0].message
    # The blocking diagnosis rode in the exception — not just warnings
    # (regression: early versions dropped validate errors here).
    assert any("default mode" in i.message for i in excinfo.value.issues)


async def test_disallow_bypass_conflicts_with_allowed_modes(tmp_path: Path) -> None:
    repo = make_tree(
        tmp_path,
        {
            "settings.yaml": """
            provider:
              base_url: https://llm.example.com/v1
              api_key: sk-test-not-a-real-key
              model_name: m
            permissions:
              default_mode: plan
              allowed_modes: [default, plan, acceptEdits, bypass]
        """,
            "permissions.yaml": "policy:\n  disallow_bypass: true\n",
        },
    )
    with pytest.raises(ConfigLoadError) as excinfo:
        await load_config(repo_root=repo, environ={})
    blocking = [i for i in excinfo.value.issues if i.level == "error"]
    assert len(blocking) == 1
    assert blocking[0].source == "config/settings.yaml"
    assert blocking[0].path == "permissions.allowed_modes"


async def test_source_for_points_at_recorded_display_names(tmp_path: Path) -> None:
    repo = make_tree(
        tmp_path,
        {
            "settings.yaml": settings_body(),
            "permissions.yaml": "default_mode: plan\n",
        },
    )
    snap = await load_config(repo_root=repo, environ={})
    assert snap.source_for("permissions.yaml") == "config/permissions.yaml"
    # Absent file: fallback naming (it was assembled from defaults).
    assert snap.source_for("hooks.yaml") == "config/hooks.yaml"


async def test_committed_valid_fixture_still_assembles_cleanly() -> None:
    root = FIXTURES / "valid-full"
    clock = FakeClock(FAKE_START)
    with clock.install():
        snap = await load_config(
            repo_root=root,
            config_dir=root,
            environ={"VA_T12_PROVIDER_KEY": "sk-v", "VA_T12_MCP_TOKEN": "t"},
        )
    assert snap.issues == ()
    assert snap.validate() == []
    # The fixture pairs policy.disallow_bypass=true with an allowed_modes
    # list that omits bypass — the cross-file check passes iff both sides
    # of the contradiction agree to disagree NOT.
    assert snap.permissions.policy.disallow_bypass is True
    assert PermissionMode.BYPASS not in snap.settings.permissions.allowed_modes


# ---------------------------------------------------------------------------
# ConfigStore: bootstrap
# ---------------------------------------------------------------------------


async def test_bootstrap_raises_on_broken_config_and_serves_on_good(
    tmp_path: Path,
) -> None:
    repo = make_tree(tmp_path, {"settings.yaml": "provider:\n  x: 1\n"})
    with pytest.raises(ConfigLoadError):
        await ConfigStore.bootstrap(repo_root=repo, environ={})
    (repo / "config" / "settings.yaml").write_text(settings_body(), encoding="utf-8")
    store = await ConfigStore.bootstrap(repo_root=repo, environ={})
    assert store.current.version == 1


def _bare_snapshot(version: int) -> ConfigSnapshot:
    """A minimal assembled snapshot for store-level unit tests."""
    return ConfigSnapshot(
        version=version,
        loaded_at=FAKE_START,
        sources=(),
        settings=Settings.model_validate(
            {
                "provider": {
                    "base_url": "https://llm.example.com/v1",
                    "api_key": "sk-test-not-a-real-key",
                    "model_name": "m",
                }
            }
        ),
        tools=ToolConfig(),
        permissions=PermissionsConfig(),
        hooks=HookConfig(),
        mcp=MCPConfig(),
        plugins=PluginsConfig(),
    )


def test_store_requires_positive_initial_version() -> None:
    with pytest.raises(ValueError):
        ConfigStore(_bare_snapshot(version=0))
    assert ConfigStore(_bare_snapshot(version=1)).current.version == 1


# ---------------------------------------------------------------------------
# Reload semantics: the three headline guarantees
# ---------------------------------------------------------------------------


async def test_held_snapshot_survives_reload_untorn(tmp_path: Path) -> None:
    """The plan's first case: two 'runs' each keep their own generation."""
    repo = make_tree(tmp_path, {"settings.yaml": settings_body("one")})
    store = await ConfigStore.bootstrap(repo_root=repo, environ={})

    run_a_config = store.current  # what a started run holds forever

    (repo / "config" / "settings.yaml").write_text(
        settings_body("two"), encoding="utf-8"
    )
    report = await store.reload(repo_root=repo, environ={})

    assert report.ok and report.version == 2
    # The new generation is live...
    assert store.current.settings.provider.model_name == "two"
    assert store.current.version == 2
    # ...and run A's view is byte-for-byte the old one: a distinct object,
    # old content intact — no tearing, no version aliasing.
    assert run_a_config is not store.current
    assert run_a_config.version == 1
    assert run_a_config.settings.provider.model_name == "one"
    # A later "run B" reads the fresh generation.
    run_b_config = store.current
    assert run_b_config.settings.provider.model_name == "two"


async def test_failed_reload_keeps_old_and_consumes_no_version(tmp_path: Path) -> None:
    repo = make_tree(tmp_path, {"settings.yaml": settings_body("good")})
    store = await ConfigStore.bootstrap(repo_root=repo, environ={})
    original = store.current

    # 1) syntax error 2) cross-file contradiction 3) env var gone — all fail.
    (repo / "config" / "settings.yaml").write_text("provider: [\n", encoding="utf-8")
    r1 = await store.reload(repo_root=repo, environ={})
    (repo / "config" / "settings.yaml").write_text(settings_body(), encoding="utf-8")
    # acceptEdits IS in the default allowed set — but bypass is not
    # (T1.1's D8-posture default), so this contradicts settings.yaml.
    (repo / "config" / "permissions.yaml").write_text(
        "default_mode: bypass\n", encoding="utf-8"
    )
    r2 = await store.reload(repo_root=repo, environ={})
    (repo / "config" / "permissions.yaml").unlink()
    (repo / "config" / "settings.yaml").write_text(
        settings_body().replace("sk-test-not-a-real-key", "${env:GONE}"),
        encoding="utf-8",
    )
    r3 = await store.reload(repo_root=repo, environ={})

    assert not r1.ok and not r2.ok and not r3.ok
    assert any("syntax" in i.message for i in r1.issues)
    assert any(i.path == "default_mode" and i.level == "error" for i in r2.issues)
    assert any("GONE is not set" in i.message for i in r3.issues)
    # Old snapshot still authoritative, same object identity.
    assert store.current is original
    # Failed generations consumed no numbers: the next success is version 2.
    (repo / "config" / "settings.yaml").write_text(
        settings_body("v2"), encoding="utf-8"
    )
    r4 = await store.reload(repo_root=repo, environ={})
    assert r4.ok and r4.version == 2 and r4.previous_version == 1
    assert store.current.settings.provider.model_name == "v2"


async def test_version_sequence_is_monotonic_and_gapless(tmp_path: Path) -> None:
    repo = make_tree(tmp_path, {"settings.yaml": settings_body("base")})
    store = await ConfigStore.bootstrap(repo_root=repo, environ={})
    seen = [store.current.version]
    for n in range(4):
        (repo / "config" / "settings.yaml").write_text(
            settings_body(f"model-{n}"), encoding="utf-8"
        )
        if n == 1:  # poison one reload: it must not burn a version
            (repo / "config" / "settings.yaml").write_text(
                "oops: [\n", encoding="utf-8"
            )
        report = await store.reload(repo_root=repo, environ={})
        if report.ok:
            assert report.version is not None
            seen.append(report.version)
    assert seen == [1, 2, 3, 4]  # 3 successes, strictly +1 each


async def test_swap_version_guard_rejects_stale_or_skipped(tmp_path: Path) -> None:
    repo = make_tree(tmp_path, {"settings.yaml": settings_body("one")})
    store = await ConfigStore.bootstrap(repo_root=repo, environ={})
    current = store.current

    same = dataclasses.replace(current, version=1)
    skipped = dataclasses.replace(current, version=99)
    report_same = store.swap(same)
    assert not report_same.ok
    assert report_same.issues[-1].source == STORE_SOURCE
    assert "exactly by one" in report_same.issues[-1].message
    assert store.current is current

    assert not store.swap(skipped).ok  # gaps are not grown by direct swaps
    ok = store.swap(dataclasses.replace(current, version=2))
    assert ok.ok and ok.version == 2 and store.current.version == 2


async def test_concurrent_reloads_serialize_without_tearing(tmp_path: Path) -> None:
    repo = make_tree(tmp_path, {"settings.yaml": settings_body("one")})
    store = await ConfigStore.bootstrap(repo_root=repo, environ={})
    reports = await asyncio.gather(
        *(store.reload(repo_root=repo, environ={}) for _ in range(5))
    )
    assert all(r.ok and r.version is not None for r in reports)
    versions = sorted(r.version for r in reports if r.version is not None)
    assert versions == [2, 3, 4, 5, 6]  # every reload advanced by exactly 1
    assert store.current.version == 6


async def test_reload_success_report_keeps_load_warnings(tmp_path: Path) -> None:
    repo = make_tree(tmp_path, {"settings.yaml": settings_body("one")})
    store = await ConfigStore.bootstrap(repo_root=repo, environ={})
    report = await store.reload(repo_root=repo, environ={})
    assert report.ok
    # All five optional files are still absent: the report must not present
    # a green reload as a silent one (SIGHUP logs read this).
    assert len([i for i in report.issues if i.level == "warning"]) == 5


# ---------------------------------------------------------------------------
# Immortality of structure & module contracts
# ---------------------------------------------------------------------------


async def test_swap_rejects_cross_file_conflict_without_installing(
    tmp_path: Path,
) -> None:
    """swap() is load_config's second gate: a candidate assembled elsewhere
    (T1.4's merge path) that contradicts itself must be refused without
    touching the store — the whole point of validate-at-swap."""
    repo = make_tree(tmp_path, {"settings.yaml": settings_body("one")})
    store = await ConfigStore.bootstrap(repo_root=repo, environ={})
    original = store.current
    conflicting = dataclasses.replace(
        original,
        version=2,
        permissions=store.current.permissions.model_copy(
            update={"default_mode": PermissionMode.BYPASS}
        ),
    )
    report = store.swap(conflicting)
    assert not report.ok
    assert any(i.path == "default_mode" and i.level == "error" for i in report.issues)
    assert store.current is original  # nothing installed, no version burned
    # A clean reload still works (installs version 2)...
    ok_report = await store.reload(repo_root=repo, environ={})
    assert ok_report.ok and ok_report.version == 2
    # ...and the rejected candidate now fails on BOTH counts: its content
    # conflicts and its version is stale.
    assert not store.swap(conflicting).ok
    repo = make_tree(tmp_path, {"settings.yaml": settings_body("one")})
    snap = await load_config(repo_root=repo, environ={})
    with pytest.raises(FrozenInstanceError):
        snap.version = 2  # type: ignore[misc]
    with pytest.raises(ValidationError):
        snap.settings.provider.model_name = "hacked"  # type: ignore[misc]
    # issues is a tuple: no in-place append even through the container.
    with pytest.raises(AttributeError):
        snap.issues.append(ConfigIssue.error("x", "", "y"))  # type: ignore[attr-defined]


async def test_config_issue_reexport_is_the_same_class() -> None:
    # §4 lists ConfigIssue under this module; the class object must be the
    # single shared one, not a copy (isinstance identity across layers).
    assert ConfigIssue is SharedConfigIssue


def test_assemble_defensive_on_missing_model() -> None:
    from app.config.loader import LoadedConfig, OverlayBundle
    from app.config.snapshot import assemble_snapshot

    broken = LoadedConfig(settings=None)  # error-free by construction...
    empty_overlays = OverlayBundle(overrides={}, issues=(), sources=())
    with pytest.raises(RuntimeError):
        assemble_snapshot(
            broken,
            empty_overlays,
            contributions=(),
            repo_root=Path("."),
            config_dir=Path("."),
            version=1,
        )


def test_snapshot_origins_alignment_is_structural() -> None:
    # A misaligned construction path (here: hand-built provenance) is a
    # programming error, refused at __post_init__, not a silent drift.
    snap = _bare_snapshot(1)
    with pytest.raises(ValueError, match="align 1:1"):
        dataclasses.replace(
            snap,
            permissions=snap.permissions.model_copy(
                update={
                    "rules": [
                        PermissionRule(effect=PermissionEffect.ASK, pattern="bash")
                    ]
                }
            ),
        )
