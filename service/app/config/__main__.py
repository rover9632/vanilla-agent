"""``python -m app.config check|dump`` — the operator-side config CLI.

Plan §7 moved everything an operator needs to a shell: no HTTP surface for
config (no admin role exists — §13-D3), so this module IS the review tool:

- ``check`` runs :func:`app.config.check.run_config_check` and exits 1 when
  any error-level finding exists (``make config-check`` chains it, and the
  Makefile's import-guard makes the target a hard gate the day this file
  exists — T0.4).
- ``dump`` prints the fully merged capability view (models + provenance +
  warnings) for human review, masking every secret field. ``dump`` runs
  against the REAL environment with no audit placeholders: a dump that
  succeeds is exactly a bootable config.

Environment resolution mirrors the service: ``$REPO_ROOT/.env`` values,
overridden by the process environment (same precedence pydantic-settings
uses). ``LISTEN_ADDR`` / ``CONFIG_ALLOW_LOCAL`` are read as raw strings
here on purpose — the CLI must still work when ``EnvSettings`` would
refuse to construct (e.g. no API key on a lint runner); the full env
validation gate is startup's job, not the checker's.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from app.config import check as checks
from app.config.issue import ConfigIssue
from app.config.loader import load_config_files, load_local_overlays
from app.config.snapshot import (
    ConfigLoadError,
    ConfigSnapshot,
    assemble_snapshot,
)
from app.core.env import REPO_ROOT, default_dotenv_path, read_dotenv

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def _truthy(raw: str) -> bool:
    """Same vocabulary EnvSettings accepts for its boolean flags."""
    return raw.strip().lower() in _TRUE_VALUES


def _environ() -> dict[str, str]:
    env = read_dotenv(default_dotenv_path())
    env.update(os.environ)  # process env wins over the dotenv file
    return env


def _paths(args: argparse.Namespace) -> tuple[Path, Path]:
    repo_root = Path(args.repo_root).expanduser() if args.repo_root else REPO_ROOT
    if not repo_root.is_absolute():
        repo_root = (Path.cwd() / repo_root).resolve()
    raw = args.config_dir or os.environ.get("CONFIG_DIR") or "config"
    config_dir = Path(raw).expanduser()
    if not config_dir.is_absolute():
        config_dir = repo_root / config_dir
    return repo_root, config_dir


def _print_issues(issues: list[ConfigIssue]) -> None:
    for issue in issues:
        loc = f"{issue.source or '<unknown>'}" + (
            f":{issue.path}" if issue.path else ""
        )
        print(f"[{issue.level.upper():7}] {loc}: {issue.message}")


def cmd_check(args: argparse.Namespace) -> int:
    repo_root, config_dir = _paths(args)
    env = _environ()
    listen = (env.get("LISTEN_ADDR") or "").strip() or None
    issues, snapshot = checks.run_config_check(
        repo_root=repo_root,
        config_dir=config_dir,
        environ=env,
        allow_local=_truthy(env.get("CONFIG_ALLOW_LOCAL", "")),
        listen_addr=listen,
    )
    _print_issues(issues)
    errors = [i for i in issues if i.level == "error"]
    warnings = [i for i in issues if i.level == "warning"]
    tail = (
        f"assembled from {len(snapshot.sources)} files"
        if snapshot
        else "core pipeline REFUSED assembly"
    )
    print(f"config-check: {len(errors)} error(s), {len(warnings)} warning(s); {tail}")
    return 1 if errors else 0


def _masked_mcp_server(server: Any) -> dict[str, Any]:
    """Server row for dump: env/header NAMES only, values never (§7/§8.6)."""
    return {
        "name": server.name,
        "transport": server.transport.value,
        "enabled": server.enabled,
        "requires_approval": server.requires_approval,
        "command": server.command,
        "args": server.args,
        "env_names": sorted(server.env),
        "url": server.url,
        "header_names": sorted(server.headers),
        "connect_timeout_s": server.connect_timeout_s,
    }


def _report(
    snapshot: ConfigSnapshot, *, repo_root: Path, config_dir: Path
) -> dict[str, Any]:
    rows, _scan_issues = checks.scan_resources(config_dir, repo_root=repo_root)
    skills = [r for r in rows if r["kind"] == "skill"]
    agents = [r for r in rows if r["kind"] == "agent"]
    catalog = snapshot.tools.tools
    enabled = sorted(
        n for n in sorted(type(catalog).model_fields) if getattr(catalog, n).enabled
    )
    disabled = sorted(
        n for n in sorted(type(catalog).model_fields) if not getattr(catalog, n).enabled
    )
    return {
        "version": snapshot.version,
        "loaded_at": snapshot.loaded_at.isoformat(),
        "sources": [dict(zip(("file", "sha256"), pair)) for pair in snapshot.sources],
        "provider": {
            "base_url": snapshot.settings.provider.base_url,
            "api_key": "**********",  # never dump, even for operators (§8.6)
            "model_name": snapshot.settings.provider.model_name,
            "temperature": snapshot.settings.provider.temperature,
            "top_p": snapshot.settings.provider.top_p,
            "max_tokens": snapshot.settings.provider.max_tokens,
            "timeout_s": snapshot.settings.provider.timeout_s,
            "retries": snapshot.settings.provider.retries,
            "allowed_models": list(snapshot.settings.provider.effective_allowed_models),
        },
        "limits": snapshot.settings.limits.model_dump(),
        "sandbox": snapshot.settings.sandbox.model_dump(),
        "registration": snapshot.settings.registration.model_dump(),
        "permissions": {
            "default_mode": snapshot.permissions.default_mode.value,
            "policy": snapshot.permissions.policy.model_dump(),
            "allowed_modes": [
                m.value for m in snapshot.settings.permissions.allowed_modes
            ],
            "rules": [
                {"origin": origin.label, **rule.model_dump(exclude={"source"})}
                for rule, origin in zip(
                    snapshot.permissions.rules, snapshot.rule_origins
                )
            ],
            "mode_presets": {
                mode.value: [rule.model_dump() for rule in rules]
                for mode, rules in snapshot.permissions.mode_presets.items()
            },
        },
        "tools": {
            "enabled": enabled,
            "disabled": disabled,
            "defaults": snapshot.tools.defaults.model_dump(),
        },
        "skills": skills,
        "agents": agents,
        "hooks": [
            {"origin": origin.label, **hook.model_dump()}
            for hook, origin in zip(snapshot.hooks.hooks, snapshot.hook_origins)
        ],
        "mcp": {
            "connect_timeout_s": snapshot.mcp.connect_timeout_s,
            "servers": [_masked_mcp_server(s) for s in snapshot.mcp.servers],
        },
        "plugins": [p.model_dump() for p in snapshot.plugins.plugins],
        "warnings": [
            {"source": i.source, "path": i.path, "message": i.message}
            for i in snapshot.issues
        ],
    }


def cmd_dump(args: argparse.Namespace) -> int:
    repo_root, config_dir = _paths(args)
    env = _environ()
    loaded = load_config_files(repo_root=repo_root, config_dir=config_dir, environ=env)
    overlays = load_local_overlays(
        repo_root=repo_root,
        config_dir=config_dir,
        environ=env,
        allow_local=_truthy(env.get("CONFIG_ALLOW_LOCAL", "")),
    )
    try:
        snapshot = assemble_snapshot(
            loaded,
            overlays,
            contributions=(),
            repo_root=repo_root,
            config_dir=config_dir,
            version=1,
        )
    except ConfigLoadError as exc:
        _print_issues(list(exc.issues))
        print(
            "config dump refused: this config would not start (fix the "
            "errors above, or run `check` for the audit view)",
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            _report(snapshot, repo_root=repo_root, config_dir=config_dir),
            indent=2,
            ensure_ascii=False,
            default=str,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.config",
        description="vanilla-agent config tooling (§7: operator shell "
        "surface; there is no HTTP equivalent)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name, func in (("check", cmd_check), ("dump", cmd_dump)):
        subparser = sub.add_parser(name)
        subparser.add_argument(
            "--repo-root",
            default=None,
            help="repository root (default: $REPO_ROOT or the checkout layout)",
        )
        subparser.add_argument(
            "--config-dir",
            default=None,
            help="config directory (default: $CONFIG_DIR or <repo-root>/config)",
        )
        subparser.set_defaults(func=func)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
