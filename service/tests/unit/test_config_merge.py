"""Unit tests for app.config.merge + local-overlay gating (docs/plan.md T1.4).

The plan's verification column: "4 类冲突各报一条 error；local 覆盖在未开
开关时被忽略" — plus the rest of §4/§5 merge semantics now implemented:

- permission-rule union keeps operator-first order and per-entry origins
  (§4's rule_source: file + ordinal);
- ``*.local.yaml`` REPLACES its document wholesale (origins attribute to the
  local file when it won), gated by ``allow_local``, unknown targets are an
  error, and ignored-because-gated is a warning, never silence;
- contributions only from registered, enabled plugins;
- cross-source name ownership for all collision families (skill slug, tool
  name, MCP server name, hook matcher — and agent names too);
- PluginContribution *structurally* cannot express policy/default_mode —
  the type is §4's "plugins must not declare bypass" enforcement;
- merged snapshots keep origins aligned 1:1 across generations.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.config.issue import ConfigIssue
from app.config.merge import Origin, PluginContribution
from app.config.schema import (
    HookEntry,
    HookEvent,
    HookKind,
    MCPServerConfig,
    MCPTransport,
    PermissionEffect,
    PermissionRule,
)
from app.config.snapshot import (
    ConfigLoadError,
    ConfigSnapshot,
    ConfigStore,
    load_config,
)

FAKE_KEY = "sk-test-not-a-real-key"


@pytest.fixture(autouse=True)
def _service_is_a_different_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """One test here drives ConfigStore.bootstrap/reload — the T1.7 §8.7
    gate compares file ownership with the service uid; tmp trees belong
    to the runner. Enforcement lives in test_config_trust.py."""
    monkeypatch.setattr("app.config.trust._current_uid", lambda: -1)


def settings_body(model_name: str = "base-model") -> str:
    return f"""provider:
  base_url: https://llm.example.com/v1
  api_key: {FAKE_KEY}
  model_name: {model_name}
"""


def rule(pattern: str, effect: str = "ask") -> dict[str, str]:
    return {"effect": effect, "pattern": pattern}


def plugins_body(*names: str, disabled: tuple[str, ...] = ()) -> str:
    lines = ["plugins:"]
    for name in names:
        enabled = "false" if name in disabled else "true"
        lines.append(
            f"  - {{name: {name}, path: config/plugins/{name}, enabled: {enabled}}}"
        )
    return "\n".join(lines) + "\n"


async def load(tmp_path: Path, files: dict[str, str], **kwargs: Any) -> ConfigSnapshot:
    config = tmp_path / "config"
    config.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (config / name).write_text(body, encoding="utf-8")
    return await load_config(
        repo_root=tmp_path, config_dir=config, environ={}, **kwargs
    )


def errors_of(excinfo: pytest.ExceptionInfo[ConfigLoadError]) -> list[ConfigIssue]:
    return [i for i in excinfo.value.issues if i.level == "error"]


def hook(
    matcher: str = "bash",
    event: HookEvent = HookEvent.PRE_TOOL_USE,
    target: str = "config/plugins/alpha/guard.sh",
) -> HookEntry:
    return HookEntry(event=event, kind=HookKind.COMMAND, matcher=matcher, target=target)


def server(name: str) -> MCPServerConfig:
    return MCPServerConfig(name=name, transport=MCPTransport.STDIO, command="mcp-test")


# ---------------------------------------------------------------------------
# A. *.local.yaml gating (plan column: ignored when the switch is off)
# ---------------------------------------------------------------------------


async def test_local_override_ignored_when_gate_disabled(tmp_path: Path) -> None:
    snap = await load(
        tmp_path,
        {
            "settings.yaml": settings_body("base-model"),
            "settings.local.yaml": settings_body("OVERRIDE-MODEL"),
        },
    )  # allow_local defaults to False
    assert snap.settings.provider.model_name == "base-model"
    gate_warnings = [i for i in snap.issues if "CONFIG_ALLOW_LOCAL" in i.message]
    assert len(gate_warnings) == 1  # "ignored" is loud, never silent
    assert gate_warnings[0].source == "config/settings.local.yaml"
    # An ignored overlay is NOT part of the snapshot's composition.
    assert all("local" not in name for name, _ in snap.sources)


async def test_local_override_replaces_when_gate_enabled(tmp_path: Path) -> None:
    snap = await load(
        tmp_path,
        {
            "settings.yaml": settings_body("base-model"),
            "settings.local.yaml": settings_body("OVERRIDE-MODEL"),
        },
        allow_local=True,
    )
    assert snap.settings.provider.model_name == "OVERRIDE-MODEL"
    # sources: base roster first, then overlays — both hashed (the base file
    # was still read; the audit trail shows exactly what composed the win).
    names = [name for name, _ in snap.sources]
    assert names[:1] == ["config/settings.yaml"]
    assert names[-1] == "config/settings.local.yaml"


async def test_local_replacement_is_wholesale_not_additive(tmp_path: Path) -> None:
    snap = await load(
        tmp_path,
        {
            "settings.yaml": settings_body(),
            "permissions.yaml": f"rules:\n  - {rule('read_file', 'allow')}\n"
            f"  - {rule('bash(rm *)', 'deny')}\n",
            "permissions.local.yaml": f"rules:\n  - {rule('grep', 'allow')}\n",
        },
        allow_local=True,
    )
    assert [r.pattern for r in snap.permissions.rules] == ["grep"]
    # Origins attribute to the LOCAL file that authored the winning doc.
    assert snap.rule_origins[0].source == "config/permissions.local.yaml"
    assert snap.rule_origins[0].label == "config/permissions.local.yaml#0"


async def test_local_targeting_no_known_stem_is_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigLoadError) as exc:
        await load(
            tmp_path,
            {
                "settings.yaml": settings_body(),
                "setting.local.yaml": settings_body("typo"),  # near-miss stem
            },
            allow_local=True,
        )
    msgs = [i.message for i in errors_of(exc)]
    assert any("targets no known config file" in m for m in msgs)


async def test_broken_local_file_refuses_and_names_it(tmp_path: Path) -> None:
    with pytest.raises(ConfigLoadError) as exc:
        await load(
            tmp_path,
            {
                "settings.yaml": settings_body(),
                "settings.local.yaml": "provider: [unclosed\n",
            },
            allow_local=True,
        )
    errs = errors_of(exc)
    assert len(errs) == 1
    assert errs[0].source.startswith("config/settings.local.yaml:")


# ---------------------------------------------------------------------------
# B. plugin roster gate
# ---------------------------------------------------------------------------


async def test_contributions_from_unregistered_plugin_refuse(tmp_path: Path) -> None:
    with pytest.raises(ConfigLoadError) as exc:
        await load(
            tmp_path,
            {"settings.yaml": settings_body(), "plugins.yaml": "plugins: []\n"},
            contributions=(PluginContribution(plugin="ghost", skills=("s",)),),
        )
    assert any(
        "ghost" in i.message and "does not register" in i.message
        for i in errors_of(exc)
    )


async def test_disabled_plugin_warns_and_contributes_nothing(tmp_path: Path) -> None:
    snap = await load(
        tmp_path,
        {
            "settings.yaml": settings_body(),
            "plugins.yaml": plugins_body("alpha", disabled=("alpha",)),
        },
        contributions=(
            PluginContribution(
                plugin="alpha",
                permission_rules=(
                    PermissionRule(effect=PermissionEffect.DENY, pattern="bash"),
                ),
                hooks=(hook(),),
            ),
        ),
    )
    assert snap.permissions.rules == []
    assert snap.hooks.hooks == []
    assert any("alpha" in i.message and "disabled" in i.message for i in snap.issues)
    assert all(i.level == "warning" for i in snap.issues)  # non-blocking


# ---------------------------------------------------------------------------
# C. §4's four collision families (plan column: one error each) + agents
# ---------------------------------------------------------------------------


async def _refused(
    tmp_path: Path, *contribs: PluginContribution, **files: str
) -> list[ConfigIssue]:
    tree = {
        "settings.yaml": settings_body(),
        "plugins.yaml": plugins_body("alpha", "beta"),
        **files,
    }
    with pytest.raises(ConfigLoadError) as exc:
        await load(tmp_path, tree, contributions=contribs)
    return errors_of(exc)


async def test_skill_slug_collision_one_error(tmp_path: Path) -> None:
    errs = await _refused(
        tmp_path,
        PluginContribution(plugin="alpha", skills=("pdf-notes",)),
        PluginContribution(plugin="beta", skills=("pdf-notes",)),
    )
    collisions = [i for i in errs if "pdf-notes" in i.message]
    assert len(collisions) == 1
    assert collisions[0].source == "plugin:beta"
    assert "plugin:alpha" in collisions[0].message


async def test_tool_name_collisions_against_builtin_and_plugin(tmp_path: Path) -> None:
    errs = await _refused(
        tmp_path,
        PluginContribution(plugin="alpha", tools=("bash", "widget")),
        PluginContribution(plugin="beta", tools=("widget",)),
    )
    assert len(errs) == 2
    by_name = {i.message.split("'")[1]: i for i in errs}
    assert "builtin" in by_name["bash"].message
    assert "plugin:alpha" in by_name["widget"].message


async def test_mcp_server_name_collision_keeps_first_declarer(tmp_path: Path) -> None:
    files = {
        "mcp.yaml": "servers:\n"
        "  - {name: fs-demo, transport: stdio, "
        "command: operator-bin}\n"
    }
    errs = await _refused(
        tmp_path,
        PluginContribution(plugin="alpha", mcp_servers=(server("fs-demo"),)),
        **files,
    )
    assert len(errs) == 1
    assert errs[0].source == "plugin:alpha"
    assert errs[0].path == "mcp_servers[0].name"


async def test_hook_matcher_owner_ship_between_sources(tmp_path: Path) -> None:
    files = {
        "hooks.yaml": "hooks:\n"
        "  - {event: PreToolUse, matcher: bash, "
        "kind: command, target: config/hooks/guard.sh}\n"
    }
    errs = await _refused(
        tmp_path,
        PluginContribution(plugin="alpha", hooks=(hook(matcher="bash"),)),
        **files,
    )
    assert len(errs) == 1
    assert "already declared by 'config/hooks.yaml'" in errs[0].message


async def test_repeats_within_one_source_are_legitimate(tmp_path: Path) -> None:
    # Two guards on the same (event, matcher) in ONE operator document are a
    # normal pattern (T12.1 merges multiple hooks); ownership is per-source.
    snap = await load(
        tmp_path,
        {
            "settings.yaml": settings_body(),
            "hooks.yaml": "hooks:\n"
            "  - {event: PreToolUse, matcher: bash, kind: command, "
            "target: config/hooks/a.sh}\n"
            "  - {event: PreToolUse, matcher: bash, kind: command, "
            "target: config/hooks/b.sh}\n",
        },
    )
    assert len(snap.hooks.hooks) == 2
    assert snap.hook_origins[0].source == snap.hook_origins[1].source
    assert [o.index for o in snap.hook_origins] == [0, 1]


async def test_agent_name_collision(tmp_path: Path) -> None:
    errs = await _refused(
        tmp_path,
        PluginContribution(plugin="alpha", agents=("reviewer",)),
        PluginContribution(plugin="beta", agents=("reviewer",)),
    )
    assert len(errs) == 1
    assert "reviewer" in errs[0].message


# ---------------------------------------------------------------------------
# D. §4 enforcement by construction + containment + provenance
# ---------------------------------------------------------------------------


def test_plugin_contribution_cannot_express_policy_or_default_mode() -> None:
    # "插件不得声明 bypass、不得覆写 policy 段" is enforced by the type's
    # field set itself — there is nowhere to put it, review can't miss it.
    assert set(PluginContribution.__dataclass_fields__) == {
        "plugin",
        "permission_rules",
        "hooks",
        "mcp_servers",
        "tools",
        "skills",
        "agents",
    }


async def test_plugin_hook_targets_must_stay_in_config_dir(tmp_path: Path) -> None:
    with pytest.raises(ConfigLoadError) as exc:
        await load(
            tmp_path,
            {"settings.yaml": settings_body(), "plugins.yaml": plugins_body("alpha")},
            contributions=(
                PluginContribution(
                    plugin="alpha", hooks=(hook(target="tools/outside.sh"),)
                ),
            ),
        )
    errs = errors_of(exc)
    assert len(errs) == 1
    assert errs[0].path == "hooks[0].target"
    assert "resolves outside the config directory" in errs[0].message


async def test_rule_union_order_origins_and_labels(tmp_path: Path) -> None:
    snap = await load(
        tmp_path,
        {
            "settings.yaml": settings_body(),
            "permissions.yaml": f"rules:\n  - {rule('read_file', 'allow')}\n"
            f"  - {rule('write_file', 'ask')}\n",
            "plugins.yaml": plugins_body("alpha", "beta"),
        },
        contributions=(
            PluginContribution(
                plugin="alpha",
                permission_rules=(
                    PermissionRule(
                        effect=PermissionEffect.DENY, pattern="bash(rm -rf *)"
                    ),
                ),
            ),
            PluginContribution(
                plugin="beta",
                permission_rules=(
                    PermissionRule(effect=PermissionEffect.ALLOW, pattern="glob"),
                ),
            ),
        ),
    )
    assert [r.pattern for r in snap.permissions.rules] == [
        "read_file",
        "write_file",
        "bash(rm -rf *)",
        "glob",
    ]
    assert [o.label for o in snap.rule_origins] == [
        "config/permissions.yaml#0",
        "config/permissions.yaml#1",
        "plugin:alpha#0",
        "plugin:beta#0",
    ]
    assert Origin("config/permissions.yaml", 3).label == "config/permissions.yaml#3"


async def test_full_assembly_end_to_end_all_layers(tmp_path: Path) -> None:
    snap = await load(
        tmp_path,
        {
            "settings.yaml": settings_body("base-model"),
            "settings.local.yaml": settings_body("deploy-model"),
            "permissions.yaml": f"rules:\n  - {rule('read_file', 'allow')}\n",
            "mcp.yaml": "servers:\n"
            "  - {name: op-server, transport: stdio, "
            "command: op}\n",
            "plugins.yaml": plugins_body("alpha", "beta"),
        },
        allow_local=True,
        contributions=(
            PluginContribution(
                plugin="alpha",
                permission_rules=(
                    PermissionRule(
                        effect=PermissionEffect.DENY, pattern="bash(curl *)"
                    ),
                ),
                mcp_servers=(server("alpha-mcp"),),
                skills=("pdf-notes",),
                tools=("chart_render",),
            ),
            PluginContribution(plugin="beta", agents=("reviewer",)),
        ),
    )
    assert snap.settings.provider.model_name == "deploy-model"  # local won
    assert [r.pattern for r in snap.permissions.rules] == [
        "read_file",
        "bash(curl *)",
    ]  # union
    assert [s.name for s in snap.mcp.servers] == ["op-server", "alpha-mcp"]
    assert len(snap.rule_origins) == len(snap.permissions.rules)
    assert len(snap.mcp_origins) == len(snap.mcp.servers)
    assert len(snap.hook_origins) == len(snap.hooks.hooks) == 0
    assert snap.validate() == []


async def test_merged_view_reaches_later_generations_only(tmp_path: Path) -> None:
    """Provenance is per-generation: a run holding v1 never sees v2's
    plugin rules; the alignment guard holds across reloads."""
    files = {"settings.yaml": settings_body(), "plugins.yaml": plugins_body("alpha")}
    snap_v1 = await load(tmp_path, files)
    contribs = (
        PluginContribution(
            plugin="alpha",
            permission_rules=(
                PermissionRule(effect=PermissionEffect.DENY, pattern="mcp__*"),
            ),
        ),
    )
    config_dir = tmp_path / "config"
    store = await ConfigStore.bootstrap(
        repo_root=tmp_path, config_dir=config_dir, environ={}
    )
    report = await store.reload(
        repo_root=tmp_path, config_dir=config_dir, environ={}, contributions=contribs
    )
    assert report.ok and report.version == 2
    assert store.current.permissions.rules  # merged in v2
    assert store.current.rule_origins[0].source == "plugin:alpha"
    assert snap_v1.permissions.rules == []  # v1 assembly untouched
    assert store.current.validate() == []
