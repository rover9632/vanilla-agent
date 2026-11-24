"""config-check: the CI-style audit over the whole config tree (T1.6).

Layered on the load/merge/assemble pipeline (everything T1.2–T1.4 already
refuses at startup is reported here too, exceptions and all), this module
adds the checks that are CHECK-time policy rather than boot-time schema
(docs/plan.md T1.6, §5/§8.6/§8.10/§8.11):

- **plaintext-secret scan** — raw text of EVERY file under ``config/``:
  roster docs, skills, agents, plugins AND untracked ``*.local.yaml`` —
  §8.6 explicitly warns that skipping the ignored files turns "scan
  passed" into false comfort. Patterns: ``sk-``-shaped keys, PEM
  private-key headers, JWTs; long-base64 runs are scanned in YAML only
  (prompt bodies may quote data URIs; config values may not). Escape
  hatch: a line carrying ``config-check:ignore`` is skipped — greppable
  and rare by policy.
- **reference integrity** — declared things must exist and be live:
  skill/agent frontmatter parses (strict T1.1 models) with name matching
  the directory/file; skill ``allowed_tools`` must reference ENABLED
  builtin tools or registered MCP servers (§3's "skill 引用了被关掉的
  工具" CI example — narrowing, never widening); agent tool/model/
  permission_mode inside the allowed sets (T10.1's rules, at check time);
  permission-rule patterns name known tools/servers on their literal
  segments; plugin directories exist (manifest depth stays T12.4);
  command hook scripts exist (executability/world-writable stay T1.7).
- **§8.11 listen gate** — ``registration.open`` with a non-loopback
  ``LISTEN_ADDR`` is an ERROR; open registration with the listen address
  undeclared is a WARNING — the check never guesses the bind (the T13.6
  systemd unit exports LISTEN_ADDR next to ``uvicorn --host``).
- **§8.10b sizing** — ``max_live_containers × sandbox.memory`` against
  1/3 of host RAM: WARNING on overshoot or unresolvable RAM (CI machines
  are not deployment machines; this is deliberately not an error).
- **unknown files** — anything else directly in ``config/`` warns,
  including the ``*.local.yml`` near-miss suffix.
- **``.env`` near-miss typos** (T0.2 review leftover 5): unknown dotenv
  keys are tolerated — legit extras (``SECRET_*``, proxies) must keep
  working — but a key closely resembling a whitelisted deployment
  variable is an error: a silent typo there means a knob nobody set.

Unset ``${env:}`` references are substituted with a placeholder during
the audit (each substitution is itself a warning naming the variable):
config-check must run on machines with no secrets, while the real
startup path (``load_config``) keeps refusing without them — the warning
says exactly that.

Everything environment-shaped is a parameter with a production default
(environs, dotenv keys, host RAM), so the engine tests are hermetic.
The module never mutates files and never prints secret values.
"""

from __future__ import annotations

import difflib
import os
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

import yaml
from pydantic import ValidationError

from app.config.issue import ConfigIssue, loc_to_path
from app.config.loader import (
    CONFIG_FILES,
    LOCAL_SUFFIX,
    display_name,
    env_references_in,
    load_config_files,
    load_local_overlays,
    resolve_repo_relative,
)
from app.config.schema import (
    BUILTIN_TOOL_NAMES,
    AgentFrontmatter,
    HookKind,
    SandboxConfig,
    SkillFrontmatter,
    ToolCatalog,
)
from app.config.snapshot import ConfigLoadError, ConfigSnapshot, assemble_snapshot
from app.config.trust import check_config_trust
from app.core.env import DEPLOY_ENV_VARS, default_dotenv_path, read_dotenv

#: Stand-in value for unset env references during the audit ONLY.
CHECK_PLACEHOLDER: Final = "config-check://placeholder-not-a-real-secret"

_IGNORE_MARKER: Final = "config-check:ignore"

_SECRET_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("sk-style key", re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_\-]{8,}")),
    ("PEM private-key header", re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{5,}")),
)
_LONG_B64: Final = re.compile(r"[A-Za-z0-9+/]{48,}={0,2}")

_LOOPBACK_PREFIX: Final = "127."
_LOOPBACK_HOSTS: Final = frozenset({"localhost", "::1"})

#: Names allowed to live directly inside ``config/`` (§5 roster + resource
#: dirs + the template-ish extras we ship).
_KNOWN_TOPLEVEL: Final[frozenset[str]] = frozenset(
    filename for filename, _, _ in CONFIG_FILES
) | frozenset(
    {
        "README.md",
        "skills",
        "agents",
        "plugins",
        # command-hook scripts live under config/hooks/ (their
        # targets must stay inside config/ — §5 — and this is the
        # spelling config/README.md documents)
        "hooks",
        ".gitkeep",
    }
)


# ---------------------------------------------------------------------------
# Production defaults for the injected environment probes
# ---------------------------------------------------------------------------


def host_total_memory_bytes() -> int | None:
    """MemTotal in bytes, or None when the host does not report it."""
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024  # kB
    except (OSError, ValueError, IndexError):
        return None
    return None


def default_dotenv_key_audit() -> list[str]:
    """Key names of the repo-root dotenv (values never leave this fn)."""
    return sorted(read_dotenv(default_dotenv_path()))


# ---------------------------------------------------------------------------
# Small shared parsers
# ---------------------------------------------------------------------------


def _mem_spec_bytes(spec: str) -> int:
    match = re.fullmatch(r"(\d+)([kmgtKMGT]?)b?", spec)
    if not match:
        raise ValueError(f"unparsable size spec {spec!r}")
    mult = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}[
        match.group(2).lower()
    ]
    return int(match.group(1)) * mult


def _listen_host(addr: str) -> str:
    host = addr.strip()
    if host.startswith("["):  # [::1]:8080
        end = host.find("]")
        host = host[1:end] if end > 0 else host
    elif host.count(":") == 1:  # host:port (bare IPv6 has 2+ colons)
        host = host.split(":", 1)[0]
    return host.rstrip(".").lower()


def _is_loopback_host(host: str) -> bool:
    return host in _LOOPBACK_HOSTS or host.startswith(_LOOPBACK_PREFIX)


def _frontmatter(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Split ``---\\n<yaml>\\n---\\n``; returns (data, error-message)."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"cannot read resource file ({type(exc).__name__})"
    if not text.startswith("---\n"):
        return None, "missing YAML frontmatter (file must start with '---')"
    end = text.find("\n---\n", 3)
    if end == -1:
        return None, "unterminated YAML frontmatter"
    try:
        data = yaml.safe_load(text[3:end])
    except yaml.YAMLError as exc:
        return None, f"frontmatter YAML is invalid: {exc}"
    if not isinstance(data, dict):
        return None, "frontmatter must be a mapping"
    return data, None


def _model_issues(exc: ValidationError, source: str) -> list[ConfigIssue]:
    return [
        ConfigIssue.error(source, loc_to_path(tuple(err["loc"])), str(err["msg"]))
        for err in exc.errors(include_url=False)
    ]


def dedupe(issues: Sequence[ConfigIssue]) -> list[ConfigIssue]:
    """Stable-order uniqueness (stage lists overlap once warnings flow
    through ``snapshot.issues`` as well)."""
    seen: set[tuple[str, str, str, str]] = set()
    out: list[ConfigIssue] = []
    for issue in issues:
        key = (issue.level, issue.source, issue.path, issue.message)
        if key not in seen:
            seen.add(key)
            out.append(issue)
    return out


# ---------------------------------------------------------------------------
# Resource scanning (shared with `python -m app.config dump`)
# ---------------------------------------------------------------------------


def scan_resources(
    config_dir: Path, *, repo_root: Path
) -> tuple[list[dict[str, Any]], list[ConfigIssue]]:
    """Parse skill/agent frontmatter into dump-ready rows + findings.

    The full parser lands in T9.1/T10.1; this is the check-time subset:
    strict model validation + naming rules, sharing the T1.1 frontmatter
    models so grammar drift is impossible.
    """
    rows: list[dict[str, Any]] = []
    issues: list[ConfigIssue] = []

    skills_dir = config_dir / "skills"
    for path in sorted(skills_dir.glob("*/SKILL.md")) if skills_dir.is_dir() else []:
        slug = path.parent.name
        display = display_name(path, config_dir, repo_root)
        data, err = _frontmatter(path)
        if data is None:
            issues.append(ConfigIssue.error(display, "", err or "unparsed"))
            continue
        try:
            skill = SkillFrontmatter.model_validate(data)
        except ValidationError as exc:
            issues.extend(_model_issues(exc, display))
            continue
        if skill.name != slug:
            issues.append(
                ConfigIssue.error(
                    display,
                    "name",
                    f"frontmatter name {skill.name!r} does not match the "
                    f"directory slug {slug!r} (T9.1 rule)",
                )
            )
        rows.append(
            {
                "kind": "skill",
                "slug": slug,
                "source": display,
                "name": skill.name,
                "description": skill.description,
                "version": skill.version,
                "model": skill.model,
                "allowed_tools": skill.allowed_tools,
            }
        )

    agents_dir = config_dir / "agents"
    for path in sorted(agents_dir.glob("*.md")) if agents_dir.is_dir() else []:
        stem = path.stem
        display = display_name(path, config_dir, repo_root)
        data, err = _frontmatter(path)
        if data is None:
            issues.append(ConfigIssue.error(display, "", err or "unparsed"))
            continue
        try:
            agent = AgentFrontmatter.model_validate(data)
        except ValidationError as exc:
            issues.extend(_model_issues(exc, display))
            continue
        if agent.name != stem:
            issues.append(
                ConfigIssue.error(
                    display,
                    "name",
                    f"frontmatter name {agent.name!r} does not match the file "
                    f"name {stem!r}",
                )
            )
        rows.append(
            {
                "kind": "agent",
                "slug": stem,
                "source": display,
                "name": agent.name,
                "description": agent.description,
                "tools": agent.tools,
                "exclude_tools": agent.exclude_tools,
                "model": agent.model,
                "permission_mode": agent.permission_mode.value
                if agent.permission_mode
                else None,
            }
        )
    return rows, issues


# ---------------------------------------------------------------------------
# Reference integrity
# ---------------------------------------------------------------------------


def _check_tool_reference(
    ref: str,
    *,
    where: str,
    path: str,
    enabled: set[str],
    servers: Mapping[str, bool],
    must_be_enabled: bool,
    issues: list[ConfigIssue],
) -> None:
    if "__" in ref:
        parts = ref.split("__")
        server = parts[1] if parts[0] == "mcp" and len(parts) >= 3 else None
        if server is None:
            issues.append(
                ConfigIssue.error(
                    where,
                    path,
                    f"malformed MCP tool reference {ref!r} "
                    "(expected mcp__<server>__<tool>)",
                )
            )
        elif server == "*":
            if not servers:
                issues.append(
                    ConfigIssue.error(
                        where,
                        path,
                        f"{ref!r} opens the MCP namespace but no server is "
                        "registered (config/mcp.yaml)",
                    )
                )
        elif server not in servers:
            issues.append(
                ConfigIssue.error(
                    where,
                    path,
                    f"{ref!r} references MCP server {server!r} that "
                    "config/mcp.yaml does not register",
                )
            )
        elif must_be_enabled and not servers[server]:
            issues.append(
                ConfigIssue.warning(
                    where,
                    path,
                    f"{ref!r} references MCP server {server!r} that is disabled "
                    "— the tool cannot exist at runtime",
                )
            )
        return
    if ref not in BUILTIN_TOOL_NAMES:
        issues.append(
            ConfigIssue.error(
                where,
                path,
                f"{ref!r} is neither a builtin tool nor mcp-namespaced — "
                "unknown tool reference (plugin-contributed tools land with "
                "T12.5 and will extend this namespace)",
            )
        )
    elif must_be_enabled and ref not in enabled:
        issues.append(
            ConfigIssue.error(
                where,
                path,
                f"{ref!r} references a tool that config/tools.yaml disables — "
                "skills may narrow the surface, never widen it (§3 CI example, "
                "T9.4)",
            )
        )


def _reference_issues(
    snapshot: ConfigSnapshot, *, config_dir: Path, repo_root: Path
) -> list[ConfigIssue]:
    issues: list[ConfigIssue] = []
    catalog: ToolCatalog = snapshot.tools.tools
    enabled = {n for n in BUILTIN_TOOL_NAMES if getattr(catalog, n).enabled}
    servers = {s.name: s.enabled for s in snapshot.mcp.servers}

    rows, resource_issues = scan_resources(config_dir, repo_root=repo_root)
    issues.extend(resource_issues)

    allowed_modes = [m.value for m in snapshot.settings.permissions.allowed_modes]
    for row in rows:
        source = row["source"]
        if row["kind"] == "skill":
            for ref in row["allowed_tools"] or ():
                _check_tool_reference(
                    ref,
                    where=source,
                    path="allowed_tools",
                    enabled=enabled,
                    servers=servers,
                    must_be_enabled=True,
                    issues=issues,
                )
            if row["model"] is not None and row["model"] not in (
                snapshot.settings.provider.effective_allowed_models
            ):
                issues.append(
                    ConfigIssue.error(
                        source,
                        "model",
                        f"skill model {row['model']!r} is not in provider."
                        "allowed_models",
                    )
                )
        else:
            refs = [*(row["tools"] or ()), *(row["exclude_tools"] or ())]
            for ref in refs:
                _check_tool_reference(
                    ref,
                    where=source,
                    path="toolset",
                    enabled=enabled,
                    servers=servers,
                    must_be_enabled=False,
                    issues=issues,
                )
            if row["model"] is not None and row["model"] not in (
                snapshot.settings.provider.effective_allowed_models
            ):
                issues.append(
                    ConfigIssue.error(
                        source,
                        "model",
                        f"agent model {row['model']!r} is not in provider."
                        "allowed_models (T10.1 rule at check time)",
                    )
                )
            if (
                row["permission_mode"] is not None
                and row["permission_mode"] not in allowed_modes
            ):
                issues.append(
                    ConfigIssue.error(
                        source,
                        "permission_mode",
                        f"agent permission_mode {row['permission_mode']!r} is "
                        "not in settings.permissions.allowed_modes",
                    )
                )

    def _walk_rules(rules: Sequence[Any], source: str) -> None:
        for index, rule in enumerate(rules):
            base = rule.pattern.split("(", 1)[0].strip()
            if not base:
                continue
            if base.startswith("mcp__"):
                pass  # the server segment is literal even if the tool
                # part wildcards — the check tolerates "*" in tool slot
            elif "*" in base:
                continue  # wildcard grammar is T7.1's; literal names only
            _check_tool_reference(
                base,
                where=source,
                path=f"rules[{index}].pattern",
                enabled=enabled,
                servers=servers,
                must_be_enabled=False,
                issues=issues,
            )

    perms_source = snapshot.source_for("permissions.yaml")
    _walk_rules(snapshot.permissions.rules, perms_source)
    for preset_rules in snapshot.permissions.mode_presets.values():
        _walk_rules(preset_rules, perms_source)

    plugins_source = snapshot.source_for("plugins.yaml")
    for index, plugin in enumerate(snapshot.plugins.plugins):
        resolved = Path(resolve_repo_relative(repo_root, plugin.path))
        if not resolved.is_dir():
            issues.append(
                ConfigIssue.error(
                    plugins_source,
                    f"plugins[{index}].path",
                    f"plugin path {plugin.path!r} does not exist — reference "
                    "integrity (manifest/content checks land with T12.4)",
                )
            )
        elif not (resolved / ".vanilla-plugin" / "plugin.json").is_file():
            issues.append(
                ConfigIssue.warning(
                    plugins_source,
                    f"plugins[{index}].path",
                    f"plugin {plugin.name!r} has no .vanilla-plugin/plugin.json "
                    "yet (T12.4 will validate manifests)",
                )
            )

    hooks_source = snapshot.source_for("hooks.yaml")
    for index, hook in enumerate(snapshot.hooks.hooks):
        if hook.kind is not HookKind.COMMAND:
            continue
        script = Path(resolve_repo_relative(repo_root, hook.target))
        if not script.is_file():
            issues.append(
                ConfigIssue.error(
                    hooks_source,
                    f"hooks[{index}].target",
                    f"hook script {hook.target!r} does not exist "
                    "(executability and world-writable checks land with T1.7)",
                )
            )
    return issues


# ---------------------------------------------------------------------------
# File-level scans
# ---------------------------------------------------------------------------


def _secret_scan(config_dir: Path, *, repo_root: Path) -> list[ConfigIssue]:
    issues: list[ConfigIssue] = []
    for path in sorted(config_dir.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # unreadable/binary: the loader pipeline reports it
        is_yaml = path.suffix in {".yaml", ".yml"}
        display = display_name(path, config_dir, repo_root)
        for number, line in enumerate(text.splitlines(), start=1):
            if _IGNORE_MARKER in line:
                continue
            hits = [label for label, pat in _SECRET_PATTERNS if pat.search(line)]
            if is_yaml and _LONG_B64.search(line):
                hits.append("long base64 run")
            for label in hits:
                issues.append(
                    ConfigIssue.error(
                        f"{display}:{number}",
                        "",
                        f"plaintext-secret scan hit ({label}) — move the value "
                        "behind ${env:VAR}; the scan covers *.local.yaml too, "
                        "so 'passed' is never false comfort (§8.6)",
                    )
                )
    return issues


def _unknown_files(config_dir: Path) -> list[ConfigIssue]:
    issues: list[ConfigIssue] = []
    if not config_dir.is_dir():
        return issues
    for entry in sorted(config_dir.iterdir()):
        name = entry.name
        if name in _KNOWN_TOPLEVEL or name.startswith("."):
            continue
        if name.endswith(LOCAL_SUFFIX):
            continue
        if name.endswith(".local.yml"):
            issues.append(
                ConfigIssue.warning(
                    str(entry),
                    "",
                    f"'{name}' is read by nothing — did you mean "
                    f"'{name[:-4]}.yaml'? (§5)",
                )
            )
        elif entry.is_dir():
            issues.append(
                ConfigIssue.warning(
                    str(entry),
                    "",
                    "unexpected directory in config/ — only skills/, agents/ "
                    "and plugins/ are assembled from it (§5)",
                )
            )
        else:
            issues.append(
                ConfigIssue.warning(
                    str(entry),
                    "",
                    "unknown file in config/ — the roster is the six documents "
                    "plus *.local.yaml; nothing else here is ever read (§5)",
                )
            )
    return issues


def _trust_audit_summary(findings: list[ConfigIssue]) -> ConfigIssue:
    """One aggregated warning for the §8.7 audit surface.

    Enforcement belongs to bootstrap/reload (ERROR-level, refuses); the
    audit reports the same detector's findings as a single warning so
    `make lint` on a developer box — where the developer, trivially, owns
    the tree — shows the truth without a wall of per-entry lines.
    """
    buckets: dict[str, list[str]] = {}
    order = [
        "world-writable",
        "owned by the service account",
        "shell metacharacters",
        "not executable",
        "does not exist",
        "cannot stat",
        "config directory is missing",
    ]
    for finding in findings:
        label = next((key for key in order if key in finding.message), "other")
        buckets.setdefault(label, []).append(finding.source)
    ordered_labels = order + [k for k in buckets if k not in order]
    parts = "; ".join(
        f"{label} ×{len(srcs)} ({', '.join(srcs[:2])}{'...' if len(srcs) > 2 else ''})"
        for label in ordered_labels
        if (srcs := buckets.get(label))
    )
    return ConfigIssue.warning(
        "<trust>",
        "",
        f"§8.7 trust-boundary: {len(findings)} violation(s) — "
        "startup/reload with CONFIG_ALLOW_LOCAL unset would REFUSE: " + parts,
    )


def _dotenv_typo_issues(keys: Iterable[str]) -> list[ConfigIssue]:
    """T0.2 leftover 5: near-miss deployment variable names in .env."""
    known = sorted(DEPLOY_ENV_VARS)
    issues: list[ConfigIssue] = []
    for key in sorted(keys):
        if key in DEPLOY_ENV_VARS:
            continue
        near = difflib.get_close_matches(key, known, n=1, cutoff=0.75)
        if near:
            issues.append(
                ConfigIssue.error(
                    ".env",
                    key,
                    f"'{key}' closely resembles deployment variable "
                    f"'{near[0]}' — did you mean {near[0]}=...? Extra keys "
                    "(SECRET_*, proxy vars) are fine; silent near-miss typos "
                    "mean a knob nobody actually set (§11, T0.2 review).",
                )
            )
    return issues


# ---------------------------------------------------------------------------
# Deploy-posture gates
# ---------------------------------------------------------------------------


def _listen_issues(
    *, registration_open: bool, listen_addr: str | None
) -> list[ConfigIssue]:
    if not registration_open:
        return []
    source = "config/settings.yaml"
    path = "registration.open"
    if listen_addr is None:
        return [
            ConfigIssue.warning(
                source,
                path,
                "open registration with LISTEN_ADDR undeclared — config-check "
                "guesses nothing (§8.11): if this deployment binds beyond "
                "loopback, export LISTEN_ADDR (the T13.6 systemd unit sets it "
                "next to uvicorn --host) and re-run",
            )
        ]
    host = _listen_host(listen_addr)
    if _is_loopback_host(host):
        return []
    return [
        ConfigIssue.error(
            source,
            path,
            f"registration.open=true while the service listens on {host!r} "
            "(non-loopback): any network visitor can register and spend the "
            "single global key (§8.11) — set registration.open=false or bind "
            "to loopback",
        )
    ]


def _sizing_issues(
    *, sandbox: SandboxConfig, memory_bytes: int | None
) -> list[ConfigIssue]:
    source = "config/settings.yaml"
    path = "sandbox.max_live_containers"
    try:
        need = sandbox.max_live_containers * _mem_spec_bytes(sandbox.memory)
    except ValueError as exc:  # schema pattern rules this out; belt+braces
        return [ConfigIssue.error(source, path, str(exc))]
    if memory_bytes is None:
        return [
            ConfigIssue.warning(
                source,
                path,
                "host RAM unknown at check time — recompute §8.10b "
                "(max_live_containers × memory ≲ 1/3 host RAM) on the actual "
                "deployment host",
            )
        ]
    if need * 3 > memory_bytes:
        gib = 1024**3
        return [
            ConfigIssue.warning(
                source,
                path,
                f"{sandbox.max_live_containers} × sandbox.memory "
                f"({sandbox.memory}) = {need / gib:.1f} GiB exceeds 1/3 of "
                f"this host's {memory_bytes / gib:.1f} GiB (§8.10b) — lower "
                "the value or deploy on a larger host",
            )
        ]
    return []


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_config_check(
    *,
    repo_root: Path,
    config_dir: Path | None = None,
    environ: Mapping[str, str] | None = None,
    allow_local: bool = False,
    listen_addr: str | None = None,
    host_memory: Callable[[], int | None] = host_total_memory_bytes,
    dotenv_keys: Callable[[], Iterable[str]] = default_dotenv_key_audit,
) -> tuple[list[ConfigIssue], ConfigSnapshot | None]:
    """Full audit pass over one config tree: (issues, snapshot-or-None).

    The snapshot is present iff the core pipeline assembled; reference /
    listen / sizing audits need it, file scans and the dotenv audit do
    not. Never raises. Deterministic stage order: pipeline → file scans →
    resources/references → listen → sizing → dotenv audit.
    """
    env_map: Mapping[str, str] = environ if environ is not None else os.environ
    config = config_dir if config_dir is not None else repo_root / "config"
    issues: list[ConfigIssue] = []

    # -- audit-mode env fill: references found in raw docs first ----------
    audit_env = dict(env_map)
    referenced: dict[str, str] = {}  # name -> display of first mentioning file
    for filename, _cls, _req in CONFIG_FILES:
        path = config / filename
        if not path.is_file():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            continue  # the pipeline below reports the syntax error
        if isinstance(data, dict):
            for name in sorted(env_references_in(data)):
                referenced.setdefault(name, display_name(path, config, repo_root))
    for name, display in referenced.items():
        if not audit_env.get(name, "").strip():
            audit_env[name] = CHECK_PLACEHOLDER
            issues.append(
                ConfigIssue.warning(
                    display,
                    "",
                    f"environment variable {name} is unset — config-check "
                    "audited with a placeholder; the service itself refuses to "
                    "start until it is set (§11)",
                )
            )

    # -- core pipeline (same trio async load_config wraps) -----------------
    overlays = load_local_overlays(
        repo_root=repo_root,
        config_dir=config,
        environ=audit_env,
        allow_local=allow_local,
    )
    scanned = load_config_files(
        repo_root=repo_root, config_dir=config, environ=audit_env
    )

    snapshot: ConfigSnapshot | None
    try:
        snapshot = assemble_snapshot(
            scanned,
            overlays,
            contributions=(),
            repo_root=repo_root,
            config_dir=config,
            version=1,
        )
        issues = [*issues, *snapshot.issues]
    except ConfigLoadError as exc:
        snapshot = None
        issues = [*issues, *exc.issues]

    # -- scans that don't need the models ----------------------------------
    issues.extend(_secret_scan(config, repo_root=repo_root))
    issues.extend(_unknown_files(config))

    if snapshot is not None:
        issues.extend(
            _reference_issues(snapshot, config_dir=config, repo_root=repo_root)
        )
        trust_findings = check_config_trust(
            config, hooks=snapshot.hooks.hooks, repo_root=repo_root
        )
        if trust_findings:
            issues.append(_trust_audit_summary(trust_findings))
        issues.extend(
            _listen_issues(
                registration_open=snapshot.settings.registration.open,
                listen_addr=listen_addr,
            )
        )
        issues.extend(
            _sizing_issues(
                sandbox=snapshot.settings.sandbox, memory_bytes=host_memory()
            )
        )
    else:
        issues.append(
            ConfigIssue.warning(
                "<check>",
                "",
                "reference/listen/sizing audits skipped: the core pipeline "
                "above refused assembly — fix those first",
            )
        )

    issues.extend(_dotenv_typo_issues(dotenv_keys()))
    return dedupe(issues), snapshot
