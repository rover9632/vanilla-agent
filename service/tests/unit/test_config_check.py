"""Unit tests for app.config.check + the CLI (docs/plan.md T1.6).

Plan column, verbatim: "故意引用不存在工具 → 非零退出；故意放一个 `sk-` →
error；故意写一个近似拼错的部署变量名 → error 且提示正主". Engine-level
counterparts of all three, plus the full T1.6 surface: placeholder audit
mode for unset env references, the §8.11 listen gate, §8.10b sizing,
secret scan (including untracked *.local.yaml — §8.6's false-comfort
point), unknown-file detection, reference integrity across
skills/agents/rules/plugins/hooks, and CLI exit codes / dump masking via
subprocess.

Hermeticity: the engine takes every environment probe as a parameter; CLI
subprocesses get a minimal env with REPO_ROOT pinned at the tmp tree (so
even the developer's real .env cannot leak into expectations).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.config.check import run_config_check
from app.config.issue import ConfigIssue

SERVICE_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = SERVICE_DIR.parent

BIG_RAM = 128 * 1024**3

SETTINGS_OK = """provider:
  base_url: https://llm.example.com/v1
  api_key: "${env:T6_KEY}"
  model_name: test-model
"""


@pytest.fixture(autouse=True)
def _service_is_a_different_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """Audit tests are about config CONTENT; the §8.7 ownership probe
    would otherwise flag every tmp file (runner == owner). Enforcement has
    its own tests in test_config_trust.py."""
    monkeypatch.setattr("app.config.trust._current_uid", lambda: -1)


def build_tree(tmp_path: Path, files: dict[str, str]) -> Path:
    """Write a full config tree (six roster docs at minimum) under tmp."""
    config = tmp_path / "config"
    config.mkdir(parents=True, exist_ok=True)
    roster = {
        "settings.yaml": SETTINGS_OK,
        "tools.yaml": "",
        "permissions.yaml": "",
        "hooks.yaml": "",
        "mcp.yaml": "",
        "plugins.yaml": "",
    }
    roster.update(files)
    for name, body in roster.items():
        target = config / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return tmp_path


def write_resource(tmp_path: Path, rel: str, body: str) -> None:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def skill_body(name: str, allowed: str = "[read_file]") -> str:
    return (
        f"---\nname: {name}\ndescription: test skill\n"
        f"allowed_tools: {allowed}\n---\n\nbody\n"
    )


def agent_body(name: str, extra: str = "") -> str:
    return f"---\nname: {name}\ndescription: test agent\n{extra}---\n\nbody\n"


def check(
    tmp_path: Path,
    *,
    environ: dict[str, str] | None = None,
    listen_addr: str | None = "127.0.0.1:8000",
    memory: int | None = BIG_RAM,
    dotenv_keys: list[str] | None = None,
) -> list[ConfigIssue]:
    issues, _snap = run_config_check(
        repo_root=tmp_path,
        environ={"T6_KEY": "value-not-a-secret", **(environ or {})},
        listen_addr=listen_addr,
        host_memory=lambda: memory,
        dotenv_keys=lambda: dotenv_keys or [],
    )
    return issues


def errs(issues: list[ConfigIssue]) -> list[ConfigIssue]:
    return [i for i in issues if i.level == "error"]


# ---------------------------------------------------------------------------
# Clean tree + placeholder audit mode
# ---------------------------------------------------------------------------


def test_clean_tree_has_zero_findings(tmp_path: Path) -> None:
    tree = build_tree(tmp_path, {})
    assert check(tree) == []


def test_unset_reference_gets_placeholder_and_warning(tmp_path: Path) -> None:
    tree = build_tree(tmp_path, {})
    issues, snapshot = run_config_check(
        repo_root=tree,
        environ={},
        listen_addr="127.0.0.1",
        host_memory=lambda: BIG_RAM,
        dotenv_keys=list,
    )
    warnings = [i for i in issues if i.level == "warning"]
    assert any(
        "T6_KEY is unset" in i.message and "placeholder" in i.message for i in warnings
    )
    # Audit mode still assembles — that is the whole point on CI machines.
    assert snapshot is not None
    # ...and the placeholder never looks like a planted secret to the scan.
    assert errs(issues) == []


# ---------------------------------------------------------------------------
# Reference integrity (plan column #1: 引用不存在工具 → error)
# ---------------------------------------------------------------------------


def test_skill_referencing_disabled_tool_is_error(tmp_path: Path) -> None:
    tree = build_tree(
        tmp_path,
        {
            "tools.yaml": """
tools:
  write_file: {enabled: false}
"""
        },
    )
    write_resource(
        tree,
        "config/skills/writer/SKILL.md",
        skill_body("writer", allowed="[write_file]"),
    )
    issues = check(tree)
    assert len(errs(issues)) == 1
    assert "disables" in errs(issues)[0].message
    assert errs(issues)[0].source.endswith("SKILL.md")


def test_skill_referencing_unknown_tool_is_error(tmp_path: Path) -> None:
    tree = build_tree(tmp_path, {})
    write_resource(
        tree, "config/skills/s1/SKILL.md", skill_body("s1", allowed="[read_fiile]")
    )
    issues = check(tree)
    assert len(errs(issues)) == 1
    assert "unknown tool reference" in errs(issues)[0].message


def test_mcp_server_references(tmp_path: Path) -> None:
    tree = build_tree(
        tmp_path,
        {
            "mcp.yaml": """
servers:
  - {name: live-server, transport: stdio, command: mcp-a}
  - {name: dead-server, transport: stdio, command: mcp-b, enabled: false}
"""
        },
    )
    write_resource(
        tree,
        "config/skills/m/SKILL.md",
        skill_body(
            "m",
            allowed='["mcp__live-server__t", "mcp__dead-server__t", '
            '"mcp__ghost-server__t", "mcp__*__t"]',
        ),
    )
    issues = check(tree)
    messages = " | ".join(i.message for i in issues)
    assert "ghost-server" in messages and "does not register" in messages
    assert "dead-server" in messages and "disabled" in messages
    # live-server fine; wildcard opens namespace with servers registered
    assert len(errs(issues)) == 1  # only ghost-server is an error


def test_agent_model_and_mode_are_bounded(tmp_path: Path) -> None:
    tree = build_tree(tmp_path, {})
    write_resource(
        tree,
        "config/agents/a1.md",
        agent_body("a1", extra="model: not-allowed\npermission_mode: bypass\n"),
    )
    issues = check(tree)
    paths = {i.path for i in errs(issues)}
    assert paths == {"model", "permission_mode"}
    assert any("allowed_models" in i.message for i in errs(issues))


def test_permission_rule_tool_segments_checked(tmp_path: Path) -> None:
    tree = build_tree(
        tmp_path,
        {
            "permissions.yaml": """
rules:
  - {effect: allow, pattern: read_file}
  - {effect: ask, pattern: "read_fiile"}
  - {effect: ask, pattern: "mcp__ghost__*"}
  - {effect: ask, pattern: "*"}
"""
        },
    )
    issues = check(tree)
    bad_paths = {i.path for i in errs(issues)}
    assert bad_paths == {"rules[1].pattern", "rules[2].pattern"}


def test_plugin_directory_and_hook_script_must_exist(tmp_path: Path) -> None:
    tree = build_tree(
        tmp_path,
        {
            "plugins.yaml": """
plugins:
  - {name: p-present, path: config/plugins/p-present}
  - {name: p-missing, path: config/plugins/p-missing}
""",
            "hooks.yaml": """
hooks:
  - {event: PreToolUse, matcher: bash, kind: command,
     target: config/hooks/guard.sh}
""",
        },
    )
    (tree / "config" / "plugins" / "p-present").mkdir(parents=True)
    issues = check(tree)
    error_paths = {i.path for i in errs(issues)}
    assert "plugins[1].path" in error_paths  # missing directory
    assert "hooks[0].target" in error_paths  # missing script
    assert any(
        "plugin.json" in i.message for i in issues if i.level == "warning"
    )  # present but manifest-less
    # make every declared thing exist -> clean
    write_resource(tree, "config/hooks/guard.sh", "#!/bin/sh\nexit 0\n")
    # T1.7 trust audit joins the party: the spawnable script also needs
    # its exec bit (argv-direct spawn, §8.7).
    os.chmod(tree / "config/hooks/guard.sh", 0o755)
    for name in ("p-present", "p-missing"):
        manifest = tree / "config/plugins" / name / ".vanilla-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text("{}", encoding="utf-8")
    assert check(tree) == []


# ---------------------------------------------------------------------------
# Secret scan (plan column #2: 故意放一个 sk- → error)
# ---------------------------------------------------------------------------


def test_planted_sk_secret_is_an_error(tmp_path: Path) -> None:
    tree = build_tree(
        tmp_path,
        {
            "mcp.yaml": """
servers:
  - name: s1
    transport: streamable_http
    url: https://mcp.example.com/rpc
    headers:
      Authorization: Bearer sk-reallookingsecret123
"""
        },
    )
    issues = check(tree)
    hits = [i for i in errs(issues) if "plaintext-secret" in i.message]
    assert len(hits) == 1
    assert "sk-style key" in hits[0].message
    assert hits[0].source.startswith("config/mcp.yaml:")


def test_secret_scan_covers_untracked_local_yaml(tmp_path: Path) -> None:
    """§8.6's false-comfort point: the gate-closed overlay is STILL scanned."""
    tree = build_tree(tmp_path, {})
    write_resource(
        tree,
        "config/settings.local.yaml",
        """provider:
  base_url: https://llm.example.com/v1
  api_key: sk-plantedinlocalfile99
  model_name: test-model
""",
    )
    issues = check(tree)  # allow_local defaults to False
    assert any("sk-style key" in i.message for i in errs(issues))


def test_secret_scan_other_patterns_and_ignore_marker(tmp_path: Path) -> None:
    tree = build_tree(
        tmp_path,
        {
            "hooks.yaml": "# config/hooks.yaml\nhooks: []\n",
        },
    )
    write_resource(
        tree,
        "config/plugins/p/notes.md",
        """
-----BEGIN RSA PRIVATE KEY-----
MIIB...
""",
    )
    issues = check(tree)
    assert any("PEM private-key header" in i.message for i in errs(issues))

    # ignore marker suppresses its own line only — never a neighbor
    tree2 = build_tree(
        tmp_path / "t2",
        {
            "tools.yaml": (
                "defaults: {timeout_s: 60}"
                "  # doc sample sk-notrealsecretvalue config-check:ignore\n"
                "tools: {read_file: {timeout_s: 5}}"
                "  # sk-secondplantedsecret\n"
            )
        },
    )
    hits = [i for i in check(tree2) if "plaintext-secret" in i.message]
    assert len(hits) == 1
    assert hits[0].source.endswith(":2")  # the unmarked second line

    # long base64 runs error in YAML, tolerated in prose
    tree3 = build_tree(tmp_path / "t3", {})
    write_resource(tree3, "config/skills/s/SKILL.md", skill_body("s") + "A" * 60 + "\n")
    # valid skill, prose carries a data-URI-ish blob: markdown is scanned
    # for keys/PEM/JWT only — not base64 runs.
    assert check(tree3) == []
    tree4 = build_tree(tmp_path / "t4", {"hooks.yaml": f"hooks: []\n# {'A' * 60}\n"})
    hits = [i for i in check(tree4) if "long base64" in i.message]
    assert len(hits) == 1


def test_jwt_hit_wildcard_without_servers_and_skill_model(tmp_path: Path) -> None:
    # JWT shape in a yaml value
    tree = build_tree(
        tmp_path,
        {
            "mcp.yaml": """
servers: []
""",
        },
    )
    write_resource(tree, "config/plugins/jwt.md", "not scanned prose\n")
    write_resource(
        tree,
        "config/settings.local.yaml",
        SETTINGS_OK.replace(
            "${env:T6_KEY}",
            "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NS5zdHJpbmcifQ.sig",
        ),
    )
    issues = check(tree)
    assert any("JWT" in i.message for i in errs(issues))

    # wildcard reference with NO registered server -> error
    tree2 = build_tree(tmp_path / "t2", {})
    write_resource(
        tree2, "config/skills/w/SKILL.md", skill_body("w", allowed='["mcp__*__t"]')
    )
    assert any("no server is registered" in i.message for i in errs(check(tree2)))

    # skill model outside allowed_models -> error (mirrors the agent rule)
    tree3 = build_tree(tmp_path / "t3", {})
    write_resource(
        tree3,
        "config/skills/sm/SKILL.md",
        """---
name: sm
description: d
model: ghost-model
---

body
""",
    )
    hits = [i for i in errs(check(tree3)) if i.path == "model"]
    assert len(hits) == 1 and "ghost-model" in hits[0].message


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ("no frontmatter at all", "missing YAML frontmatter"),
        ("---\nname: x\n", "unterminated YAML frontmatter"),
        ("---\n{invalid: [\n---\n", "frontmatter YAML is invalid"),
        ("---\n- just\n- a\n- list\n---\n", "frontmatter must be a mapping"),
    ],
)
def test_frontmatter_failure_modes_report_cleanly(tmp_path, body, fragment) -> None:
    tree = build_tree(tmp_path, {})
    write_resource(tree, "config/skills/bad/SKILL.md", body)
    issues = check(tree)
    assert any(fragment in i.message for i in errs(issues))


def test_host_memory_probe_reads_meminfo_shapes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.config import check as check_mod

    fake = tmp_path / "meminfo"
    fake.write_text("MemTotal:       32768 kB\nMemFree: 1 kB\n")
    real = Path
    monkeypatch.setattr(
        check_mod, "Path", lambda *_a, **_k: real("/proc/meminfo") if not _a else fake
    )
    assert check_mod.host_total_memory_bytes() == 32768 * 1024
    missing = tmp_path / "nope"
    monkeypatch.setattr(check_mod, "Path", lambda *_a, **_k: missing)
    assert check_mod.host_total_memory_bytes() is None


def test_default_dotenv_key_audit_uses_runtime_repo_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import check as check_mod

    (tmp_path / ".env").write_text("SOME_KEY=1\nOTHER_KEY=2\n", encoding="utf-8")
    monkeypatch.setattr(check_mod, "default_dotenv_path", lambda: tmp_path / ".env")
    assert check_mod.default_dotenv_key_audit() == ["OTHER_KEY", "SOME_KEY"]


# ---------------------------------------------------------------------------
# Unknown files + dotenv typos (plan column #3: 近似拼错 → error 提示正主)
# ---------------------------------------------------------------------------


def test_unknown_config_files_warn_with_suggestions(tmp_path: Path) -> None:
    tree = build_tree(tmp_path, {})
    write_resource(tree, "config/settings.local.yml", "provider: {}\n")
    write_resource(tree, "config/notes.txt", "hi\n")
    (tree / "config" / "scratch").mkdir()
    (tree / "config" / "scratch" / "x.txt").write_text("hi\n")
    issues = check(tree)
    warnings = [i.message for i in issues if i.level == "warning"]
    assert any("did you mean" in m and "settings.local.yaml" in m for m in warnings)
    assert any("unknown file" in m for m in warnings)
    assert any("unexpected directory" in m for m in warnings)
    assert errs(issues) == []  # unknown files warn, never block


def test_dotenv_near_miss_typo_points_at_the_real_variable(tmp_path: Path) -> None:
    tree = build_tree(tmp_path, {})
    issues = check(
        tree,
        dotenv_keys=[
            "SESSION_TTL_DAY",  # near-miss of SESSION_TTL_DAYS
            "SECRET_MASTER_EXTRA",  # legit extra key, tolerated
            "MY_PROXY_URL",  # unrelated, tolerated
        ],
    )
    typed = errs(issues)
    assert len(typed) == 1
    assert typed[0].source == ".env" and typed[0].path == "SESSION_TTL_DAY"
    assert "did you mean SESSION_TTL_DAYS" in typed[0].message
    assert "SECRET_MASTER_EXTRA" not in " ".join(i.message for i in issues)


# ---------------------------------------------------------------------------
# §8.11 listen gate + §8.10b sizing
# ---------------------------------------------------------------------------


def test_open_registration_listen_gate(tmp_path: Path) -> None:
    tree = build_tree(tmp_path, {})
    # non-loopback + open -> error (§8.11, the T1.5-mandated visibility)
    issues = check(tree, listen_addr="0.0.0.0:8000")
    assert any(i.level == "error" and "non-loopback" in i.message for i in issues)
    # undeclared -> warning (the check never guesses)
    assert any(
        "LISTEN_ADDR undeclared" in i.message
        for i in check(tree, listen_addr=None)
        if i.level == "warning"
    )
    # loopback forms are clean
    for addr in ("127.0.0.1", "127.5.5.5:8000", "localhost:3000", "::1", "[::1]:8080"):
        assert not [
            i for i in check(tree, listen_addr=addr) if "registration" in i.path
        ], addr


def test_closed_registration_needs_no_listen_declaration(tmp_path: Path) -> None:
    tree = build_tree(
        tmp_path,
        {
            "settings.yaml": SETTINGS_OK
            + """
registration:
  open: false
"""
        },
    )
    assert check(tree, listen_addr="0.0.0.0") == []


def test_container_sizing_warns_on_overshoot_and_unknown_ram(tmp_path: Path) -> None:
    tree = build_tree(tmp_path, {})
    # 8 × 2g = 16 GiB needs >= 48 GiB host RAM for the 1/3 rule (§8.10b)
    assert any(
        "§8.10b" in i.message
        for i in check(tree, memory=16 * 1024**3)
        if i.level == "warning"
    )
    assert any(
        "host RAM unknown" in i.message
        for i in check(tree, memory=None)
        if i.level == "warning"
    )
    assert not any("§8.10b" in i.message for i in check(tree, memory=64 * 1024**3))


# ---------------------------------------------------------------------------
# The real shipped config must pass (make lint depends on it)
# ---------------------------------------------------------------------------


def test_repo_config_passes_with_zero_errors() -> None:
    issues, snapshot = run_config_check(
        repo_root=REPO_ROOT,
        environ={"OPENAI_API_KEY": "sk-test-not-a-real-key"},
        listen_addr="127.0.0.1:8000",
        host_memory=lambda: BIG_RAM,
        dotenv_keys=list,
    )
    assert errs(issues) == []
    assert snapshot is not None


# ---------------------------------------------------------------------------
# CLI: subprocess exit codes and dump masking
# ---------------------------------------------------------------------------


def _cli_env(tmp_path: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    return {
        # PATH is added by _run_cli from the real environ; REPO_ROOT pins the
        # dotenv lookup away from the developer's actual .env.
        "REPO_ROOT": str(tmp_path),
        "T6_KEY": "value-not-a-secret",
        "LISTEN_ADDR": "127.0.0.1:8000",
        **(extra or {}),
    }


def _run_cli(
    tree: Path, *args: str, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    import os as _os

    env = {"PATH": _os.environ["PATH"], **_cli_env(tree, env_extra)}
    return subprocess.run(
        [sys.executable, "-m", "app.config", *args, "--repo-root", str(tree)],
        check=False,  # exit codes ARE the assertions here
        cwd=SERVICE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_cli_check_exit_codes(tmp_path: Path) -> None:
    tree = build_tree(tmp_path, {})
    clean = _run_cli(tree, "check")
    assert clean.returncode == 0
    assert "config-check: 0 error(s)" in clean.stdout

    write_resource(
        tree, "config/skills/bad/SKILL.md", skill_body("bad", allowed="[no_such_tool]")
    )
    broken = _run_cli(tree, "check")
    assert broken.returncode == 1  # plan column: 非零退出
    assert "[ERROR" in broken.stdout
    assert "config-check: 1 error(s)" in broken.stdout


def test_cli_check_listens_to_env_listen_addr(tmp_path: Path) -> None:
    tree = build_tree(tmp_path, {})
    result = _run_cli(tree, "check", env_extra={"LISTEN_ADDR": "0.0.0.0"})
    assert result.returncode == 1
    assert "non-loopback" in result.stdout


def test_cli_dump_masks_secrets_and_lists_resources(tmp_path: Path) -> None:
    tree = build_tree(tmp_path, {})
    write_resource(
        tree,
        "config/skills/demo/SKILL.md",
        skill_body("demo", allowed="[read_file, glob]"),
    )
    write_resource(tree, "config/agents/helper.md", agent_body("helper"))
    result = _run_cli(tree, "dump", env_extra={"T6_KEY": "SUPERSECRET9999"})
    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert "SUPERSECRET9999" not in result.stdout
    assert report["provider"]["api_key"] == "**********"
    assert [s["slug"] for s in report["skills"]] == ["demo"]
    assert [a["slug"] for a in report["agents"]] == ["helper"]
    assert report["tools"]["disabled"] == []  # default catalog, nothing off
    assert report["version"] == 1
    assert report["sources"][0]["file"] == "config/settings.yaml"


def test_cli_dump_refuses_unbootable_config(tmp_path: Path) -> None:
    tree = build_tree(tmp_path, {"settings.yaml": "provider: [broken\n"})
    result = _run_cli(tree, "dump")
    assert result.returncode == 1
    assert "refused" in result.stderr


def test_app_main_marker_imports_clean_and_refuses_to_run() -> None:
    marker = subprocess.run(
        [sys.executable, "-c", "import app.__main__"],
        check=False,
        cwd=SERVICE_DIR,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert marker.returncode == 0 and marker.stdout == ""
    run = subprocess.run(
        [sys.executable, "-m", "app"],
        check=False,
        cwd=SERVICE_DIR,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode != 0
    assert "T5.2" in run.stderr
