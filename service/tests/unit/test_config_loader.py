"""Unit tests for app.config.loader + app.config.issue (docs/plan.md T1.2).

Verification per the plan column: missing env vars, escaping paths, YAML
syntax errors, many problems reported in ONE pass, fixture-directory covered
(`tests/fixtures/configs/`). On top of the listed cases: source ordering is
deterministic, sha256 sources match the raw bytes, per-file fault isolation
(a broken file never pollutes its siblings' reports or leaks placeholder
garbage into models), issue text never echoes secret values (§8.6), and the
`${...}` grammar rejects `${file:}`/unknown schemes while leaving plain
`${HOME}` text alone.
"""

from __future__ import annotations

import hashlib
import textwrap
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.config.issue import ConfigIssue, loc_to_path
from app.config.loader import CONFIG_FILES, LoadedConfig, load_config_files
from app.config.schema import (
    HookKind,
    MCPTransport,
    NetworkMode,
    PermissionMode,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "configs"

#: The only env vars the committed fixtures reference. Values are fake.
FULL_ENV = {
    "VA_T12_PROVIDER_KEY": "sk-provider-fixture-value",
    "VA_T12_MCP_TOKEN": "tok-fixture-value",
    "VA_T12_EMPTY": "",
}

SETTINGS_OK = """
provider:
  base_url: https://llm.example.com/v1
  api_key: sk-test-not-a-real-key
  model_name: vanilla-mini
"""


def load_case(
    name: str, *, environ: Mapping[str, str] | None = FULL_ENV, config_subdir: str = ""
) -> LoadedConfig:
    """Load a committed fixture tree; repo_root is the fixture directory
    itself (bad-paths nests its config under ``config/``)."""
    root = FIXTURES / name
    return load_config_files(
        repo_root=root,
        config_dir=root / config_subdir,
        environ=dict(environ) if environ is not None else os_environ_default(),
    )


def os_environ_default() -> Any:
    """Sentinel passthrough: None means 'use os.environ' upstream."""
    return None


def load_tree(
    tmp_path: Path, files: dict[str, str], environ: Mapping[str, str] | None = None
) -> LoadedConfig:
    """Write *files* into ``<tmp>/config/`` (standard layout) and load."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        target = config_dir / name
        target.write_text(textwrap.dedent(body).lstrip("\n"), encoding="utf-8")
    return load_config_files(
        repo_root=tmp_path,
        config_dir=config_dir,
        environ=dict(environ) if environ else {},
    )


def issues_by_source(loaded: LoadedConfig) -> dict[str, set[tuple[str, str]]]:
    """source -> {(path, message-fragment-insensitive full message)}."""
    out: dict[str, set[tuple[str, str]]] = {}
    for issue in loaded.issues:
        out.setdefault(issue.source, set()).add((issue.path, issue.message))
    return out


def paths_for(loaded: LoadedConfig, source: str, level: str = "error") -> set[str]:
    return {
        i.path
        for i in loaded.issues
        if i.level == level and i.source.split(":")[0] == source
    }


# ---------------------------------------------------------------------------
# Committed fixtures: the success paths
# ---------------------------------------------------------------------------


def test_valid_minimal_loads_with_defaults_and_warnings() -> None:
    loaded = load_case("valid-minimal")
    assert loaded.errors == ()
    # settings.yaml present; the five optional files each warn...
    assert len(loaded.warnings) == 5
    assert paths_for(loaded, "tools.yaml", "warning") == {""}
    # ...and yield schema defaults, per LoadedConfig's contract.
    assert loaded.tools is not None and loaded.tools.defaults.timeout_s == 60.0
    assert loaded.mcp is not None and loaded.mcp.servers == []
    assert loaded.settings is not None
    assert loaded.settings.limits.max_turns == 50
    assert loaded.settings.sandbox.network_mode is NetworkMode.NONE
    assert loaded.settings.provider.model_name == "vanilla-mini"


def test_valid_full_is_clean_and_expanded() -> None:
    loaded = load_case("valid-full")
    assert loaded.issues == ()
    assert loaded.has_errors is False
    settings = loaded.settings
    assert settings is not None
    # ${env:} was expanded BEFORE validation — the model holds the real value.
    assert (
        settings.provider.api_key.get_secret_value() == FULL_ENV["VA_T12_PROVIDER_KEY"]
    )
    assert settings.provider.timeout_s == 90.0  # int widened
    assert settings.permissions.default_mode is PermissionMode.PLAN
    assert settings.sandbox.image_tag == "2026.11"  # quoted YAML stays a str

    assert loaded.tools is not None
    assert loaded.tools.tools.write_file.enabled is False
    assert loaded.tools.tools.bash.hard_timeout_s == 120.0
    assert loaded.tools.tools.http_fetch.allowed_domains == [
        "docs.example.com",
        "*.example.org",
    ]

    assert loaded.permissions is not None
    assert loaded.permissions.policy.disallow_bypass is True
    assert list(loaded.permissions.mode_presets) == [PermissionMode.PLAN]

    assert loaded.hooks is not None
    assert loaded.hooks.hooks[0].kind is HookKind.COMMAND  # contained path ok
    assert loaded.hooks.hooks[1].kind is HookKind.PYTHON  # module:attr skip

    assert loaded.mcp is not None
    stdio, http = loaded.mcp.servers
    assert stdio.transport is MCPTransport.STDIO
    assert stdio.env["FS_TOKEN"].get_secret_value() == "tok-fixture-value"
    assert (
        http.headers["Authorization"].get_secret_value() == "Bearer tok-fixture-value"
    )

    assert loaded.plugins is not None
    assert loaded.plugins.plugins[0].path == "plugins/demo"


def test_sources_list_all_files_with_real_sha256() -> None:
    loaded = load_case("valid-full")
    names = [name for name, _ in loaded.sources]
    assert names == [filename for filename, _, _ in CONFIG_FILES]
    root = FIXTURES / "valid-full"
    for name, digest in loaded.sources:
        raw = (root / name).read_bytes()
        assert digest == hashlib.sha256(raw).hexdigest()


def test_bad_paths_display_names_are_repo_relative() -> None:
    loaded = load_case("bad-paths", config_subdir="config")
    sources = {i.source.split(":")[0] for i in loaded.issues}
    assert "config/plugins.yaml" in sources


# ---------------------------------------------------------------------------
# Committed fixtures: collect-all problems in one pass
# ---------------------------------------------------------------------------


def test_errors_mixed_reports_every_problem_at_once() -> None:
    loaded = load_case("errors-mixed")
    by_file = issues_by_source(loaded)

    assert {p for p, _ in by_file["settings.yaml"]} == {
        "provider.api_key",
        "provider.model_name",
        "provider.temperature",
        "provider.timeout_s",
        "provider.typo_field",
        "limits.max_turns",
        "unknown_section",
    }
    assert {p for p, _ in by_file["tools.yaml"]} == {"tools.read_fiile"}
    assert {p for p, _ in by_file["permissions.yaml"]} == {
        "default_mode",
        "rules[1].effect",  # §4's example path shape
    }
    # One message per problem, with pydantic's own clear wording.
    messages = dict(by_file["settings.yaml"])
    assert messages["provider.api_key"] == "Field required"
    assert "valid number" in messages["provider.timeout_s"]
    assert "less than or equal to 2" in messages["provider.temperature"]
    assert "Extra inputs are not permitted" in messages["provider.typo_field"]

    # 10 errors, no fail-fast; the three absent optional files still warn.
    assert len(loaded.errors) == 10
    assert len(loaded.warnings) == 3
    # Failed files yield None models; their issues are the whole story.
    assert (
        loaded.settings is None and loaded.tools is None and loaded.permissions is None
    )


def test_issue_ordering_is_file_sequence_and_deterministic() -> None:
    loaded = load_case("errors-mixed")
    order = [i.source.split(":")[0] for i in loaded.errors]
    assert order == sorted(
        order, key=["settings.yaml", "tools.yaml", "permissions.yaml"].index
    )
    assert loaded == load_case("errors-mixed")  # frozen dataclass equality


def test_missing_env_and_forbidden_file_reference() -> None:
    loaded = load_case("bad-env")
    settings_paths = {
        i.path: i.message for i in loaded.errors if i.source.startswith("settings.yaml")
    }
    assert set(settings_paths) == {"provider.api_key", "provider.model_name"}
    assert "VA_T12_UNSET is not set" in settings_paths["provider.api_key"]
    assert "set but empty" in settings_paths["provider.model_name"]
    # Errors name variables; they never echo values.
    for issue in loaded.issues:
        assert "sk-provider" not in issue.message

    mcp_msgs = [i.message for i in loaded.errors if i.source.startswith("mcp.yaml")]
    assert any("${file:...}" in m and "forbidden" in m for m in mcp_msgs)
    # Fault isolation: expansion failure -> no cascading schema errors.
    assert len([i for i in loaded.errors if i.source.startswith("settings")]) == 2
    assert loaded.settings is None and loaded.mcp is None


def test_yaml_syntax_error_is_isolated() -> None:
    loaded = load_case("bad-syntax")
    assert len(loaded.errors) == 1
    issue = loaded.errors[0]
    assert issue.source.startswith("hooks.yaml:")  # file:line for syntax
    assert issue.level == "error" and "YAML syntax error" in issue.message
    # The valid sibling file loaded normally despite the neighbor blowing up.
    assert loaded.settings is not None
    assert loaded.settings.provider.model_name == "vanilla-mini"
    assert len(loaded.warnings) == 4  # tools/permissions/mcp/plugins absent


def test_path_containment_escapes_are_errors() -> None:
    loaded = load_case("bad-paths", config_subdir="config")
    assert paths_for(loaded, "config/plugins.yaml") == {"plugins[0].path"}
    assert paths_for(loaded, "config/hooks.yaml") == {"hooks[0].target"}
    escaping = next(i for i in loaded.errors if i.path == "plugins[0].path")
    assert "outside the config directory" in escaping.message
    # Containment errors do NOT void the model (unlike parse failures): the
    # snapshot layer refuses on has_errors, the diagnosis stays in issues.
    assert loaded.plugins is not None and loaded.settings is not None


# ---------------------------------------------------------------------------
# tmp-tree unit cases: file-level and grammar-level edges
# ---------------------------------------------------------------------------


def test_missing_required_settings_file(tmp_path: Path) -> None:
    loaded = load_tree(tmp_path, {})
    assert paths_for(loaded, "config/settings.yaml") == {""}
    assert "required config file is missing" in loaded.errors[0].message
    assert loaded.has_errors and loaded.settings is None


def test_empty_settings_file_reports_missing_provider(tmp_path: Path) -> None:
    loaded = load_tree(tmp_path, {"settings.yaml": "", "hooks.yaml": ""})
    # Empty required file -> provider Field required (not a syntax error).
    assert {i.path for i in loaded.errors} == {"provider"}
    # Empty optional file is legitimate "no entries": model set, no warning.
    assert loaded.hooks is not None and loaded.hooks.hooks == []


def test_duplicate_yaml_keys_are_rejected(tmp_path: Path) -> None:
    loaded = load_tree(
        tmp_path,
        {
            "settings.yaml": """
        provider:
          base_url: https://llm.example.com/v1
          api_key: sk-test-not-a-real-key
          model_name: one
          model_name: two
    """
        },
    )
    assert len(loaded.errors) == 1
    issue = loaded.errors[0]
    assert "duplicate mapping key" in issue.message
    assert issue.source.endswith(":5")  # line of the second occurrence


def test_multi_document_and_non_mapping_roots_rejected(tmp_path: Path) -> None:
    multi = load_tree(tmp_path, {"settings.yaml": "provider: 1\n---\nx: 2\n"})
    assert any("single YAML document" in i.message for i in multi.errors)
    seq = load_tree(tmp_path, {"settings.yaml": "- 1\n- 2\n"})
    assert any(
        "top level must be a mapping, got sequence" in i.message for i in seq.errors
    )
    # An unhashable (list) mapping key: the duplicate-key scan skips it and
    # yaml's own constructor error surfaces — the scanner never crashes.
    unhashable = load_tree(tmp_path, {"tools.yaml": "? [a, b]\n: c\n"})
    assert any("unhashable key" in i.message for i in unhashable.errors)


def test_yaml_anchors_and_merge_keys_still_work(tmp_path: Path) -> None:
    # The duplicate-key scanner must skip `<<` itself (multi-merge is legal)
    # while normal duplicate keys stay rejected (covered in the dup test).
    loaded = load_tree(
        tmp_path,
        {
            "settings.yaml": SETTINGS_OK,
            "tools.yaml": """
        tools:
          read_file: &read-only { enabled: true }
          grep:
            <<: *read-only
    """,
        },
    )
    assert loaded.errors == ()
    assert loaded.tools is not None
    assert loaded.tools.tools.grep.enabled is True


def test_marker_only_document_is_an_empty_document(tmp_path: Path) -> None:
    loaded = load_tree(tmp_path, {"settings.yaml": SETTINGS_OK, "hooks.yaml": "---\n"})
    # Not a syntax error, not a "missing file" warning: empty content.
    assert loaded.errors == ()
    assert paths_for(loaded, "config/hooks.yaml", "warning") == set()
    assert loaded.hooks is not None and loaded.hooks.hooks == []


def test_display_falls_back_to_config_relative(tmp_path: Path) -> None:
    # Deployment with CONFIG_DIR outside REPO_ROOT: sources name relative
    # to the config dir, never an absolute path.
    (tmp_path / "cfg").mkdir()
    (tmp_path / "cfg" / "settings.yaml").write_text(
        SETTINGS_OK.lstrip(), encoding="utf-8"
    )
    loaded = load_config_files(
        repo_root=tmp_path / "repo", config_dir=tmp_path / "cfg", environ={}
    )
    assert [name for name, _ in loaded.sources] == ["settings.yaml"]
    assert loaded.settings is not None


def test_windows_shaped_plugin_path_is_rejected(tmp_path: Path) -> None:
    # `C:\...` is a literal relative name on POSIX; the schema's path guard
    # rejects it before the containment pass ever sees a model.
    drive = load_tree(
        tmp_path,
        {"plugins.yaml": 'plugins:\n  - name: p\n    path: "C:\\\\plugins\\\\x"\n'},
    )
    assert paths_for(drive, "config/plugins.yaml") == {"plugins[0].path"}


def test_invalid_utf8_and_directory(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "settings.yaml").write_bytes(b"\xff\xfe not utf8")
    (config / "tools.yaml").mkdir()
    loaded = load_config_files(repo_root=tmp_path, config_dir=config, environ={})
    messages = [i.message for i in loaded.errors]
    assert any("not valid UTF-8" in m for m in messages)
    assert any("found a directory" in m for m in messages)


# --- ${env:...} grammar ------------------------------------------------------


def test_reference_errors_matrix(tmp_path: Path) -> None:
    cases = [
        ("${ENV:KEY}", {"KEY": "v"}, "did you mean ${env:...}"),
        ("${vault:x}", {}, "only ${env:VAR}"),
        ("${env:BAD-NAME}", {}, "malformed env reference"),
        ("${env:KEY:-fallback}", {"KEY": "v"}, "malformed env reference"),
        ("${env:MISSING}", {}, "is not set"),
        ("${env:BLANK}", {"BLANK": "  "}, "set but empty"),
    ]
    for template, env, fragment in cases:
        loaded = load_tree(
            tmp_path,
            {
                "settings.yaml": f"""
                provider:
                  base_url: https://llm.example.com/v1
                  api_key: sk-test-not-a-real-key
                  model_name: "{template}"
            """
            },
            environ=env,
        )
        assert len(loaded.errors) == 1, template
        assert fragment in loaded.errors[0].message, template
        assert loaded.settings is None, template  # no cascading schema noise


def test_reference_expansion_cases(tmp_path: Path) -> None:
    # Colon-less ${HOME} stays literal text (shell snippets in notes work).
    loaded = load_tree(
        tmp_path,
        {
            "settings.yaml": """
        provider:
          base_url: https://llm.example.com/v1
          api_key: sk-test-not-a-real-key
          model_name: "echo ${HOME} ok"
    """
        },
    )
    assert loaded.errors == ()
    assert loaded.settings is not None
    assert loaded.settings.provider.model_name == "echo ${HOME} ok"

    # Embedded expansion produces the joined string.
    loaded = load_tree(
        tmp_path,
        {
            "settings.yaml": """
        provider:
          base_url: https://llm.example.com/v1
          api_key: "Bearer ${env:T}!"
          model_name: m
    """
        },
        environ={"T": "tok"},
    )
    assert loaded.settings is not None
    assert loaded.settings.provider.api_key.get_secret_value() == "Bearer tok!"

    # Single pass: substituted content is never re-scanned.
    loaded = load_tree(
        tmp_path,
        {
            "settings.yaml": """
        provider:
          base_url: https://llm.example.com/v1
          api_key: "${env:T}"
          model_name: m
    """
        },
        environ={"T": "${env:OTHER}"},
    )
    assert loaded.errors == ()
    assert loaded.settings is not None
    assert loaded.settings.provider.api_key.get_secret_value() == "${env:OTHER}"


def test_environ_defaults_to_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VA_T12_PROCESS_ENV_UNSET", raising=False)
    config = tmp_path / "config"
    config.mkdir()
    (config / "settings.yaml").write_text(
        SETTINGS_OK.replace(
            "sk-test-not-a-real-key", "${env:VA_T12_PROCESS_ENV_UNSET}"
        ),
        encoding="utf-8",
    )
    loaded = load_config_files(repo_root=tmp_path, config_dir=config)
    assert len(loaded.errors) == 1
    assert "VA_T12_PROCESS_ENV_UNSET is not set" in loaded.errors[0].message


def test_issue_messages_never_echo_expanded_secrets(tmp_path: Path) -> None:
    loaded = load_tree(
        tmp_path,
        {
            "settings.yaml": """
        provider:
          base_url: https://llm.example.com/v1
          api_key: "${env:VA_KEY}"
          model_name: vanilla-mini
        limits:
          max_turns: "lots"
    """
        },
        environ={"VA_KEY": "sk-HUNTER-VALUE"},
    )
    assert paths_for(loaded, "config/settings.yaml") == {"limits.max_turns"}
    assert "sk-HUNTER-VALUE" not in repr(loaded.issues)
    assert "sk-HUNTER-VALUE" not in repr(loaded.sources)


def test_dict_key_enum_error_path_drops_the_bracket_marker(tmp_path: Path) -> None:
    loaded = load_tree(
        tmp_path,
        {
            "permissions.yaml": """
        mode_presets:
          turbo: []
    """
        },
    )
    assert paths_for(loaded, "config/permissions.yaml") == {"mode_presets.turbo"}


# ---------------------------------------------------------------------------
# issue.py units
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("loc", "expected"),
    [
        ((), ""),
        (("provider", "api_key"), "provider.api_key"),
        (("rules", 1, "effect"), "rules[1].effect"),
        (("mode_presets", "turbo", "[key]"), "mode_presets.turbo"),
        (("servers", 0, "args", 2), "servers[0].args[2]"),
        (("a", "str:b"), "a.b"),
    ],
)
def test_loc_to_path_shapes(loc: tuple, expected: str) -> None:
    assert loc_to_path(loc) == expected


def test_config_issue_is_frozen_with_level_helpers() -> None:
    error = ConfigIssue.error("settings.yaml", "provider", "boom")
    warning = ConfigIssue.warning("settings.yaml", "", "meh")
    assert (error.level, warning.level) == ("error", "warning")
    with pytest.raises(ValidationError):
        error.level = "warning"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        ConfigIssue.error("s", "", "m").__setattr__("extra_key", 1)
