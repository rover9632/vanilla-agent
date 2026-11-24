"""The shipped ``config/`` tree loads as a clean snapshot (docs/plan.md T1.5).

T1.5's verification column, adapted to what exists before the T1.6 CLI
(``make config-check`` currently SKIPs by design — its guard turns this into
a hard gate the day T1.6 lands):

- the real repository ``config/`` assembles with zero issues;
- the snapshot lists the expected tools/skills/agents;
- the shipped posture is pinned: ``network_mode == none`` (D8),
  ``registration.open == true`` with the §8.11 consequence documented and
  enforced by config-check in T1.6, write tools disabled, http_fetch
  deny-all, disallow_bypass on.

Deliberately tolerant of ONE developer-machine condition: stray
``config/*.local.yaml`` files (git-ignored). With the gate closed they only
warn, and the tests filter exactly that warning class — anything else in
``issues`` is a finding.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from app.config.schema import (
    BUILTIN_TOOL_NAMES,
    AgentFrontmatter,
    NetworkMode,
    PermissionMode,
    SkillFrontmatter,
)
from app.config.snapshot import ConfigSnapshot, load_config

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"

TEST_ENVIRON = {"OPENAI_API_KEY": "sk-test-not-a-real-key"}

EXPECTED_ENABLED = {
    "read_file",
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
EXPECTED_DISABLED = {"write_file", "edit_file", "delete_file", "move_file"}


def _frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), path
    end = text.index("\n---\n", 3)
    data = yaml.safe_load(text[3:end])
    assert isinstance(data, dict), path
    return data


async def snapshot() -> ConfigSnapshot:
    return await load_config(
        repo_root=REPO_ROOT, config_dir=CONFIG_DIR, environ=TEST_ENVIRON
    )


def non_local_issues(snap: ConfigSnapshot) -> list[str]:
    return [
        f"{i.level} {i.source}: {i.message}"
        for i in snap.issues
        if ".local." not in i.source
    ]


# ---------------------------------------------------------------------------


async def test_shipped_config_assembles_without_findings() -> None:
    snap = await snapshot()
    assert non_local_issues(snap) == []
    assert snap.validate() == []
    assert snap.version == 1
    # every roster file physically present -> recorded with real shas
    names = {Path(name).name for name, _ in snap.sources}
    assert {
        "settings.yaml",
        "tools.yaml",
        "permissions.yaml",
        "hooks.yaml",
        "mcp.yaml",
        "plugins.yaml",
    } <= names


async def test_default_posture_is_the_pinned_safest_one() -> None:
    snap = await snapshot()
    assert snap.settings.sandbox.network_mode is NetworkMode.NONE  # D8
    assert snap.settings.registration.open is True  # §8.11 (config-check
    # must ERROR when the service later listens off-loopback — T1.6/T13.6)
    assert snap.permissions.policy.disallow_bypass is True
    assert PermissionMode.BYPASS not in snap.settings.permissions.allowed_modes
    assert snap.settings.sandbox.max_live_containers == 8  # §8.10b default
    assert snap.settings.limits.max_concurrent_runs == 1


async def test_tool_surface_is_the_reviewed_default_set() -> None:
    snap = await snapshot()
    catalog = snap.tools.tools
    enabled = {n for n in BUILTIN_TOOL_NAMES if getattr(catalog, n).enabled}
    assert enabled == EXPECTED_ENABLED
    assert {
        n for n in BUILTIN_TOOL_NAMES if not getattr(catalog, n).enabled
    } == EXPECTED_DISABLED
    assert catalog.http_fetch.allowed_domains == []  # fail closed
    assert catalog.bash.hard_timeout_s == 300.0


async def test_permission_examples_cover_three_effects_with_provenance() -> None:
    snap = await snapshot()
    effects = [r.effect.value for r in snap.permissions.rules]
    assert sorted(effects) == ["allow", "ask", "deny"]
    assert len(snap.permissions.rules) == 3
    assert all(o.source == "config/permissions.yaml" for o in snap.rule_origins)
    assert [o.index for o in snap.rule_origins] == [0, 1, 2]


async def test_env_references_resolve_to_the_single_global_key() -> None:
    # D2/§13: exactly one provider secret is referenced process-wide; this
    # also proves the shipped api_key ${env:} reference actually expands.
    referenced: set[str] = set()
    for path in sorted(CONFIG_DIR.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))

        def walk(node: object) -> None:
            if isinstance(node, dict):
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)
            elif isinstance(node, str):
                referenced.update(re.findall(r"\$\{env:([A-Za-z_]\w*)\}", node))

        walk(data)
    assert referenced == {"OPENAI_API_KEY"}
    snap = await snapshot()
    assert (
        snap.settings.provider.api_key.get_secret_value()
        == TEST_ENVIRON["OPENAI_API_KEY"]
    )


async def test_shipped_config_contains_no_plaintext_secrets() -> None:
    # Mini-version of the T1.6 scanner: sk- shapes, PEM headers, JWTs —
    # across ALL config files including comments (raw bytes, not YAML).
    patterns = [
        re.compile(r"sk-[A-Za-z0-9_-]{10,}"),
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        re.compile(r"eyJ[A-Za-z0-9_-]{10,}\."),
    ]
    offenders = []
    for path in sorted(CONFIG_DIR.rglob("*")):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for pat in patterns:
            if pat.search(text):
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {pat.pattern}")
    assert offenders == []


async def test_example_skill_and_agent_exist_and_stay_in_bounds() -> None:
    skill_files = sorted(CONFIG_DIR.glob("skills/*/SKILL.md"))
    agent_files = sorted(CONFIG_DIR.glob("agents/*.md"))
    assert len(skill_files) == 1 and len(agent_files) == 1

    skill = SkillFrontmatter.model_validate(_frontmatter(skill_files[0]))
    assert skill.name == skill_files[0].parent.name  # slug == directory
    assert skill.allowed_tools is not None
    snap = await snapshot()
    enabled = {n for n in BUILTIN_TOOL_NAMES if getattr(snap.tools.tools, n).enabled}
    # T9.4's rule ahead of T9: a skill may narrow, never widen.
    assert set(skill.allowed_tools) <= enabled

    agent = AgentFrontmatter.model_validate(_frontmatter(agent_files[0]))
    assert agent.name == agent_files[0].stem
    assert set(agent.exclude_tools or ()) <= BUILTIN_TOOL_NAMES
    assert agent.permission_mode in snap.settings.permissions.allowed_modes


async def test_config_readme_documents_every_roster_file() -> None:
    readme = (CONFIG_DIR / "README.md").read_text(encoding="utf-8")
    for filename in (
        "settings.yaml",
        "tools.yaml",
        "permissions.yaml",
        "hooks.yaml",
        "mcp.yaml",
        "plugins.yaml",
    ):
        assert filename in readme, filename
    for anchor in ("${env:", "kill -HUP", "*.local.yaml", "network_mode"):
        assert anchor in readme, anchor
