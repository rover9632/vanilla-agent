"""Immutable config snapshot and the atomic-swap store (docs/plan.md T1.3, §4).

:class:`ConfigSnapshot` is the assembled capability view one process run is
born with; main-loop invariant #6 (§3) says the run holds *this object* for
its whole life — a later reload must never tear what an executing run sees.
That invariant is bought with two deliberate mechanics, both enforced here:

- snapshots are deep-immutable (frozen dataclass of frozen pydantic models,
  tuples throughout), so there is no in-place mutation to defend against;
- :class:`ConfigStore` replaces a snapshot only by swapping the *pointer*
  after building and validating the candidate in full ("全绿才原子替换;
  失败保留旧的"). A replaced snapshot stays perfectly usable by whoever
  still references it — old and new coexist, no version aliasing.

``version`` is assigned by the store and advances exactly by one per
*installed* snapshot (rejected candidates consume nothing), so
``run.params_json.config_version`` is a gapless monotonic history and
"which capability set did this run use" is always answerable.

``loaded_at`` comes from :mod:`app.core.clock` (injectable; tests freeze it).

Cross-file consistency checks that only the assembled view can answer live
in :meth:`ConfigSnapshot.validate` (§4's collect-don't-raise contract: it
returns issues, never throws). Since T1.4 the assembled view is the *merged*
view: operator documents (with active ``*.local.yaml`` replacements applied)
unioned with enabled-plugin contributions, provenance recorded per entry
(:mod:`app.config.merge`); same-name conflicts across sources are merge
errors. Reference integrity (skills→tools etc.) lands on ``validate`` in
T1.6. The startup-only ``CONFIG_PERMISSIVE=1``
escape hatch mentioned in §4 is deliberately NOT consumed here: refusing to
boot is the startup wiring's policy (T1.8), not the snapshot builder's.

Per §4, ``ConfigIssue``'s nominal home is this module — it is re-exported
from :mod:`app.config.issue` so every layer shares one class object.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from app.config.issue import ConfigIssue
from app.config.loader import (
    LoadedConfig,
    OverlayBundle,
    load_config_files,
    load_local_overlays,
)
from app.config.merge import (
    Origin,
    PluginContribution,
    merge_contributions,
    operator_displays,
)
from app.config.schema import (
    HookConfig,
    MCPConfig,
    PermissionMode,
    PermissionsConfig,
    PluginsConfig,
    Settings,
    ToolConfig,
)
from app.core.clock import get_clock

__all__ = [
    "ConfigIssue",  # §4 nominal home; class object lives in app.config.issue
    "ConfigLoadError",
    "ConfigSnapshot",
    "ConfigStore",
    "Origin",  # re-exported from app.config.merge (T1.4 provenance)
    "PluginContribution",  # ditto (T12.5 produces these)
    "ReloadReport",
    "assemble_snapshot",
    "load_config",
]

#: Synthetic ``source`` for findings the store itself raises (a rejected
#: swap precondition — no config file is wrong; the caller is).
STORE_SOURCE: Final = "<store>"


class ConfigLoadError(Exception):
    """Initial load refused: the assembled config carries error-level
    findings. Carries every issue (warnings included) for CLI reporting;
    ``errors`` narrows to the blocking ones. Startup fails fast on this —
    permissive boot is a caller policy (§4), not a behavior of this module.
    """

    def __init__(self, issues: tuple[ConfigIssue, ...]) -> None:
        self.issues = issues
        super().__init__(self._describe(issues))

    @staticmethod
    def _describe(issues: tuple[ConfigIssue, ...]) -> str:
        lines = [
            f"{i.source or '<unknown>'}"
            + (f":{i.path}" if i.path else "")
            + f" {i.message}"
            for i in issues
            if i.level == "error"
        ]
        return "config load refused:\n  " + "\n  ".join(lines)


@dataclass(frozen=True)
class ConfigSnapshot:
    """One fully validated assembly of the config tree (§4)."""

    version: int
    loaded_at: datetime
    #: (display path, sha256) per file that composed this snapshot (T1.2).
    sources: tuple[tuple[str, str], ...]
    settings: Settings
    tools: ToolConfig
    permissions: PermissionsConfig
    hooks: HookConfig
    mcp: MCPConfig
    plugins: PluginsConfig
    #: Load-time findings that survived into a usable config (warnings).
    #: Error-level findings never reach a snapshot: loading refuses instead.
    issues: tuple[ConfigIssue, ...] = ()
    #: Per-entry provenance for the three union resources (T1.4). Each
    #: tuple aligns 1:1 with the corresponding merged entries; §4's
    #: rule_source and the T12 hook audit trail read these labels.
    rule_origins: tuple[Origin, ...] = ()
    hook_origins: tuple[Origin, ...] = ()
    mcp_origins: tuple[Origin, ...] = ()

    def __post_init__(self) -> None:
        """Alignment is structural: no construction path (tests using
        ``dataclasses.replace`` included) can desync provenance from its
        entries."""
        for name, origins, entries in (
            ("rule", self.rule_origins, self.permissions.rules),
            ("hook", self.hook_origins, self.hooks.hooks),
            ("mcp server", self.mcp_origins, self.mcp.servers),
        ):
            if len(origins) != len(entries):
                raise ValueError(
                    f"{name}_origins must align 1:1 with the merged {name} "
                    f"entries ({len(origins)} origins vs {len(entries)})"
                )

    def source_for(self, filename: str) -> str:
        """Display path of the document that AUTHORED one roster stem.

        When a ``*.local.yaml`` replacement won, findings about that stem
        must point at the local file (it is what an operator would go edit),
        not at the replaced base — so the local spelling is searched first.
        Falls back to the default-layout name when the file was absent and
        defaults were used (§5 naming)."""
        stem = filename.removesuffix(".yaml")
        for want in (f"{stem}.local.yaml", filename):
            for name, _ in self.sources:
                if name == want or name.endswith("/" + want):
                    return name
        return f"config/{filename}"

    def validate(self) -> list[ConfigIssue]:
        """Cross-file consistency findings for the assembled view.

        Collects everything, raises nothing (§4). Today the pairs that only
        make sense *together* are the permission-mode ones; T1.4 adds
        same-name conflict detection and T1.6 reference integrity to this
        same method so config-check stays one call.
        """
        issues: list[ConfigIssue] = []
        allowed = self.settings.permissions.allowed_modes
        modes_note = ", ".join(repr(m.value) for m in allowed) or "(empty)"

        default_mode = self.permissions.default_mode
        if default_mode not in allowed:
            issues.append(
                ConfigIssue.error(
                    self.source_for("permissions.yaml"),
                    "default_mode",
                    f"default mode {default_mode.value!r} is not in "
                    f"settings.yaml permissions.allowed_modes [{modes_note}]",
                )
            )

        if self.permissions.policy.disallow_bypass and PermissionMode.BYPASS in allowed:
            issues.append(
                ConfigIssue.error(
                    self.source_for("settings.yaml"),
                    "permissions.allowed_modes",
                    "permissions.yaml policy.disallow_bypass is a hard "
                    "constraint, but allowed_modes still offers 'bypass' — "
                    "remove it from the list or lift the policy",
                )
            )
        return issues


def assemble_snapshot(
    loaded: LoadedConfig,
    overlays: OverlayBundle,
    *,
    contributions: Sequence[PluginContribution],
    repo_root: Path,
    config_dir: Path,
    version: int,
) -> ConfigSnapshot:
    """Merge base layer + local replacements + plugin contributions into a
    snapshot, refusing anything with error-level findings.

    Raises:
        ConfigLoadError: base load, overlay ingestion, merge, or cross-file
            validation produced errors — the exception carries the COMPLETE
            diagnosis across every stage and level.
        RuntimeError: programming error — the loader promised a model that
            is missing while reporting no error (cannot happen per
            :class:`LoadedConfig`'s contract; guarded anyway).
    """
    if loaded.has_errors:
        raise ConfigLoadError(loaded.issues)
    finals: dict[str, Any] = {}
    for stem in ("settings", "tools", "permissions", "hooks", "mcp", "plugins"):
        model = overlays.overrides.get(stem, getattr(loaded, stem))
        if model is None:
            raise RuntimeError(
                f"loader returned no {stem} model without an error issue"
            )
        finals[stem] = model
    outcome = merge_contributions(
        permissions=finals["permissions"],
        hooks=finals["hooks"],
        mcp=finals["mcp"],
        plugins=finals["plugins"],
        contributions=contributions,
        displays=operator_displays(config_dir, repo_root, overlays.overrides),
        repo_root=repo_root,
        config_dir=config_dir,
    )
    all_issues = (*loaded.issues, *overlays.issues, *outcome.issues)
    snapshot = ConfigSnapshot(
        version=version,
        loaded_at=get_clock().now(),
        sources=(*loaded.sources, *overlays.sources),
        settings=finals["settings"],
        tools=finals["tools"],
        permissions=outcome.permissions,
        hooks=outcome.hooks,
        mcp=outcome.mcp,
        plugins=finals["plugins"],
        issues=tuple(i for i in all_issues if i.level == "warning"),
        rule_origins=outcome.rule_origins,
        hook_origins=outcome.hook_origins,
        mcp_origins=outcome.mcp_origins,
    )
    findings = (*all_issues, *snapshot.validate())
    if any(i.level == "error" for i in findings):
        raise ConfigLoadError(findings)
    return snapshot


async def load_config(
    *,
    repo_root: Path,
    config_dir: Path | None = None,
    environ: Mapping[str, str] | None = None,
    version: int = 1,
    allow_local: bool = False,
    contributions: Sequence[PluginContribution] = (),
) -> ConfigSnapshot:
    """Assemble one snapshot (§4's entry point): base files -> local
    replacements -> plugin contributions -> cross-file validation.

    ``config_dir`` defaults to ``<repo_root>/config`` (the §5 layout; the
    deployment may relocate it via ``CONFIG_DIR``, wired in T1.8).
    ``allow_local`` is the ``CONFIG_ALLOW_LOCAL`` gate (env-wired T1.8);
    ``contributions`` arrive from the plugin registrar (produced T12.5).
    File I/O runs in worker threads so a reload never blocks the event
    loop; assembly itself is pure.
    """
    config = config_dir if config_dir is not None else repo_root / "config"
    loaded = await asyncio.to_thread(
        load_config_files,
        repo_root=repo_root,
        config_dir=config,
        environ=environ,
    )
    overlays = await asyncio.to_thread(
        load_local_overlays,
        repo_root=repo_root,
        config_dir=config,
        environ=environ,
        allow_local=allow_local,
    )
    return assemble_snapshot(
        loaded,
        overlays,
        contributions=contributions,
        repo_root=repo_root,
        config_dir=config,
        version=version,
    )


@dataclass(frozen=True)
class ReloadReport:
    """Outcome of one :meth:`ConfigStore.swap` / :meth:`ConfigStore.reload`.

    ``ok`` reports *only* the swap decision: when false the store still
    serves the previous snapshot; ``issues`` carries everything that led
    there (load errors, validation findings).
    """

    ok: bool
    previous_version: int
    version: int | None  # the installed version when ok
    issues: tuple[ConfigIssue, ...] = ()


class ConfigStore:
    """The live-config handle every consumer reads through (§4).

    ``current`` is read on the event loop without locking (it is one
    attribute read of an immutable object — the snapshot a reader gets is
    always whole, never a mix of two generations). Every write path is
    serialized under ``_lock``: ``reload()`` does its I/O inside the lock so
    two SIGHUPs can never publish out of order, and ``swap()`` — a
    synchronous loop-thread call — cannot interleave with anything.
    """

    def __init__(self, initial: ConfigSnapshot) -> None:
        if initial.version < 1:
            raise ValueError(f"snapshot versions start at 1 (got {initial.version})")
        self._current = initial
        self._lock = asyncio.Lock()

    @classmethod
    async def bootstrap(
        cls,
        *,
        repo_root: Path,
        config_dir: Path | None = None,
        environ: Mapping[str, str] | None = None,
        allow_local: bool = False,
        contributions: Sequence[PluginContribution] = (),
    ) -> ConfigStore:
        """Initial store at version 1; raises :class:`ConfigLoadError` if the
        shipped config is not usable (the service must not start on top of
        it — permissive boot, if ever wanted, is the caller's decision)."""
        return cls(
            await load_config(
                repo_root=repo_root,
                config_dir=config_dir,
                environ=environ,
                version=1,
                allow_local=allow_local,
                contributions=contributions,
            )
        )

    @property
    def current(self) -> ConfigSnapshot:
        return self._current

    def swap(self, candidate: ConfigSnapshot) -> ReloadReport:
        """Publish *candidate* iff it validates and advances exactly by one.

        Failure keeps the previous snapshot installed and consumes no
        version number (a rejected candidate simply never happened).
        """
        previous = self._current
        problems = candidate.validate()
        # A report carries the *whole* diagnosis: the candidate's surviving
        # load-time warnings plus whatever this decision produced.
        findings = (*candidate.issues, *problems)
        if any(p.level == "error" for p in problems):
            return ReloadReport(
                ok=False,
                previous_version=previous.version,
                version=None,
                issues=findings,
            )
        if candidate.version != previous.version + 1:
            return ReloadReport(
                ok=False,
                previous_version=previous.version,
                version=None,
                issues=(
                    *findings,
                    ConfigIssue.error(
                        STORE_SOURCE,
                        "version",
                        f"candidate must advance the version exactly by one "
                        f"(current {previous.version}, "
                        f"candidate {candidate.version})",
                    ),
                ),
            )
        self._current = candidate
        return ReloadReport(
            ok=True,
            previous_version=previous.version,
            version=candidate.version,
            issues=findings,
        )

    async def reload(
        self,
        *,
        repo_root: Path,
        config_dir: Path | None = None,
        environ: Mapping[str, str] | None = None,
        allow_local: bool = False,
        contributions: Sequence[PluginContribution] = (),
    ) -> ReloadReport:
        """Re-read the config tree and swap it in iff fully green (§7's
        SIGHUP path). Never raises for config problems: even a load
        refusal lands in the report, and the old snapshot stays
        authoritative. (Startup is different — :meth:`bootstrap` raises
        :class:`ConfigLoadError` so the service refuses to boot.)"""
        async with self._lock:
            next_version = self._current.version + 1
            try:
                candidate = await load_config(
                    repo_root=repo_root,
                    config_dir=config_dir,
                    environ=environ,
                    version=next_version,
                    allow_local=allow_local,
                    contributions=contributions,
                )
            except ConfigLoadError as exc:
                return ReloadReport(
                    ok=False,
                    previous_version=self._current.version,
                    version=None,
                    issues=exc.issues,
                )
            return self.swap(candidate)
