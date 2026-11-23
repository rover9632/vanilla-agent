"""Merge layers: base documents + plugin contributions + provenance (T1.4).

The assembly order (plan §4 "合并优先级"): the six base files (with any
``*.local.yaml`` replacement already applied by the loader) form the
operator layer; enabled plugins then *contribute* additional resources.
Value-shaped settings are never merged field-wise (replacement only —
``loader.load_local_overlays``); the additive resources are merged here
with union semantics:

- **permission rules**: union, operator first then plugins in roster order.
  Each merged rule keeps an :class:`Origin` (source label + its index
  inside that source) — §4's ``PermissionResult.rule`` "points at config
  file + ordinal" and §6's ``tool_call.rule_source`` both read this.
  Plugins cannot express ``policy``/``default_mode`` at all:
  :class:`PluginContribution` has no such field, so "plugins must not
  declare bypass, must not override the operator policy" (§4) is enforced
  by the type, not by runtime review.
- **hooks / MCP servers**: union as well, each entry origin-aligned.
- **name conflicts across sources are errors, never silent overrides**
  (§4's four families): skill slug, tool name, MCP server name, hook
  matcher (the ``(event, matcher)`` pair). *Within one source* repeats are
  legitimate (two guards on ``PreToolUse/bash`` in the operator file);
  *across sources* the second declarer is refused — the offending entry is
  not merged (keeping the union constructible) and every collision is
  reported in one pass.
- contributions from an unregistered plugin are an error (register it in
  ``plugins.yaml`` or drop the producer); from a *disabled* plugin, a
  warning and a skip (the roster, not the producer, is the enable switch).

T9/T10 extend the skill/agent collision space with the operator-side
directory scans — the owner maps below are where those names get reserved.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.config.issue import ConfigIssue
from app.config.loader import display_name, path_within, resolve_repo_relative
from app.config.schema import (
    BUILTIN_TOOL_NAMES,
    HookConfig,
    HookEntry,
    HookEvent,
    HookKind,
    MCPConfig,
    MCPServerConfig,
    PermissionRule,
    PermissionsConfig,
    PluginsConfig,
)

# ---------------------------------------------------------------------------
# Core value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Origin:
    """Where a merged entry was authored: source label + position in it.

    Operator entries label as the display path of the operator document
    that actually won (base file or its local replacement); plugin entries
    label as ``plugin:<name>``.
    """

    source: str
    index: int

    @property
    def label(self) -> str:
        return f"{self.source}#{self.index}"


def plugin_label(name: str) -> str:
    """Origin source label for contributions of one plugin."""
    return f"plugin:{name}"


@dataclass(frozen=True)
class PluginContribution:
    """Everything one plugin adds to the assembly.

    Produced by T12.5's registrar (manifest + parsed sub-documents); T1.4
    defines the consumption contract and its tests drive it directly. The
    type carries NO policy/default_mode fields and no whole-file override
    slots — per the module docstring, that absence *is* §4's enforcement
    that plugins cannot widen permissions policy or claim bypass.
    """

    plugin: str
    permission_rules: tuple[PermissionRule, ...] = ()
    hooks: tuple[HookEntry, ...] = ()
    mcp_servers: tuple[MCPServerConfig, ...] = ()
    tools: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    agents: tuple[str, ...] = ()


@dataclass(frozen=True)
class MergeOutcome:
    """Merged views plus aligned provenance plus findings.

    Each ``*_origins`` tuple aligns 1:1 (by index) with the corresponding
    merged entries inside the three models; ``ConfigSnapshot.__post_init__``
    re-checks the alignment so no construction path can drift it.
    """

    permissions: PermissionsConfig
    hooks: HookConfig
    mcp: MCPConfig
    rule_origins: tuple[Origin, ...]
    hook_origins: tuple[Origin, ...]
    mcp_origins: tuple[Origin, ...]
    issues: tuple[ConfigIssue, ...]


# ---------------------------------------------------------------------------
# Ownership helpers (§4 cross-source uniqueness)
# ---------------------------------------------------------------------------

_HookKey = tuple[HookEvent, str]


def _claim(
    reserved: MutableMapping[str, str],
    name: str,
    owner: str,
    *,
    kind: str,
    where: str,
    path: str,
    issues: list[ConfigIssue],
) -> bool:
    """Reserve *name* for *owner*; False (plus an error) when another
    source already holds it. Same-source repeats are the source's own
    business and simply re-confirm ownership."""
    prior = reserved.get(name)
    if prior is not None and prior != owner:
        issues.append(
            ConfigIssue.error(
                where,
                path,
                f"{kind} {name!r} is already declared by {prior!r}; same-name "
                "declarations across sources are refused — no silent override "
                "(plan §4)",
            )
        )
        return False
    reserved[name] = owner
    return True


# ---------------------------------------------------------------------------
# The merge
# ---------------------------------------------------------------------------


def merge_contributions(
    *,
    permissions: PermissionsConfig,
    hooks: HookConfig,
    mcp: MCPConfig,
    plugins: PluginsConfig,
    contributions: Sequence[PluginContribution],
    displays: Mapping[str, str],
    repo_root: Path,
    config_dir: Path,
) -> MergeOutcome:
    """Union the operator layer with enabled plugin contributions.

    ``displays`` maps each stem to the display path of the operator
    document that won (base or local replacement) — origins label by it.
    Never raises: every problem becomes an issue, and the caller refuses
    snapshot assembly when any error is present.
    """
    issues: list[ConfigIssue] = []
    perms_display = displays.get("permissions", "config/permissions.yaml")
    hooks_display = displays.get("hooks", "config/hooks.yaml")
    mcp_display = displays.get("mcp", "config/mcp.yaml")
    plugins_display = displays.get("plugins", "config/plugins.yaml")

    # -- roster gate: contributions only from registered, enabled plugins --
    roster = {p.name: p for p in plugins.plugins}
    active: list[PluginContribution] = []
    for contribution in contributions:
        entry = roster.get(contribution.plugin)
        if entry is None:
            issues.append(
                ConfigIssue.error(
                    plugins_display,
                    "",
                    f"contributions from plugin {contribution.plugin!r} that "
                    "plugins.yaml does not register — register it or drop the "
                    "producer (no unlisted capability)",
                )
            )
        elif not entry.enabled:
            issues.append(
                ConfigIssue.warning(
                    plugins_display,
                    "",
                    f"plugin {contribution.plugin!r} is disabled — its "
                    "contributions are skipped",
                )
            )
        else:
            active.append(contribution)

    # -- permissions: plain union, origins follow the same append order --
    rules: list[PermissionRule] = list(permissions.rules)
    rule_origins: list[Origin] = [Origin(perms_display, i) for i in range(len(rules))]
    for contribution in active:
        label = plugin_label(contribution.plugin)
        for index, rule in enumerate(contribution.permission_rules):
            rules.append(rule)
            rule_origins.append(Origin(label, index))

    # -- hooks: union + (event, matcher) ownership + target containment --
    hook_entries: list[HookEntry] = list(hooks.hooks)
    hook_origins: list[Origin] = [
        Origin(hooks_display, i) for i in range(len(hook_entries))
    ]
    hook_owners: dict[_HookKey, str] = {}
    for entry in hook_entries:
        hook_owners.setdefault((entry.event, entry.matcher), hooks_display)
    for contribution in active:
        label = plugin_label(contribution.plugin)
        for index, entry in enumerate(contribution.hooks):
            key = (entry.event, entry.matcher)
            prior = hook_owners.get(key)
            if prior is not None and prior != label:
                issues.append(
                    ConfigIssue.error(
                        label,
                        f"hooks[{index}].matcher",
                        f"hook matcher {entry.matcher!r} on event "
                        f"{entry.event.value!r} is already declared by "
                        f"{prior!r}; hook matchers must be unique across "
                        "sources — no silent override (plan §4)",
                    )
                )
                continue
            hook_owners[key] = label
            if entry.kind is HookKind.COMMAND and not path_within(
                config_dir, resolve_repo_relative(repo_root, entry.target)
            ):
                issues.append(
                    ConfigIssue.error(
                        label,
                        f"hooks[{index}].target",
                        f"plugin hook script {entry.target!r} resolves outside "
                        "the config directory (plan §5)",
                    )
                )
            hook_entries.append(entry)
            hook_origins.append(Origin(label, index))

    # -- mcp: union + server-name ownership -------------------------------
    servers: list[MCPServerConfig] = list(mcp.servers)
    mcp_origins: list[Origin] = [Origin(mcp_display, i) for i in range(len(servers))]
    server_names: dict[str, str] = {s.name: mcp_display for s in servers}
    for contribution in active:
        label = plugin_label(contribution.plugin)
        for index, server in enumerate(contribution.mcp_servers):
            claimed = _claim(
                server_names,
                server.name,
                label,
                kind="MCP server name",
                where=label,
                path=f"mcp_servers[{index}].name",
                issues=issues,
            )
            if not claimed:
                continue  # merged roster keeps the first declarer's server
            servers.append(server)
            mcp_origins.append(Origin(label, index))

    # -- tools / skills / agents: cross-source name reservation -----------
    # (T1.4 checks only; the resources themselves materialize in T12/T9/T10.
    # Builtin tool names are permanently reserved.)
    tool_owner: dict[str, str] = {n: "builtin" for n in BUILTIN_TOOL_NAMES}
    skill_owner: dict[str, str] = {}
    agent_owner: dict[str, str] = {}
    for contribution in active:
        label = plugin_label(contribution.plugin)
        for family, names, reserved, path in (
            ("tool name", contribution.tools, tool_owner, "tools"),
            ("skill slug", contribution.skills, skill_owner, "skills"),
            ("agent name", contribution.agents, agent_owner, "agents"),
        ):
            for index, name in enumerate(names):
                _claim(
                    reserved,
                    name,
                    label,
                    kind=family,
                    where=label,
                    path=f"{path}[{index}]",
                    issues=issues,
                )

    return MergeOutcome(
        permissions=PermissionsConfig(
            default_mode=permissions.default_mode,
            policy=permissions.policy,
            mode_presets=permissions.mode_presets,
            rules=rules,
        ),
        hooks=HookConfig(hooks=hook_entries),
        mcp=MCPConfig(servers=servers, connect_timeout_s=mcp.connect_timeout_s),
        rule_origins=tuple(rule_origins),
        hook_origins=tuple(hook_origins),
        mcp_origins=tuple(mcp_origins),
        issues=tuple(issues),
    )


def operator_displays(
    config_dir: Path, repo_root: Path, overridden: Mapping[str, object]
) -> dict[str, str]:
    """Display paths of the operator-layer documents that won per stem.

    Origins for operator entries must name the file that was actually used:
    ``<stem>.local.yaml`` when it replaced the base document, the base file
    otherwise (§4's rule_source honesty).
    """
    displays: dict[str, str] = {}
    for stem in ("permissions", "hooks", "mcp", "plugins", "settings", "tools"):
        filename = f"{stem}.local.yaml" if stem in overridden else f"{stem}.yaml"
        displays[stem] = display_name(config_dir / filename, config_dir, repo_root)
    return displays
