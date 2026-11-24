"""Config trust-boundary checks (docs/plan.md T1.7; §8.7, invariant #7).

ADR-015 moved capability into files — therefore **write access to
``config/`` IS capability authorization**. §8.7's consequence, enforced
here at the two places plan names ("启动与 reload 都执行"):

- every entry inside ``config/`` (the directory itself, subdirectories,
  files): world-writable is fatal, and ownership by the *service account*
  (the process's own uid) is fatal — an owner can always chmod its files
  back to writable, so mode bits alone cannot save that posture.
- command hooks additionally: the script must exist, be executable
  (spawned argv-direct, never through a shell), be non-world-writable,
  and its declared path must contain no shell metacharacters — a target
  that looks like ``x.sh; curl …`` is a config-time mistake worth refusing
  even though argv-passing never interprets it.

Dev escape (§8.7): the two switches are deliberately ONE — the existing
``CONFIG_ALLOW_LOCAL``. With it on, every finding above degrades to a
loud ``DEV MODE`` warning (nothing is silently skipped; §8.7 also rules
out inventing further skip switches like ``CONFIG_TRUST_ON_STARTUP`` —
there is no second escape hatch here, by policy).

Enforcement vs audit (same detector, different severity):

- :func:`apply_trust` is wired into ``ConfigStore.bootstrap`` /
  ``ConfigStore.reload``: findings come back ERROR → boot refuses,
  reload keeps the old snapshot.
- config-check reports the same findings as WARNING — the audit surface
  tells you what startup WOULD do without turning ``make lint`` red on
  developer machines (where the developer, necessarily, owns the tree).

Windows/non-POSIX: uid semantics don't exist there; the ownership probe
degrades to "no finding" rather than a false alarm (deploys are Linux;
see §12 deployment posture).
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from app.config.issue import ConfigIssue
from app.config.loader import resolve_repo_relative
from app.config.schema import HookEntry, HookKind

if TYPE_CHECKING:
    # runtime cycle guard: snapshot.py imports THIS module (T1.7 gate);
    # the annotation below is a string under `from __future__ import
    # annotations`, so there is nothing to import at runtime.
    from app.config.snapshot import ConfigSnapshot

#: Characters that make a declared path look like shell syntax. argv-direct
#: spawning never interprets them — which is exactly why a config carrying
#: them is a mistake rather than a feature (§8.7).
_SHELL_METACHARS: Final = re.compile(r"[;&|<>`$()\\\"'\x00-\x1f]")

_DEV_PREFIX: Final = "DEV MODE (CONFIG_ALLOW_LOCAL=1): "


def _current_uid() -> int:
    """Process uid on POSIX; -1 (matches nothing) where uid does not exist."""
    return getattr(os, "getuid", lambda: -1)()


@dataclass(frozen=True)
class TrustResult:
    errors: tuple[ConfigIssue, ...]
    warnings: tuple[ConfigIssue, ...]


def _entry_findings(path: Path, *, what: str, service_uid: int | None) -> list[str]:
    problems: list[str] = []
    try:
        st = path.stat()
    except OSError as exc:  # vanished mid-walk: a live audit says so
        problems.append(f"cannot stat {what} ({exc.strerror or type(exc).__name__})")
        return problems
    if st.st_mode & 0o002:
        problems.append(
            f"{what} is world-writable — anyone who can reach this box can "
            "rewrite capability (§8.7)"
        )
    if service_uid is not None and st.st_uid == service_uid:
        problems.append(
            f"{what} is owned by the service account (uid {service_uid}) — "
            "the process could rewrite its own capability; deploy with the "
            "tree read-only or owned by a different account (§8.7)"
        )
    return problems


def _hook_script_findings(raw_target: str, resolved: Path) -> list[str]:
    problems: list[str] = []
    if _SHELL_METACHARS.search(raw_target):
        problems.append(
            f"hook target {raw_target!r} contains shell metacharacters — "
            "hooks spawn argv-direct; a target like this is a typo or a "
            "probe, never a feature (§8.7)"
        )
    try:
        st = resolved.stat()
    except OSError:
        problems.append(
            f"hook script {raw_target!r} does not exist — nothing spawnable "
            "(existence is also a config-check reference finding)"
        )
        return problems
    if st.st_mode & 0o002:
        problems.append(
            f"hook script {raw_target!r} is world-writable — the executed "
            "bytes must not be attacker-editable (§8.7)"
        )
    if not os.access(resolved, os.X_OK):
        problems.append(
            f"hook script {raw_target!r} is not executable — argv-direct "
            "spawn would fail anyway; set the exec bit (§8.7)"
        )
    # NOTE: the service-uid rule already fired via the generic walk (hook
    # scripts live inside config/) — deliberately not duplicated here.
    return problems


def check_config_trust(
    config_dir: Path,
    *,
    hooks: Sequence[HookEntry] = (),
    repo_root: Path | None = None,
    service_uid: int | None = None,
) -> list[ConfigIssue]:
    """Raw findings (always error-severity objects; severity assignment
    happens in :func:`apply_trust` / the check integration)."""
    uid = _current_uid() if service_uid is None else service_uid
    issues: list[ConfigIssue] = []

    if not config_dir.is_dir():
        issues.append(
            ConfigIssue.error(
                str(config_dir),
                "",
                "config directory is missing — trust boundary cannot be proven",
            )
        )
        return issues

    for problem in _entry_findings(
        config_dir, what="the config directory itself", service_uid=uid
    ):
        issues.append(ConfigIssue.error(str(config_dir), "", problem))

    for root, dirnames, filenames in os.walk(config_dir):
        dirnames.sort()
        for name in sorted(dirnames) + sorted(filenames):
            display = Path(root) / name
            for problem in _entry_findings(
                display, what=f"{display.name}", service_uid=uid
            ):
                issues.append(ConfigIssue.error(str(display), "", problem))

    if repo_root is not None:
        for index, hook in enumerate(hooks):
            if hook.kind is not HookKind.COMMAND:
                continue
            resolved = Path(resolve_repo_relative(repo_root, hook.target))
            for problem in _hook_script_findings(hook.target, resolved):
                issues.append(
                    ConfigIssue.error(str(resolved), f"hooks[{index}].target", problem)
                )
    return issues


def apply_trust(
    snapshot: ConfigSnapshot,
    *,
    config_dir: Path,
    repo_root: Path,
    allow_local: bool,
    service_uid: int | None = None,
) -> TrustResult:
    """Startup/reload gate (§8.7). Errors refuse; dev mode demotes every
    finding to a loud warning and lets the boot proceed."""
    raw = check_config_trust(
        config_dir,
        hooks=snapshot.hooks.hooks,
        repo_root=repo_root,
        service_uid=service_uid,
    )
    if allow_local:
        # model_copy(update=...) skips revalidation but DOES rewrite the
        # level: TrustResult.warnings must genuinely hold warning objects
        # (a dev-mode "error" leaking into snapshot.issues would be a
        # contract violation, not cosmetics).
        warnings = tuple(
            ConfigIssue.warning(i.source, i.path, _DEV_PREFIX + i.message) for i in raw
        )
        return TrustResult(errors=(), warnings=warnings)
    return TrustResult(errors=tuple(raw), warnings=())
