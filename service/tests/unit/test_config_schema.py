"""Unit tests for app.config.schema (docs/plan.md T1.1).

The plan's verification column: unknown fields error, type/enum validation,
missing-required errors are clear. On top of that, this file pins the
contract the downstream M1..M12 TODOs lean on:

- field-name whitelists per model = the §5 YAML vocabulary (renaming a
  config key must be a deliberate edit here, like DEPLOY_ENV_VARS in
  test_env.py pins the deployment surface);
- defaults = the decisions themselves: network default ``none`` (D8),
  open registration + per-IP gates (§8.11), concurrency 1 / container 8
  (§8.10), hook block-on-timeout (ADR-010), MCP ask-by-default (ADR-007);
- the strict-mode enum carve-out actually works at every annotation depth
  (field, list element, dict key);
- secrets never ride a repr (SecretStr), models are frozen (invariant #6).
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from app.config.schema import (
    BUILTIN_TOOL_NAMES,
    MODEL_CONFIG,
    TOOL_RESULT_BYTES_HARD_MAX,
    TOOL_TIMEOUT_S_HARD_MAX,
    AgentFrontmatter,
    BashToolEntry,
    HookConfig,
    HookEntry,
    HookEvent,
    HookKind,
    HookTimeoutAction,
    HttpFetchToolEntry,
    LimitsConfig,
    MCPConfig,
    MCPServerConfig,
    MCPTransport,
    NetworkMode,
    PermissionEffect,
    PermissionMode,
    PermissionRule,
    PermissionsConfig,
    PermissionsDefaults,
    PermissionsPolicy,
    PluginEntry,
    PluginsConfig,
    ProviderConfig,
    RegistrationConfig,
    SandboxConfig,
    Settings,
    SkillFrontmatter,
    ToolCatalog,
    ToolConfig,
    ToolDefaults,
    ToolEntry,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

PROVIDER: dict[str, Any] = {
    "base_url": "https://llm.example.com/v1",
    "api_key": "sk-test-not-a-real-key",
    "model_name": "vanilla-mini",
}

SKILL_MIN: dict[str, Any] = {"name": "pdf-notes", "description": "Summarize PDFs"}
AGENT_MIN: dict[str, Any] = {"name": "code-review", "description": "Review diffs"}
HOOK_MIN: dict[str, Any] = {
    "event": "PreToolUse",
    "kind": "command",
    "target": "scripts/hooks/guard.sh",
}
MCP_STDIO_MIN: dict[str, Any] = {
    "name": "fs-tools",
    "transport": "stdio",
    "command": "mcp-server-fs",
}
MCP_HTTP_MIN: dict[str, Any] = {
    "name": "remote-tools",
    "transport": "streamable_http",
    "url": "https://mcp.example.com/rpc",
}
PLUGIN_MIN: dict[str, Any] = {"name": "demo", "path": "config/plugins/demo"}
RULE_MIN: dict[str, Any] = {"effect": "ask", "pattern": "bash(git *)"}

#: Every public model with the smallest payload that validates. Drives both
#: the "unknown field rejected" sweep and the field-whitelist pinning.
MINIMAL_PAYLOADS: dict[type[BaseModel], dict[str, Any]] = {
    Settings: {"provider": dict(PROVIDER)},
    ProviderConfig: dict(PROVIDER),
    LimitsConfig: {},
    SandboxConfig: {},
    RegistrationConfig: {},
    PermissionsDefaults: {},
    ToolConfig: {},
    ToolDefaults: {},
    ToolEntry: {},
    BashToolEntry: {},
    HttpFetchToolEntry: {},
    ToolCatalog: {},
    PermissionsConfig: {"rules": [dict(RULE_MIN)]},
    PermissionsPolicy: {},
    PermissionRule: dict(RULE_MIN),
    HookConfig: {"hooks": [dict(HOOK_MIN)]},
    HookEntry: dict(HOOK_MIN),
    MCPConfig: {"servers": [dict(MCP_STDIO_MIN)]},
    MCPServerConfig: dict(MCP_STDIO_MIN),
    PluginsConfig: {"plugins": [dict(PLUGIN_MIN)]},
    PluginEntry: dict(PLUGIN_MIN),
    SkillFrontmatter: dict(SKILL_MIN),
    AgentFrontmatter: dict(AGENT_MIN),
}


def _entry_name(item: tuple[type[BaseModel], Any]) -> str:
    # Named function (not a lambda) so parametrize's overloaded signature
    # does not collapse the sort key's argument to `object` for pyright.
    return item[0].__name__


def _model_name(model: type[BaseModel]) -> str:
    return model.__name__


def err_types(exc: ValidationError) -> list[str]:
    return [str(e["type"]) for e in exc.errors()]


def err_locs(exc: ValidationError) -> list[tuple[str, ...]]:
    return [tuple(str(p) for p in e["loc"]) for e in exc.errors()]


def parse(model: type[BaseModel], **overrides: Any) -> Any:
    """Validate the model's minimal payload merged with *overrides*."""
    return model.model_validate({**MINIMAL_PAYLOADS[model], **overrides})


# ---------------------------------------------------------------------------
# Sweep 1: unknown fields error (T1.1 "extra=forbid"), every model
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "payload"),
    sorted(MINIMAL_PAYLOADS.items(), key=_entry_name),
)
def test_minimal_payload_validates(model: type[BaseModel], payload: dict) -> None:
    model.model_validate(payload)


@pytest.mark.parametrize(
    ("model", "payload"),
    sorted(MINIMAL_PAYLOADS.items(), key=_entry_name),
)
def test_unknown_field_rejected(model: type[BaseModel], payload: dict) -> None:
    bad = {**payload, "__not_a_config_key__": 1}
    with pytest.raises(ValidationError) as excinfo:
        model.model_validate(bad)
    assert "extra_forbidden" in err_types(excinfo.value)
    assert ("__not_a_config_key__",) in err_locs(excinfo.value)


def test_catalog_typos_are_extra_forbidden_not_ignored() -> None:
    """The closed ToolCatalog turns `read_fiile:` into an error; a free-form
    dict of tool names could not have."""
    with pytest.raises(ValidationError) as excinfo:
        ToolConfig.model_validate({"tools": {"read_fiile": {"enabled": False}}})
    assert "extra_forbidden" in err_types(excinfo.value)
    assert ("tools", "read_fiile") in err_locs(excinfo.value)


# ---------------------------------------------------------------------------
# Sweep 2: field whitelists pin the §5 YAML vocabulary
# ---------------------------------------------------------------------------

EXPECTED_FIELDS: dict[type[BaseModel], set[str]] = {
    Settings: {"provider", "limits", "sandbox", "registration", "permissions"},
    ProviderConfig: {
        "base_url",
        "api_key",
        "model_name",
        "temperature",
        "top_p",
        "max_tokens",
        "timeout_s",
        "retries",
        "allowed_models",
    },
    LimitsConfig: {
        "max_turns",
        "wall_clock_s",
        "max_concurrent_runs",
        "per_user_daily_tokens",
        "global_hourly_tokens",
    },
    SandboxConfig: {
        "memory",
        "cpus",
        "pids_limit",
        "network_mode",
        "max_live_containers",
        "idle_ttl_s",
        "image_tag",
        "base_image",
        "pip_index_url",
        "apt_mirror",
    },
    RegistrationConfig: {"open", "per_ip_rate", "per_ip_daily"},
    PermissionsDefaults: {"default_mode", "allowed_modes"},
    ToolConfig: {"defaults", "tools"},
    ToolDefaults: {"timeout_s", "max_result_bytes", "max_args_bytes"},
    ToolEntry: {"enabled", "timeout_s", "max_result_bytes", "max_args_bytes"},
    BashToolEntry: {
        "enabled",
        "timeout_s",
        "max_result_bytes",
        "max_args_bytes",
        "hard_timeout_s",
    },
    HttpFetchToolEntry: {
        "enabled",
        "timeout_s",
        "max_result_bytes",
        "max_args_bytes",
        "allowed_domains",
        "max_redirects",
        "max_response_bytes",
    },
    PermissionsConfig: {"default_mode", "policy", "mode_presets", "rules"},
    PermissionsPolicy: {"disallow_bypass"},
    PermissionRule: {"effect", "pattern", "note", "source"},
    HookConfig: {"hooks"},
    HookEntry: {
        "event",
        "matcher",
        "kind",
        "target",
        "timeout_ms",
        "on_timeout",
        "enabled",
    },
    MCPConfig: {"servers", "connect_timeout_s"},
    MCPServerConfig: {
        "name",
        "transport",
        "command",
        "args",
        "env",
        "url",
        "headers",
        "requires_approval",
        "enabled",
        "connect_timeout_s",
    },
    PluginsConfig: {"plugins"},
    PluginEntry: {"name", "path", "enabled", "trusted"},
    SkillFrontmatter: {
        "name",
        "description",
        "when_to_use",
        "allowed_tools",
        "model",
        "version",
    },
    AgentFrontmatter: {
        "name",
        "description",
        "tools",
        "exclude_tools",
        "model",
        "max_turns",
        "permission_mode",
        "timeout_s",
    },
}


@pytest.mark.parametrize("model", sorted(EXPECTED_FIELDS, key=_model_name))
def test_field_whitelist_matches_plan_section_5(model: type[BaseModel]) -> None:
    assert set(model.model_fields) == EXPECTED_FIELDS[model]


def test_builtin_tool_names_are_the_closed_set() -> None:
    """The registry (T6.1), skill checks (T9.4) and agent checks (T10.1) all
    reference this exact set; additions ride on new tool implementations."""
    assert BUILTIN_TOOL_NAMES == {
        "read_file",
        "write_file",
        "edit_file",
        "delete_file",
        "move_file",
        "list_dir",
        "glob",
        "grep",
        "bash",
        "http_fetch",
        "ask_user",
        "skill_load",
        "task",
        "todo_write",
        "enter_plan_mode",
        "exit_plan_mode",
    }
    assert set(ToolCatalog.model_fields) == BUILTIN_TOOL_NAMES


def test_hook_event_values_are_the_t12_wire_names() -> None:
    assert [e.value for e in HookEvent] == [
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "PermissionRequest",
        "PermissionDenied",
        "SubagentStart",
        "SubagentStop",
        "PreCompact",
        "PostCompact",
        "Stop",
    ]


# ---------------------------------------------------------------------------
# Sweep 3: missing required fields report loc + "Field required"
# ---------------------------------------------------------------------------


def test_settings_requires_provider() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings.model_validate({})
    assert ("provider",) in err_locs(excinfo.value)
    assert any(e["msg"] == "Field required" for e in excinfo.value.errors())


@pytest.mark.parametrize("field", ["base_url", "api_key", "model_name"])
def test_provider_three_required_fields(field: str) -> None:
    payload = {k: v for k, v in PROVIDER.items() if k != field}
    with pytest.raises(ValidationError) as excinfo:
        ProviderConfig.model_validate(payload)
    assert err_locs(excinfo.value) == [(field,)]
    assert excinfo.value.errors()[0]["msg"] == "Field required"


@pytest.mark.parametrize(
    ("model", "field"),
    [
        (PermissionRule, "effect"),
        (PermissionRule, "pattern"),
        (HookEntry, "event"),
        (HookEntry, "kind"),
        (HookEntry, "target"),
        (MCPServerConfig, "name"),
        (MCPServerConfig, "transport"),
        (PluginEntry, "name"),
        (PluginEntry, "path"),
        (SkillFrontmatter, "name"),
        (SkillFrontmatter, "description"),
        (AgentFrontmatter, "name"),
        (AgentFrontmatter, "description"),
    ],
)
def test_nested_required_fields_report_their_own_loc(
    model: type[BaseModel], field: str
) -> None:
    payload = {k: v for k, v in MINIMAL_PAYLOADS[model].items() if k != field}
    with pytest.raises(ValidationError) as excinfo:
        model.model_validate(payload)
    assert (field,) in err_locs(excinfo.value)


# ---------------------------------------------------------------------------
# Enums: accepted spellings and rejections
# ---------------------------------------------------------------------------


def test_enum_value_vocabulary() -> None:
    # These strings are the YAML vocabulary (ADR-009 camelCase included).
    assert [m.value for m in PermissionMode] == [
        "default",
        "plan",
        "acceptEdits",
        "bypass",
    ]
    assert [e.value for e in PermissionEffect] == ["allow", "ask", "deny"]
    assert [n.value for n in NetworkMode] == ["none", "internal", "per_user_bridge"]
    assert [t.value for t in MCPTransport] == ["stdio", "streamable_http"]
    assert [k.value for k in HookKind] == ["command", "python"]
    assert [a.value for a in HookTimeoutAction] == ["block", "allow"]


@pytest.mark.parametrize("mode", ["none", "internal", "per_user_bridge"])
def test_network_mode_three_tiers(mode: str) -> None:
    assert parse(SandboxConfig, network_mode=mode).network_mode is NetworkMode(mode)


def test_plain_bridge_is_not_expressible() -> None:
    """ADR-005/§8.4: the default docker bridge breaks cross-user isolation
    and must stay a validation error, not a config option."""
    with pytest.raises(ValidationError) as excinfo:
        parse(SandboxConfig, network_mode="bridge")
    assert "enum" in err_types(excinfo.value)
    # Error text lists the legal spellings for the author.
    assert "per_user_bridge" in str(excinfo.value)


def test_always_effect_is_gone() -> None:
    """ADR-009 removed `always` (it would mean writing config at runtime)."""
    with pytest.raises(ValidationError):
        parse(PermissionRule, effect="always")


def test_hook_event_spelling_is_case_sensitive() -> None:
    assert parse(HookEntry, event="PostToolUse").event is HookEvent.POST_TOOL_USE
    with pytest.raises(ValidationError):
        parse(HookEntry, event="post_tool_use")


def test_enum_carve_out_works_at_every_annotation_depth() -> None:
    """strict=True would reject str->StrEnum by identity; *Input aliases
    re-admit it by value — in fields, list elements, and dict keys alike."""
    cfg = PermissionsConfig.model_validate(
        {
            "default_mode": "plan",
            "mode_presets": {"acceptEdits": [dict(RULE_MIN)]},
        }
    )
    assert cfg.default_mode is PermissionMode.PLAN
    assert list(cfg.mode_presets) == [PermissionMode.ACCEPT_EDITS]
    defaults = parse(PermissionsDefaults, allowed_modes=["default", "plan"])
    assert defaults.allowed_modes == [PermissionMode.DEFAULT, PermissionMode.PLAN]
    with pytest.raises(ValidationError):
        parse(PermissionsDefaults, allowed_modes=["default", "turbo"])
    with pytest.raises(ValidationError):
        PermissionsConfig.model_validate({"mode_presets": {"yolo": []}})


# ---------------------------------------------------------------------------
# Strict scalars and constrained ranges
# ---------------------------------------------------------------------------


def test_numeric_strings_are_rejected_not_coerced() -> None:
    for model, field, value in [
        (ProviderConfig, "timeout_s", "120"),
        (ProviderConfig, "retries", "2"),
        (LimitsConfig, "max_turns", 1.5),
        (SandboxConfig, "pids_limit", "256"),
        (HookEntry, "timeout_ms", "10000"),
    ]:
        base = dict(MINIMAL_PAYLOADS[model])
        with pytest.raises(ValidationError) as excinfo:
            model.model_validate({**base, field: value})
        assert (field,) in err_locs(excinfo.value), (model, field, value)


def test_int_widens_to_float_but_bool_never_passes() -> None:
    # `cpus: 1` is a plain int in YAML and must be accepted as 1.0.
    assert parse(SandboxConfig, cpus=1).cpus == 1.0
    with pytest.raises(ValidationError):
        parse(SandboxConfig, cpus=True)
    with pytest.raises(ValidationError):
        parse(RegistrationConfig, open="yes")  # strict bool: no string truthiness


@pytest.mark.parametrize(
    ("field", "ok", "bad"),
    [
        ("temperature", [0, 2, 0.7], [2.5, -0.1]),
        ("top_p", [1, 0.001], [0, 1.2]),
        ("retries", [0, 10], [-1, 11]),
        ("max_tokens", [1], [0, -5]),
        ("timeout_s", [0.5], [0, -1]),
    ],
)
def test_provider_ranges(field: str, ok: list, bad: list) -> None:
    for value in ok:
        assert parse(ProviderConfig, **{field: value}) is not None
    for value in bad:
        with pytest.raises(ValidationError) as excinfo:
            parse(ProviderConfig, **{field: value})
        assert (field,) in err_locs(excinfo.value)


def test_sandbox_ranges_and_size_specs() -> None:
    for memory in ["2g", "512m", "1024k", "1000000"]:
        assert parse(SandboxConfig, memory=memory).memory == memory
    for memory in ["2G-b", "g", "2 g", "-2g", ""]:
        with pytest.raises(ValidationError):
            parse(SandboxConfig, memory=memory)
    for field in ["pids_limit", "max_live_containers"]:
        for bad in [0, -1]:
            with pytest.raises(ValidationError):
                parse(SandboxConfig, **{field: bad})


def test_domain_slug_and_path_shapes() -> None:
    entry = parse(
        HttpFetchToolEntry,
        allowed_domains=["example.com", "sub.example.com", "*.example.org"],
    )
    assert len(entry.allowed_domains) == 3
    for bad in [
        "https://example.com",
        "example.com/path",
        "EXAMPLE.com",
        "exa mple.com",
        "*",
        ".example.com",
        "example..com",
    ]:
        with pytest.raises(ValidationError):
            parse(HttpFetchToolEntry, allowed_domains=[bad])

    for good in ["x", "a1", "pdf-notes", "code-review"]:
        assert SkillFrontmatter.model_validate({**SKILL_MIN, "name": good}).name == good
    for bad in ["Pdf-Notes", "_x", "-x", "x-", "a__b", "x" * 65, "中文"]:
        with pytest.raises(ValidationError):
            SkillFrontmatter.model_validate({**SKILL_MIN, "name": bad})

    for good in ["config/plugins/demo", "demo", "./demo"]:
        assert PluginEntry.model_validate({**PLUGIN_MIN, "path": good})
    for bad in ["/etc", "..", "../outside", "config/../etc", "C:\\plugins"]:
        with pytest.raises(ValidationError):
            PluginEntry.model_validate({**PLUGIN_MIN, "path": bad})


def test_hook_and_fetch_bounds() -> None:
    assert parse(HookEntry, timeout_ms=100).timeout_ms == 100
    with pytest.raises(ValidationError):
        parse(HookEntry, timeout_ms=50)
    with pytest.raises(ValidationError):
        parse(HookEntry, matcher="   ")  # blank after strip
    assert parse(HttpFetchToolEntry, max_redirects=0).max_redirects == 0
    with pytest.raises(ValidationError):
        parse(HttpFetchToolEntry, max_redirects=21)


def test_list_items_are_strict_too() -> None:
    with pytest.raises(ValidationError):
        parse(MCPServerConfig, name="ok", transport="stdio", command="x", args=["a", 1])


# ---------------------------------------------------------------------------
# Decision-bearing defaults (D8, §8.10, §8.11, ADR-007/009/010)
# ---------------------------------------------------------------------------


def test_default_posture_is_the_safest_one() -> None:
    sandbox, reg, limits = SandboxConfig(), RegistrationConfig(), LimitsConfig()
    assert sandbox.network_mode is NetworkMode.NONE  # D8, plan line 643
    assert sandbox.max_live_containers == 8  # §8.10b default (re-size at T1.5)
    assert limits.max_concurrent_runs == 1  # §8.10 per-user default
    assert reg.open is True  # §8.11 default true; config-check flags + non-loopback
    assert reg.per_ip_rate == 5 and reg.per_ip_daily == 10


def test_default_knobs_of_the_remaining_files() -> None:
    assert PermissionsPolicy().disallow_bypass is False  # operator declares it
    assert PermissionsConfig().default_mode is PermissionMode.DEFAULT
    # D8 posture: bypass is only ever in allowed_modes if an author puts it
    # there explicitly.
    assert PermissionsDefaults().allowed_modes == [
        PermissionMode.DEFAULT,
        PermissionMode.PLAN,
        PermissionMode.ACCEPT_EDITS,
    ]
    hook = HookEntry(event=HookEvent.PRE_TOOL_USE, kind=HookKind.COMMAND, target="s.sh")
    assert (hook.matcher, hook.timeout_ms) == ("*", 10_000)
    assert hook.on_timeout is HookTimeoutAction.BLOCK and hook.enabled
    server = MCPServerConfig.model_validate(MCP_STDIO_MIN)
    assert server.requires_approval is True and server.enabled is True
    assert server.connect_timeout_s is None  # falls through to file default
    assert MCPConfig().connect_timeout_s == 30.0
    defaults = ToolDefaults()
    assert defaults.timeout_s == 60.0 and defaults.max_result_bytes == 64 * 1024
    assert defaults.max_args_bytes is None
    assert BashToolEntry().hard_timeout_s == 300.0
    provider = ProviderConfig.model_validate(PROVIDER)
    assert (provider.temperature, provider.top_p) == (1.0, 1.0)
    assert provider.timeout_s == 120.0 and provider.retries == 2
    assert provider.allowed_models is None  # → effective = default model only
    assert provider.effective_allowed_models == ("vanilla-mini",)
    listed = ProviderConfig.model_validate(
        {**PROVIDER, "allowed_models": ["vanilla-mini", "vanilla-max"]}
    )
    assert listed.effective_allowed_models == ("vanilla-mini", "vanilla-max")


def test_hard_caps_admit_overshoot_for_t6_1_to_clamp() -> None:
    """T6.1's contract is *clamp + ConfigIssue warning*; rejecting >cap at
    the schema would make that untestable (the config could not even load)."""
    entry = ToolEntry(
        timeout_s=TOOL_TIMEOUT_S_HARD_MAX * 2,
        max_result_bytes=TOOL_RESULT_BYTES_HARD_MAX * 2,
    )
    assert entry.timeout_s is not None
    assert entry.max_result_bytes is not None
    assert entry.timeout_s > TOOL_TIMEOUT_S_HARD_MAX
    assert entry.max_result_bytes > TOOL_RESULT_BYTES_HARD_MAX


# ---------------------------------------------------------------------------
# Cross-field checks a single file can answer
# ---------------------------------------------------------------------------


def test_provider_default_model_must_be_in_allowlist() -> None:
    with pytest.raises(ValidationError) as excinfo:
        parse(ProviderConfig, allowed_models=["other-model"])
    assert "model_name" in str(excinfo.value)
    with pytest.raises(ValidationError):
        parse(ProviderConfig, allowed_models=[])
    ok = parse(ProviderConfig, allowed_models=["vanilla-mini", "other"])
    assert ok.model_name == "vanilla-mini"


def test_settings_permissions_default_must_be_allowed() -> None:
    with pytest.raises(ValidationError) as excinfo:
        parse(
            PermissionsDefaults,
            default_mode="bypass",
            allowed_modes=["default", "plan"],
        )
    assert "allowed_modes" in str(excinfo.value)
    assert (
        parse(
            PermissionsDefaults, default_mode="plan", allowed_modes=["default", "plan"]
        ).default_mode
        is PermissionMode.PLAN
    )


def test_disallow_bypass_owns_its_file_default() -> None:
    with pytest.raises(ValidationError) as excinfo:
        PermissionsConfig.model_validate(
            {"default_mode": "bypass", "policy": {"disallow_bypass": True}}
        )
    assert "disallow_bypass" in str(excinfo.value)
    assert PermissionsConfig.model_validate(
        {"default_mode": "acceptEdits", "policy": {"disallow_bypass": True}}
    )


@pytest.mark.parametrize(
    ("payload", "expect_error"),
    [
        ({"transport": "stdio"}, True),  # no command
        ({"transport": "stdio", "command": "c", "url": "https://x.dev"}, True),
        ({"transport": "streamable_http"}, True),  # no url
        (
            {"transport": "streamable_http", "url": "https://x.dev", "command": "c"},
            True,
        ),
        ({"transport": "streamable_http", "url": "https://x.dev"}, False),
        (
            {
                "transport": "stdio",
                "command": "c",
                "args": ["--v"],
                "env": {"TOKEN": "x"},
            },
            False,
        ),
    ],
)
def test_mcp_transport_shapes_are_exclusive(payload, expect_error) -> None:
    body = {"name": "srv", **payload}
    if expect_error:
        with pytest.raises(ValidationError):
            MCPServerConfig.model_validate(body)
    else:
        assert MCPServerConfig.model_validate(body)


def test_mcp_and_plugin_names_must_be_unique_within_a_file() -> None:
    with pytest.raises(ValidationError) as excinfo:
        MCPConfig.model_validate(
            {"servers": [dict(MCP_STDIO_MIN), dict(MCP_STDIO_MIN)]}
        )
    assert MCP_STDIO_MIN["name"] in str(excinfo.value)
    with pytest.raises(ValidationError):
        PluginsConfig.model_validate({"plugins": [dict(PLUGIN_MIN), dict(PLUGIN_MIN)]})


def test_python_hook_target_must_name_a_callable() -> None:
    assert (
        parse(HookEntry, kind="python", target="myplugin.hooks:check").kind
        is HookKind.PYTHON
    )
    with pytest.raises(ValidationError) as excinfo:
        parse(HookEntry, kind="python", target="myplugin.hooks.check")
    assert "module:attr" in str(excinfo.value)
    # command targets keep plain paths, colons not required:
    assert parse(HookEntry, kind="command", target="scripts/run.sh")


def test_agent_toolset_allows_tools_or_exclude_not_both() -> None:
    assert parse(AgentFrontmatter, tools=["read_file"]).exclude_tools is None
    assert parse(AgentFrontmatter, exclude_tools=["bash"]).tools is None
    with pytest.raises(ValidationError) as excinfo:
        parse(AgentFrontmatter, tools=["read_file"], exclude_tools=["bash"])
    assert "not both" in str(excinfo.value)
    with pytest.raises(ValidationError):
        parse(AgentFrontmatter, permission_mode="turbo")


# ---------------------------------------------------------------------------
# SecretStr: secrets never ride repr/dump; blank keys rejected
# ---------------------------------------------------------------------------


def test_api_key_never_appears_in_repr_or_dump() -> None:
    settings = Settings.model_validate(MINIMAL_PAYLOADS[Settings])
    needle = PROVIDER["api_key"]
    assert needle not in repr(settings) and needle not in str(settings)
    dumped = settings.model_dump(mode="json")
    assert dumped["provider"]["api_key"] == "**********"


def test_mcp_env_and_header_values_are_secrets() -> None:
    cfg = MCPConfig.model_validate(
        {
            "servers": [
                {**MCP_HTTP_MIN, "headers": {"Authorization": "Bearer sk-topsecret"}},
                {**MCP_STDIO_MIN, "env": {"API_TOKEN": "sk-topsecret2"}},
            ]
        }
    )
    assert "sk-topsecret" not in repr(cfg) and "sk-topsecret2" not in repr(cfg)
    values = [
        s.headers["Authorization"].get_secret_value()
        for s in cfg.servers
        if s.name == "remote-tools"
    ]
    assert values == ["Bearer sk-topsecret"]


def test_blank_api_key_is_rejected() -> None:
    for blank in ["", "   "]:
        with pytest.raises(ValidationError) as excinfo:
            parse(ProviderConfig, api_key=blank)
        assert ("api_key",) in err_locs(excinfo.value)
    assert parse(ProviderConfig, api_key="real-looking-key")


# ---------------------------------------------------------------------------
# Frozen: invariant #6 starts at the leaves
# ---------------------------------------------------------------------------


def test_models_are_frozen_end_to_end() -> None:
    settings = Settings.model_validate(
        {
            "provider": dict(PROVIDER),
            "sandbox": {"network_mode": "internal"},
        }
    )
    with pytest.raises(ValidationError):
        settings.sandbox = SandboxConfig()  # type: ignore[misc]
    with pytest.raises(ValidationError):
        settings.sandbox.network_mode = NetworkMode.NONE  # type: ignore[misc]
    with pytest.raises(ValidationError):
        settings.provider.model_name = "other"  # type: ignore[misc]
    assert MODEL_CONFIG.get("frozen") is True
    assert MODEL_CONFIG.get("extra") == "forbid"
    assert MODEL_CONFIG.get("strict") is True


# ---------------------------------------------------------------------------
# One realistic multi-section document per file (acceptance smoke)
# ---------------------------------------------------------------------------


def test_full_realistic_settings_document() -> None:
    settings = Settings.model_validate(
        {
            "provider": {
                **PROVIDER,
                "temperature": 0.2,
                "max_tokens": 8192,
                "timeout_s": 90,
                "retries": 3,
                "allowed_models": ["vanilla-mini", "vanilla-max"],
            },
            "limits": {
                "max_turns": 40,
                "wall_clock_s": 900,
                "per_user_daily_tokens": 500_000,
                "global_hourly_tokens": 4_000_000,
            },
            "sandbox": {
                "memory": "2g",
                "cpus": 2,
                "network_mode": "none",
                "max_live_containers": 6,
                "idle_ttl_s": 600,
                "image_tag": "2026.11",
                "base_image": "python:3.12-slim",
                "pip_index_url": "https://pypi.example.com/simple",
            },
            "registration": {"open": False, "per_ip_rate": 2, "per_ip_daily": 4},
            "permissions": {
                "default_mode": "plan",
                "allowed_modes": ["default", "plan", "acceptEdits"],
            },
        }
    )
    assert settings.limits.max_turns == 40
    assert settings.sandbox.cpus == 2.0  # YAML int widened
    assert settings.registration.open is False
    assert settings.permissions.allowed_modes == [
        PermissionMode.DEFAULT,
        PermissionMode.PLAN,
        PermissionMode.ACCEPT_EDITS,
    ]
    assert settings.provider.effective_allowed_models == ("vanilla-mini", "vanilla-max")


def test_full_realistic_capability_files() -> None:
    tools = ToolConfig.model_validate(
        {
            "defaults": {"timeout_s": 30, "max_result_bytes": 32768},
            "tools": {
                "write_file": {"enabled": False},
                "edit_file": {"enabled": False},
                "bash": {"hard_timeout_s": 120},
                "http_fetch": {"allowed_domains": ["docs.example.com"]},
            },
        }
    )
    assert tools.tools.write_file.enabled is False
    assert tools.tools.write_file.timeout_s is None  # falls through
    assert tools.tools.bash.hard_timeout_s == 120.0
    assert tools.tools.read_file.enabled is True

    perms = PermissionsConfig.model_validate(
        {
            "default_mode": "default",
            "policy": {"disallow_bypass": True},
            "rules": [
                {"effect": "deny", "pattern": "bash(rm *)", "note": "no deletes"},
                {"effect": "allow", "pattern": "read_file"},
                {"effect": "ask", "pattern": "mcp__fs-tools__*"},
            ],
        }
    )
    assert [r.effect for r in perms.rules] == [
        PermissionEffect.DENY,
        PermissionEffect.ALLOW,
        PermissionEffect.ASK,
    ]

    hooks = HookConfig.model_validate(
        {
            "hooks": [
                HOOK_MIN,
                {
                    **HOOK_MIN,
                    "event": "Stop",
                    "matcher": "*",
                    "kind": "python",
                    "target": "demo_plugin.hooks:on_stop",
                    "on_timeout": "allow",
                    "timeout_ms": 5000,
                },
            ]
        }
    )
    assert hooks.hooks[1].on_timeout is HookTimeoutAction.ALLOW

    mcp = MCPConfig.model_validate(
        {
            "servers": [
                {**MCP_STDIO_MIN, "args": ["--root", "/srv"], "enabled": False},
                {
                    **MCP_HTTP_MIN,
                    "headers": {"Authorization": "Bearer x"},
                    "connect_timeout_s": 5,
                },
            ]
        }
    )
    assert mcp.servers[1].connect_timeout_s == 5.0

    plugins = PluginsConfig.model_validate(
        {"plugins": [{**PLUGIN_MIN, "enabled": False, "trusted": True}]}
    )
    assert plugins.plugins[0].trusted is True

    skill = SkillFrontmatter.model_validate(
        {
            **SKILL_MIN,
            "when_to_use": "When a PDF needs notes.",
            "allowed_tools": ["read_file", "mcp__fs-tools__fetch"],
            "model": "vanilla-mini",
            "version": "1.0",
        }
    )
    assert skill.allowed_tools == ["read_file", "mcp__fs-tools__fetch"]

    agent = AgentFrontmatter.model_validate(
        {
            **AGENT_MIN,
            "exclude_tools": ["task"],
            "max_turns": 10,
            "permission_mode": "plan",
            "timeout_s": 600,
        }
    )
    assert agent.permission_mode is PermissionMode.PLAN
