"""Declarative schema for the ``config/`` tree (docs/plan.md T1.1; ADR-015, §5).

One root model per configuration file — ``settings.yaml`` -> :class:`Settings`,
``tools.yaml`` -> :class:`ToolConfig`, ``permissions.yaml`` ->
:class:`PermissionsConfig`, ``hooks.yaml`` -> :class:`HookConfig`,
``mcp.yaml`` -> :class:`MCPConfig`, ``plugins.yaml`` -> :class:`PluginsConfig`
— plus the two shared frontmatter models for
``config/skills/<slug>/SKILL.md`` (:class:`SkillFrontmatter`) and
``config/agents/<name>.md`` (:class:`AgentFrontmatter`).

This module is *pure field vocabulary*: types, enums, required keys, ranges,
and the cross-field checks a single file can answer on its own. Everything
with filesystem or cross-file context belongs downstream, in plan order:

- YAML reading, ``${env:VAR}`` expansion, path resolution against
  ``REPO_ROOT``, and collect-all-issues reporting: the loader (T1.2);
- merge precedence (settings defaults < plugin contributions < ``*.local.yaml``)
  and same-name conflicts across sources: T1.4;
- reference integrity (skill ``allowed_tools`` ∈ enabled tools, agent tool
  references, hook/plugin existence): snapshot ``validate()`` and
  config-check (T1.3/T1.6);
- directory ownership / world-writable trust boundary: T1.7.

Strictness (plan T1.1 "strict, ``extra="forbid"``"): every model forbids
unknown fields and validates in pydantic strict mode, so ``timeout_s: "60"``
is a typo to report, never a silent coercion. The deliberate exceptions:
int-for-float widening is still accepted (YAML writes ``cpus: 1``), and enums
— strict mode refuses ``str -> StrEnum`` by identity, which no YAML-sourced
config can satisfy — are re-admitted by *value* through the ``*Input``
aliases below, keeping the good ``Input should be 'a' or 'b'`` error text.

Immutability: every model is frozen. Main-loop invariant #6 (docs/plan.md §3)
says a run holds its own ConfigSnapshot; nothing may mutate loaded config in
place, so immutability starts at these leaf models.

Secrets: values that are secret at rest (``provider.api_key``, MCP server
``env``/``headers``) are ``SecretStr``, so an accidental ``repr`` of any
enclosing model shows ``**********``, never the value (§8.6). ``${env:}``
references are expanded by the loader *before* validation, so models always
see resolved values, never placeholders.

Defaults: conservative shipped starting points; the actual configured numbers
live in ``config/*.yaml`` (T1.5). Numbers whose correct value depends on the
deployment host carry the recomputation *principle* (§8.10), not a concrete
observation.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Final, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)

# ---------------------------------------------------------------------------
# Shared model policy and scalar aliases
# ---------------------------------------------------------------------------

#: Unknown keys are errors, scalars are not coerced, instances are frozen.
#: See the module docstring for the enum and int->float carve-outs.
MODEL_CONFIG: Final = ConfigDict(extra="forbid", strict=True, frozen=True)

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
#: Kebab/lower-case identifiers: skill slugs, subagent names, MCP server
#: names (embedded into ``mcp__<server>__<tool>``, so no underscore/``__``
#: can ever make that split ambiguous), plugin names.
SlugStr = Annotated[
    str,
    StringConstraints(
        pattern=r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$", min_length=1, max_length=64
    ),
]
#: Kept ``str`` (not ``HttpUrl``): the openai SDK and docker tooling take
#: opaque base URLs verbatim; scheme+shape is the checkable part (§5).
HttpUrlStr = Annotated[str, StringConstraints(pattern=r"^https?://\S+$")]
#: Bare hostname, optionally ``*.``-prefixed. Matching semantics belong to
#: T6.7; the shape is pinned here so an allowlist entry can never smuggle a
#: scheme or a path.
DomainStr = Annotated[
    str,
    StringConstraints(
        pattern=r"^(\*\.)?[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
        r"(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$"
    ),
]
#: Docker size notation: ``2g``, ``512m``, ``1024k``, or plain bytes.
SizeSpecStr = Annotated[str, StringConstraints(pattern=r"^\d+([kmgtKMGT]b?)?$")]
PositiveInt = Annotated[int, Field(gt=0)]
PositiveFloat = Annotated[float, Field(gt=0)]
DescriptionStr = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1024)
]

# ---------------------------------------------------------------------------
# Enums (values are the YAML vocabulary; spellings reach config files)
# ---------------------------------------------------------------------------


class PermissionMode(StrEnum):
    """ADR-009 modes. ``acceptEdits`` is camelCase on purpose — that is the
    string authors write in ``permissions.yaml``/``agents/*.md``."""

    DEFAULT = "default"
    PLAN = "plan"
    ACCEPT_EDITS = "acceptEdits"
    BYPASS = "bypass"


class PermissionEffect(StrEnum):
    """Rule effects. There is no ``always``: that would mean writing config
    at runtime, which ADR-015/ADR-009 removed. Effect precedence
    (deny > ask > allow) is engine behavior (T7.1), not schema."""

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class NetworkMode(StrEnum):
    """ADR-005/D8. The plain docker ``bridge`` network is *not* expressible:
    same-segment container reachability would break cross-user isolation, so
    relaxation is only ``internal`` or ``per_user_bridge`` (§8.4)."""

    NONE = "none"
    INTERNAL = "internal"
    PER_USER_BRIDGE = "per_user_bridge"


class HookEvent(StrEnum):
    """T12.1 wire names (Claude Code style, PascalCase)."""

    SESSION_START = "SessionStart"
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    POST_TOOL_USE_FAILURE = "PostToolUseFailure"
    PERMISSION_REQUEST = "PermissionRequest"
    PERMISSION_DENIED = "PermissionDenied"
    SUBAGENT_START = "SubagentStart"
    SUBAGENT_STOP = "SubagentStop"
    PRE_COMPACT = "PreCompact"
    POST_COMPACT = "PostCompact"
    STOP = "Stop"


class HookKind(StrEnum):
    COMMAND = "command"
    PYTHON = "python"


class HookTimeoutAction(StrEnum):
    """``on_timeout``: what to do when a hook overruns (ADR-010). Blocking is
    the default: an unresponsive hook must not silently wave work through."""

    BLOCK = "block"
    ALLOW = "allow"


class MCPTransport(StrEnum):
    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable_http"


# One *Input alias per enum, applied at the innermost annotation (field,
# dict key, list element). Never annotate a config enum with the bare class.
PermissionModeInput = Annotated[PermissionMode, Field(strict=False)]
PermissionEffectInput = Annotated[PermissionEffect, Field(strict=False)]
NetworkModeInput = Annotated[NetworkMode, Field(strict=False)]
HookEventInput = Annotated[HookEvent, Field(strict=False)]
HookKindInput = Annotated[HookKind, Field(strict=False)]
HookTimeoutActionInput = Annotated[HookTimeoutAction, Field(strict=False)]
MCPTransportInput = Annotated[MCPTransport, Field(strict=False)]


def _duplicates(values: list[str]) -> list[str]:
    """Names occurring more than once, in first-repeat order."""
    seen: set[str] = set()
    dup: list[str] = []
    for value in values:
        if value in seen and value not in dup:
            dup.append(value)
        seen.add(value)
    return dup


# ---------------------------------------------------------------------------
# settings.yaml -> Settings
# ---------------------------------------------------------------------------


class ProviderConfig(BaseModel):
    """§13-D1: exactly three required fields (base_url / api_key /
    model_name) plus generic chat-parameters. No Azure-specific branch
    (ADR-001): anything non-OpenAI-compatible fronts itself behind
    ``base_url``."""

    model_config = MODEL_CONFIG

    base_url: HttpUrlStr
    api_key: SecretStr
    model_name: NonEmptyStr
    temperature: Annotated[float, Field(ge=0, le=2)] = 1.0
    top_p: Annotated[float, Field(gt=0, le=1)] = 1.0
    max_tokens: PositiveInt | None = None
    timeout_s: PositiveFloat = 120.0
    retries: Annotated[int, Field(ge=0, le=10)] = 2
    #: Allow-set validated against in §6 (`thread.model` must be a member).
    #: ``None`` means "just the default model" (see the validator below).
    allowed_models: list[NonEmptyStr] | None = None

    @field_validator("api_key")
    @classmethod
    def _api_key_non_empty(cls, v: SecretStr) -> SecretStr:
        # Message never echoes the value — secret discipline (§8.6).
        if not v.get_secret_value().strip():
            raise ValueError("must be a non-empty string")
        return v

    @model_validator(mode="after")
    def _default_model_is_allowed(self) -> Self:
        if (
            self.allowed_models is not None
            and self.model_name not in self.allowed_models
        ):
            raise ValueError(
                "allowed_models must contain the default "
                f"model_name {self.model_name!r}"
            )
        return self

    @property
    def effective_allowed_models(self) -> tuple[str, ...]:
        """The allow-set consumers (thread creation, snapshot ``validate``)
        check against, with the ``None`` case resolved."""
        if self.allowed_models is None:
            return (self.model_name,)
        return tuple(self.allowed_models)


class LimitsConfig(BaseModel):
    """§8.10 quota backstops — the hard gates that make open registration
    survivable with one global provider key (§13-D2)."""

    model_config = MODEL_CONFIG

    # per-run
    max_turns: PositiveInt = 50
    wall_clock_s: PositiveFloat = 1800.0
    # per-user (`users` columns are seeded from these at registration, §6)
    max_concurrent_runs: PositiveInt = 1
    per_user_daily_tokens: PositiveInt = 200_000
    # account-level circuit breakers (§8.10 global gate)
    global_hourly_tokens: PositiveInt = 2_000_000


class SandboxConfig(BaseModel):
    """ADR-005 execution guardrails. Defaults are the *strictest* posture
    (D8): no network at all. Build-time source overrides (``base_image``,
    ``pip_index_url``, ``apt_mirror``) exist because deployment networks
    differ, but they only matter once something can reach them (§12)."""

    model_config = MODEL_CONFIG

    memory: SizeSpecStr = "2g"
    cpus: PositiveFloat = 1.0
    pids_limit: PositiveInt = 256
    network_mode: NetworkModeInput = NetworkMode.NONE
    #: Sizing principle (recompute per deployment, §8.10b):
    #: ``max_live_containers × memory`` ≲ 1/3 of host RAM. T1.5 revalidates
    #: the shipped value against the actual host and config-check reports
    #: overshoot.
    max_live_containers: PositiveInt = 8
    idle_ttl_s: PositiveFloat = 900.0
    # Image provenance / build inputs (consumed by T6.3; the runtime image
    # reference itself is deployment-side, `SANDBOX_IMAGE` in core/env.py).
    image_tag: NonEmptyStr | None = None
    base_image: NonEmptyStr | None = None
    pip_index_url: HttpUrlStr | None = None
    apt_mirror: HttpUrlStr | None = None


class RegistrationConfig(BaseModel):
    """§8.11 open-registration guardrails (consumed by T3.3/T3.4)."""

    model_config = MODEL_CONFIG

    open: bool = True
    #: Requests per IP per hour.
    per_ip_rate: PositiveInt = 5
    #: Successful registrations per IP per day.
    per_ip_daily: PositiveInt = 10


class PermissionsDefaults(BaseModel):
    """The ``permissions`` block inside settings.yaml: which modes exist for
    threads (the per-file ``permissions.yaml`` default/rules come later);
    §6 `thread.permission_mode` must be a member of ``allowed_modes``."""

    model_config = MODEL_CONFIG

    default_mode: PermissionModeInput = PermissionMode.DEFAULT
    #: `bypass` is NOT in the default set — same posture as D8: the strictest
    #: stance is the default, and reaching bypass is an explicit, reviewable
    #: authoring act (add it here *and* keep permissions.yaml
    #: policy.disallow_bypass=false honest). Cross-file enforcement of
    #: disallow_bypass against this list happens at merge time (T1.4/T7.2).
    allowed_modes: list[PermissionModeInput] = Field(
        default_factory=lambda: [
            PermissionMode.DEFAULT,
            PermissionMode.PLAN,
            PermissionMode.ACCEPT_EDITS,
        ]
    )

    @model_validator(mode="after")
    def _default_is_allowed(self) -> Self:
        if self.default_mode not in self.allowed_modes:
            raise ValueError(
                f"default_mode {self.default_mode.value!r} must be listed in "
                "allowed_modes"
            )
        return self


class Settings(BaseModel):
    """``settings.yaml`` root (§5). Only ``provider`` is mandatory; every
    other section documents its conservative defaults and may be omitted."""

    model_config = MODEL_CONFIG

    provider: ProviderConfig
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    registration: RegistrationConfig = Field(default_factory=RegistrationConfig)
    permissions: PermissionsDefaults = Field(default_factory=PermissionsDefaults)


# ---------------------------------------------------------------------------
# tools.yaml -> ToolConfig
# ---------------------------------------------------------------------------

# Hard caps (§4 ToolSpec: tools.yaml may tighten, never loosen past these).
# Values *above* a cap validate successfully on purpose: T6.1's contract is
# to clamp them and emit a ConfigIssue warning, which requires the input to
# parse first — rejecting here would turn "clamped with warning" into
# "refused to boot".
TOOL_TIMEOUT_S_HARD_MAX: Final = 3600.0
TOOL_RESULT_BYTES_HARD_MAX: Final = 8 * 1024 * 1024


class ToolDefaults(BaseModel):
    """Baseline knobs every builtin tool inherits unless its entry overrides."""

    model_config = MODEL_CONFIG

    timeout_s: PositiveFloat = 60.0
    max_result_bytes: PositiveInt = 64 * 1024
    #: Cap on serialized tool arguments (the "参数上限" of §5). ``None`` =
    #: each tool's handler-side default.
    max_args_bytes: PositiveInt | None = None


class ToolEntry(BaseModel):
    """Per-tool override; every ``None`` falls through to ToolDefaults."""

    model_config = MODEL_CONFIG

    #: Schema default is permissive (True); *shipped policy* is set by the
    #: T1.5 config file, which disables write-class tools explicitly.
    enabled: bool = True
    timeout_s: PositiveFloat | None = None
    max_result_bytes: PositiveInt | None = None
    max_args_bytes: PositiveInt | None = None


class BashToolEntry(ToolEntry):
    """``hard_timeout_s`` is the ceiling a single exec may not exceed
    regardless of what the model asks for (§5 "bash 硬超时", enforced T6.6)."""

    model_config = MODEL_CONFIG

    hard_timeout_s: PositiveFloat = 300.0


class HttpFetchToolEntry(ToolEntry):
    """Host-side fetch (§8.4③: *not* governed by the sandbox network mode).
    Empty ``allowed_domains`` means deny-all — fail closed (T6.7)."""

    model_config = MODEL_CONFIG

    allowed_domains: list[DomainStr] = Field(default_factory=list)
    max_redirects: Annotated[int, Field(ge=0, le=20)] = 5
    max_response_bytes: PositiveInt = 1024 * 1024


class ToolCatalog(BaseModel):
    """Closed registry of builtin tool names — one field each, so
    ``extra="forbid"`` doubles as typo detection: a tool name not listed here
    cannot be configured into existence, and MCP/plugin tools live in their
    own files with their own namespaces (§4 ToolSpec.namespace).

    Names are the wire contract the model sees and permission patterns
    reference; adding one here means a tool implementation in M6/M8/M9/M10.
    """

    model_config = MODEL_CONFIG

    read_file: ToolEntry = Field(default_factory=ToolEntry)
    write_file: ToolEntry = Field(default_factory=ToolEntry)
    edit_file: ToolEntry = Field(default_factory=ToolEntry)
    delete_file: ToolEntry = Field(default_factory=ToolEntry)
    move_file: ToolEntry = Field(default_factory=ToolEntry)
    list_dir: ToolEntry = Field(default_factory=ToolEntry)
    glob: ToolEntry = Field(default_factory=ToolEntry)
    grep: ToolEntry = Field(default_factory=ToolEntry)
    bash: BashToolEntry = Field(default_factory=BashToolEntry)
    http_fetch: HttpFetchToolEntry = Field(default_factory=HttpFetchToolEntry)
    ask_user: ToolEntry = Field(default_factory=ToolEntry)
    skill_load: ToolEntry = Field(default_factory=ToolEntry)
    task: ToolEntry = Field(default_factory=ToolEntry)
    todo_write: ToolEntry = Field(default_factory=ToolEntry)
    enter_plan_mode: ToolEntry = Field(default_factory=ToolEntry)
    exit_plan_mode: ToolEntry = Field(default_factory=ToolEntry)


#: Derived from the catalog above — the single authoritative builtin tool
#: name set (referenced by skill/agent frontmatter validation, T9.4/T10.1).
BUILTIN_TOOL_NAMES: Final[frozenset[str]] = frozenset(ToolCatalog.model_fields)


class ToolConfig(BaseModel):
    """``tools.yaml`` root (§5)."""

    model_config = MODEL_CONFIG

    defaults: ToolDefaults = Field(default_factory=ToolDefaults)
    tools: ToolCatalog = Field(default_factory=ToolCatalog)


# ---------------------------------------------------------------------------
# permissions.yaml -> PermissionsConfig
# ---------------------------------------------------------------------------


class PermissionRule(BaseModel):
    """One allow/ask/deny rule. ``pattern`` is the ADR-009 grammar
    (``ToolName``, ``ToolName(principal*)``, ``mcp__srv__*``); its syntax is
    validated by the engine (T7.1) — here it is just a non-empty string."""

    model_config = MODEL_CONFIG

    effect: PermissionEffectInput
    pattern: NonEmptyStr
    note: NonEmptyStr | None = None
    #: §5 authorable provenance hint; the loader (T1.2) may stamp
    #: ``file:index`` for PermissionResult.rule_source (§4).
    source: NonEmptyStr | None = None


class PermissionsPolicy(BaseModel):
    """Operator-declared hard constraints. Plugins may never override this
    block, and never express ``bypass`` (§4 merge rules)."""

    model_config = MODEL_CONFIG

    disallow_bypass: bool = False


class PermissionsConfig(BaseModel):
    """``permissions.yaml`` root (§5, ADR-009)."""

    model_config = MODEL_CONFIG

    default_mode: PermissionModeInput = PermissionMode.DEFAULT
    policy: PermissionsPolicy = Field(default_factory=PermissionsPolicy)
    #: Per-mode extra rules layered on the built-in preset (T7.2).
    mode_presets: dict[PermissionModeInput, list[PermissionRule]] = Field(
        default_factory=dict
    )
    rules: list[PermissionRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _bypass_policy(self) -> Self:
        # Same-file check: settings.yaml `permissions.allowed_modes`
        # excluding bypass is the cross-file half (resolved at merge, T1.4).
        if self.policy.disallow_bypass and self.default_mode is PermissionMode.BYPASS:
            raise ValueError("default_mode 'bypass' contradicts policy.disallow_bypass")
        return self


# ---------------------------------------------------------------------------
# hooks.yaml -> HookConfig
# ---------------------------------------------------------------------------


class HookEntry(BaseModel):
    """One hook binding (ADR-010). ``matcher`` selects tool names (``*`` =
    all); semantics in T12.1. Path/permission checks on ``target`` belong to
    T1.7; existence to the loader."""

    model_config = MODEL_CONFIG

    event: HookEventInput
    matcher: NonEmptyStr = "*"
    kind: HookKindInput
    #: kind=command -> repo-relative script (argv-direct, never via shell);
    #: kind=python -> ``module:attr`` callable, trusted plugins only.
    target: NonEmptyStr
    timeout_ms: Annotated[int, Field(ge=100, le=600_000)] = 10_000
    on_timeout: HookTimeoutActionInput = HookTimeoutAction.BLOCK
    enabled: bool = True

    @model_validator(mode="after")
    def _target_shape(self) -> Self:
        if self.kind is HookKind.PYTHON and ":" not in self.target:
            raise ValueError("python hooks must target a callable as 'module:attr'")
        return self


class HookConfig(BaseModel):
    """``hooks.yaml`` root (§5). An empty list is the normal case and must
    cost zero at runtime (T12.1)."""

    model_config = MODEL_CONFIG

    hooks: list[HookEntry] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# mcp.yaml -> MCPConfig
# ---------------------------------------------------------------------------


class MCPServerConfig(BaseModel):
    """One MCP server entry (ADR-007: roster in ``mcp.yaml``; the tool
    catalog itself is memory-only). ``env``/``headers`` values are
    ``SecretStr`` — resolved ``${env:}`` contents never ride reprs into
    logs, and ``GET /mcp/status`` must not echo them or their names (§7)."""

    model_config = MODEL_CONFIG

    name: SlugStr
    transport: MCPTransportInput
    # transport=stdio
    command: NonEmptyStr | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[NonEmptyStr, SecretStr] = Field(default_factory=dict)
    # transport=streamable_http
    url: HttpUrlStr | None = None
    headers: dict[NonEmptyStr, SecretStr] = Field(default_factory=dict)
    # common
    #: Default *true*: a freshly configured server asks before it acts.
    requires_approval: bool = True
    enabled: bool = True
    #: ``None`` falls through to MCPConfig.connect_timeout_s.
    connect_timeout_s: PositiveFloat | None = None

    @model_validator(mode="after")
    def _transport_fields(self) -> Self:
        if self.transport is MCPTransport.STDIO:
            if self.command is None:
                raise ValueError("stdio servers require `command`")
            if self.url is not None or self.headers:
                raise ValueError(
                    "stdio servers must not set url/headers "
                    "(that is streamable_http's shape)"
                )
        else:
            if self.url is None:
                raise ValueError("streamable_http servers require `url`")
            if self.command is not None or self.args or self.env:
                raise ValueError(
                    "streamable_http servers must not set command/args/env "
                    "(that is stdio's shape)"
                )
        return self


class MCPConfig(BaseModel):
    """``mcp.yaml`` root (§5)."""

    model_config = MODEL_CONFIG

    servers: list[MCPServerConfig] = Field(default_factory=list)
    connect_timeout_s: PositiveFloat = 30.0

    @model_validator(mode="after")
    def _unique_names(self) -> Self:
        dup = _duplicates([s.name for s in self.servers])
        if dup:
            raise ValueError(f"duplicate MCP server name(s): {', '.join(dup)}")
        return self


# ---------------------------------------------------------------------------
# plugins.yaml -> PluginsConfig
# ---------------------------------------------------------------------------


class PluginEntry(BaseModel):
    """One plugin binding (ADR-011). ``enabled`` assembles it; only
    ``trusted`` may register in-process Python entry points — and both are
    review-gated file contents, never runtime toggles (§1.2)."""

    model_config = MODEL_CONFIG

    name: SlugStr
    #: Repo-relative directory (resolved against REPO_ROOT by the loader,
    #: which also proves existence; §5 forbids escaping config/).
    path: NonEmptyStr
    enabled: bool = True
    trusted: bool = False

    @field_validator("path")
    @classmethod
    def _repo_relative(cls, v: str) -> str:
        # Shape-only guard; traversal *resolution* is the loader's (§5).
        # POSIX spelling on purpose (Linux deploys) — a backslash is not a
        # separator here, but `C:\...`-shaped values are always mistakes.
        parts = PurePosixPath(v).parts
        if "\\" in v:
            raise ValueError("must be a POSIX repo-relative path")
        if not parts or v.startswith("/") or ".." in parts:
            raise ValueError("must be a repo-relative path without '..'")
        return v


class PluginsConfig(BaseModel):
    """``plugins.yaml`` root (§5)."""

    model_config = MODEL_CONFIG

    plugins: list[PluginEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_names(self) -> Self:
        dup = _duplicates([p.name for p in self.plugins])
        if dup:
            raise ValueError(f"duplicate plugin name(s): {', '.join(dup)}")
        return self


# ---------------------------------------------------------------------------
# Shared frontmatter models (skills/ and agents/; ADR-008)
# ---------------------------------------------------------------------------


class SkillFrontmatter(BaseModel):
    """YAML frontmatter of ``config/skills/<slug>/SKILL.md`` (§5; parsing and
    slug-vs-directory checks land in T9.1). The system prompt carries only
    name/description/when_to_use — the body is loaded on demand
    (progressive disclosure, T9.3)."""

    model_config = MODEL_CONFIG

    name: SlugStr
    description: DescriptionStr
    when_to_use: (
        Annotated[
            str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2048)
        ]
        | None
    ) = None
    #: ``None`` = inherits the snapshot's enabled set; a list narrows it.
    #: Membership is checked against enabled tools at validate-time (T9.4) —
    #: frontmatter can only *reduce* permissions, never grant (§8.5).
    allowed_tools: list[NonEmptyStr] | None = None
    model: NonEmptyStr | None = None
    version: NonEmptyStr | None = None


class AgentFrontmatter(BaseModel):
    """YAML frontmatter of ``config/agents/<name>.md`` (§5; consumed by
    T10.1). Body is the subagent's system prompt. Omitted fields inherit
    from settings (T10.1 resolves)."""

    model_config = MODEL_CONFIG

    name: SlugStr
    description: DescriptionStr
    #: Allowlist (``None`` = inherit) and denylist — mutually exclusive:
    #: "everything except X" and "only X together" do not compose.
    tools: list[NonEmptyStr] | None = None
    exclude_tools: list[NonEmptyStr] | None = None
    model: NonEmptyStr | None = None
    max_turns: PositiveInt | None = None
    #: §8 allowed_modes / policy membership is cross-checked at snapshot
    #: build (T10.1/T1.4), same as thread modes.
    permission_mode: PermissionModeInput | None = None
    timeout_s: PositiveFloat | None = None

    @model_validator(mode="after")
    def _toolset_shape(self) -> Self:
        if self.tools is not None and self.exclude_tools is not None:
            raise ValueError("set `tools` or `exclude_tools`, not both")
        return self
