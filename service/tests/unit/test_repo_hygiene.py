"""Repo hygiene guard tests (docs/plan.md T0.4).

Turns the T0.1a `.gitignore` decisions into executable assertions, per the
original threat model: runtime data (`pw_hash`, session tokens, conversation
bodies, agent artifacts), local overrides (possible plaintext secrets), and
dependency/build junk must never enter git history — where removal cost far
exceeds prevention cost (§8.6).

Three layers, deliberately independent so a single edit cannot pass all:

1. rule-level  — `git check-ignore --no-index` per representative path:
   pattern matching only, unaffected by current tracking status.
2. state-level  — `git ls-files`: nothing matching a runtime-data pattern is
   *tracked right now* (catches `git add -f` and force-adds through loosened
   rules).
3. source-level — the `.gitignore` file itself still carries the
   `!.env.example` negation (its removal would silently un-trackable the
   template that §11 depends on).

If someone "helpfully" loosens a rule, layer 1 goes red; if they also
force-committed something, layer 2 goes red with the exact path printed.
"""

from __future__ import annotations

import subprocess

import pytest

from app.core.env import REPO_ROOT


def git(*args: str) -> subprocess.CompletedProcess[str]:
    # check=False is deliberate: callers inspect returncode as the answer
    # (check-ignore exits 1 for "not ignored", which is a *result*, not an
    # error).
    return subprocess.run(
        ["git", *args],
        check=False,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )


def is_ignored(path: str) -> bool:
    """Rule answer for *path*, independent of the index (--no-index).

    Without it, a tracked-but-also-ignored path reports "not ignored" — the
    check would then depend on tracking state and drift with commits.
    """
    proc = git("check-ignore", "-q", "--no-index", "--", path)
    assert proc.returncode in (0, 1), f"git check-ignore failed: {proc.stderr}"
    return proc.returncode == 0


# --- layer 1: rules must keep excluding the runtime/local surface ------------------

IGNORED_PATHS = [
    # env & secrets (§8.6)
    ".env",
    ".env.local",
    ".env.production",
    # local deployment overrides (§5)
    "config/settings.local.yaml",
    "config/mcp.local.yaml",
    # runtime data (§6, §11 ./data/) — pw_hash, tokens, artifacts, backups
    "data/vanilla.db",
    "data/vanilla.db-wal",
    "data/vanilla.db-shm",
    "data/artifacts/ab/abcdef0123",
    "data/workspaces/some-user/notes.md",
    "data/backups/vanilla.db",
    # any sqlite file, wherever it appears
    "service/vanilla.db",
    "scratch.db-wal",
    # python toolchain caches & venv
    "service/.venv/pyvenv.cfg",
    "service/app/__pycache__/env.cpython-312.pyc",
    "service/.mypy_cache/3.12/cache.0.db",
    "service/.ruff_cache/0.17.6/blob",
    "service/.pytest_cache/CACHEDIR.TAG",
    # web scaffold output (§13-D…/T5.4)
    "web/node_modules/react/package.json",
    "web/.next/BUILD_ID",
    "web/out/index.html",
    "web/next-env.d.ts",
    "web/tsconfig.tsbuildinfo",
    # coverage artifacts (the T0.3-derived gap closed by this TODO)
    ".coverage",
    "service/.coverage",
    "service/.coverage.ip.pid",
    "htmlcov/index.html",
    "cov.json",
    # editor droppings
    "docs/notes.swp",
    ".DS_Store",
]

# Paths that MUST stay trackable: the publishable core (§Documentation
# privacy) plus the placeholders that carry no secrets.
NOT_IGNORED_PATHS = [
    ".gitignore",
    ".env.example",  # the `!.env.example` negation — template, placeholders only
    "Makefile",
    "docs/plan.md",
    "service/pyproject.toml",
    "service/app/core/env.py",
    "service/tests/unit/test_env.py",
    "config/settings.yaml",
    "config/skills/demo/SKILL.md",
]


@pytest.mark.parametrize("path", IGNORED_PATHS)
def test_runtime_and_local_paths_stay_ignored(path: str) -> None:
    assert is_ignored(path), f"{path} is no longer ignored — .gitignore loosened?"


@pytest.mark.parametrize("path", NOT_IGNORED_PATHS)
def test_publishable_paths_stay_trackable(path: str) -> None:
    assert not is_ignored(path), f"{path} became ignored — docs/plan §11 depends on it"


# --- layer 2: nothing forbidden is actually tracked ---------------------------------

FORBIDDEN_TRACKED_PATHSPECS = [
    # every .env* EXCEPT the public template — the glob would otherwise
    # match the legitimately-tracked .env.example; magic exclusions keep
    # this layer consistent with the `!.env.example` negation rule.
    ".env*",
    ":(exclude).env.example",
    "*.local.yaml",
    "data/*",
    "*.db",
    "*.db-shm",
    "*.db-wal",
    "web/node_modules/*",
    "web/.next/*",
    ".coverage",
    ".coverage.*",
    "htmlcov/*",
    "cov.json",
]


def test_no_runtime_data_is_currently_tracked() -> None:
    proc = git("ls-files", "--", *FORBIDDEN_TRACKED_PATHSPECS)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == []


def test_hygiene_essentials_are_tracked() -> None:
    """The guard infrastructure itself must be in the repo, or CI quietly
    stops running it: the template `.env.example` (T0.2/§11) and this very
    test's rule source `.gitignore`."""
    tracked = set(git("ls-files").stdout.splitlines())
    assert {".gitignore", ".env.example"} <= tracked


# --- layer 3: the .gitignore source keeps its load-bearing lines ----------------------


def test_gitignore_keeps_the_env_template_negation() -> None:
    """`!.env.example` after `.env.*`: without it the template falls under
    the secret-file blanket and 'just tightening' silently un-publishes it."""
    rules = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "!.env.example" in rules


def test_gitignore_covers_the_full_runtime_surface() -> None:
    """Pin the categories themselves in source, not only their behavior —
    a deleted line is a design change, which must fail loudly here."""
    rules = set((REPO_ROOT / ".gitignore").read_text(encoding="utf-8").split())
    required = {
        ".env",
        ".env.*",
        "!.env.example",
        "config/*.local.yaml",
        "/data/",
        "*.db",
        "*.db-shm",
        "*.db-wal",
        ".venv/",
        "web/node_modules/",
        "web/.next/",
        ".coverage",
        "htmlcov/",
        "cov.json",
    }
    missing = required - rules
    assert not missing, f".gitignore lost rules: {sorted(missing)}"
