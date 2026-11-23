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
returns issues, never throws). Same-name-conflict detection and plugin
contribution merging extend this method in T1.4; reference integrity
(skills→tools etc.) in T1.6. The startup-only ``CONFIG_PERMISSIVE=1``
escape hatch mentioned in §4 is deliberately NOT consumed here: refusing to
boot is the startup wiring's policy (T1.8), not the snapshot builder's.

Per §4, ``ConfigIssue``'s nominal home is this module — it is re-exported
from :mod:`app.config.issue` so every layer shares one class object.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from app.config.issue import ConfigIssue
from app.config.loader import LoadedConfig, load_config_files
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
    "ReloadReport",
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

    def source_for(self, filename: str) -> str:
        """Display path of one roster file as recorded at load time, or the
        default-layout spelling when the file was absent (§5 naming)."""
        for name, _ in self.sources:
            if name == filename or name.endswith("/" + filename):
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


def snapshot_from_loaded(loaded: LoadedConfig, *, version: int) -> ConfigSnapshot:
    """Build a snapshot from an error-free :class:`LoadedConfig`.

    Raises:
        ConfigLoadError: the load carried error-level findings.
        RuntimeError: programming error — the loader promised models that
            are missing (cannot happen while ``has_errors`` is False).
    """
    if loaded.has_errors:
        raise ConfigLoadError(loaded.issues)
    parts: dict[str, Any] = {}
    for stem in ("settings", "tools", "permissions", "hooks", "mcp", "plugins"):
        model = getattr(loaded, stem)
        if model is None:  # defensive; LoadedConfig's contract rules it out
            raise RuntimeError(
                f"loader returned no {stem} model without an error issue"
            )
        parts[stem] = model
    return ConfigSnapshot(
        version=version,
        loaded_at=get_clock().now(),
        sources=loaded.sources,
        issues=loaded.issues,
        **parts,
    )


async def load_config(
    *,
    repo_root: Path,
    config_dir: Path | None = None,
    environ: Mapping[str, str] | None = None,
    version: int = 1,
) -> ConfigSnapshot:
    """Assemble one snapshot (§4's entry point): files -> expand -> models
    -> cross-file validation, refusing anything with error findings.

    ``config_dir`` defaults to ``<repo_root>/config`` (the §5 layout; the
    deployment may relocate it via ``CONFIG_DIR``, wired in T1.8). File I/O
    runs in a worker thread so a reload never blocks the event loop.
    """
    loaded = await asyncio.to_thread(
        load_config_files,
        repo_root=repo_root,
        config_dir=config_dir if config_dir is not None else repo_root / "config",
        environ=environ,
    )
    snapshot = snapshot_from_loaded(loaded, version=version)
    problems = snapshot.validate()
    if any(i.level == "error" for i in problems):
        # Carry the blocking findings AND the surviving warnings, so one
        # exception holds the complete diagnosis.
        raise ConfigLoadError((*snapshot.issues, *problems))
    return snapshot


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
    ) -> ConfigStore:
        """Initial store at version 1; raises :class:`ConfigLoadError` if the
        shipped config is not usable (the service must not start on top of
        it — permissive boot, if ever wanted, is the caller's decision)."""
        return cls(
            await load_config(
                repo_root=repo_root, config_dir=config_dir, environ=environ, version=1
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
                )
            except ConfigLoadError as exc:
                return ReloadReport(
                    ok=False,
                    previous_version=self._current.version,
                    version=None,
                    issues=exc.issues,
                )
            return self.swap(candidate)
