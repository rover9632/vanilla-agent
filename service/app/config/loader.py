"""Config file loading: read -> expand -> validate -> collect (T1.2).

The loader turns a ``config/`` directory into typed schema models
(:mod:`app.config.schema`) plus a complete list of :class:`ConfigIssue`
findings. Contract highlights (docs/plan.md §4/§5):

- **Collect, never fail-fast**: every readable problem in every file is
  reported in one pass — YAML syntax errors, missing/failed env references,
  schema violations (all of them per file), path escapes, missing required
  files. Callers check ``LoadedConfig.has_errors``; they do not catch
  exceptions per file.
- **``${env:VAR}`` expansion happens before validation**, so schema models
  only ever see resolved values (schema.py's docstring states the same from
  its side). Rules: exactly ``${env:NAME}`` with a valid identifier is
  expanded — whole-string or embedded in a larger string; ``${file:...}`` is
  rejected by name (plan §5 forbids file indirection); any other
  ``${scheme:...}`` is a clear error (this catches ``${ENV:X}`` typos that
  would otherwise ship a literal placeholder to the provider SDK); a bare
  ``${HOME}`` with no colon is ordinary literal text (shell snippets in
  ``note`` fields stay writable). A var that is unset *or empty* is an error
  naming the variable — values are never echoed (§8.6). Expansion is
  single-pass: substituted content is never re-scanned for references.
- **Path containment** (§5): every declared path is repo-root-relative and
  must stay inside the config directory once normalized. Which fields are
  paths grows with the milestones — today: ``plugins[].path`` and
  ``hooks[].target`` for ``kind: command`` (python targets are
  ``module:attr``, not paths; skill/agent trees arrive with T9/T10). The
  check is lexical: symlink/ownership semantics belong to the trust-boundary
  work (T1.7), file existence to config-check (T1.6).
- **Per-file fault isolation**: if a file's YAML is broken or an expansion
  failed, its schema validation is skipped — the reported finding is the
  actionable one; re-validating leftover placeholder text would only add
  noise. Other files still load and report.
- ``settings.yaml`` is required; the other five files are optional and yield
  their schema defaults, recorded as a *warning* so "file vanished" is always
  visible in dump/config-check output (ADR-015: absent config must not be
  silently different from empty config).
- YAML is parsed with ``SafeLoader`` only (never a constructor-capable
  loader), duplicate mapping keys are an error instead of yaml's silent
  last-wins, and a document must be a single top-level mapping.
- ``*.local.yaml`` deployment overrides (T1.4, plan §5) are discovered by
  :func:`load_local_overlays`: only when the caller explicitly enables them
  (the ``CONFIG_ALLOW_LOCAL`` wiring), and one such file *replaces* its
  target document wholesale; a stray ``foo.local.yaml`` that targets no
  known stem is an error, never a guess.

The API is synchronous and side-effect-free (pure filesystem reads); T1.3's
``load_config()`` wraps it into the async contract of §4 and feeds the
snapshot with ``sources`` (sha256 per file that composed the config).
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final

import yaml
from pydantic import BaseModel, ValidationError

from app.config.issue import ConfigIssue, loc_to_path
from app.config.schema import (
    HookConfig,
    HookKind,
    MCPConfig,
    PermissionsConfig,
    PluginsConfig,
    Settings,
    ToolConfig,
)

# ---------------------------------------------------------------------------
# File roster — the closed set of root config documents (plan §5)
# ---------------------------------------------------------------------------

#: ``(filename, root model, required?)`` in load order. Deterministic
#: ordering is contract: two loads of the same directory yield the same
#: issue sequence (a test pins it).
CONFIG_FILES: Final[tuple[tuple[str, type[BaseModel], bool], ...]] = (
    ("settings.yaml", Settings, True),
    ("tools.yaml", ToolConfig, False),
    ("permissions.yaml", PermissionsConfig, False),
    ("hooks.yaml", HookConfig, False),
    ("mcp.yaml", MCPConfig, False),
    ("plugins.yaml", PluginsConfig, False),
)

#: Suffix of the optional deployment override documents (plan §5).
LOCAL_SUFFIX: Final = ".local.yaml"

#: stem (``settings``...) -> root model; drives overlay target resolution.
_ROSTER_BY_STEM: Final[dict[str, type[BaseModel]]] = {
    filename.removesuffix(".yaml"): model_cls for filename, model_cls, _ in CONFIG_FILES
}

# ---------------------------------------------------------------------------
# ${env:...} reference machinery
# ---------------------------------------------------------------------------

#: A reference token is ``${scheme:body}``; bare ``${X}`` (no colon) is
#: ordinary literal text.
_REF_RE: Final = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):([^{}]*)\}")
_ENV_NAME_RE: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


#: Sentinel for "mapping lookup found nothing" (None is a legal value).
class _Miss:
    """Distinct singleton types so `is` checks narrow the union for the
    type checker instead of leaking sentinels into later branches."""


_MISS: Final = _Miss()

# ---------------------------------------------------------------------------
# YAML parsing: SafeLoader + duplicate-key refusal
# ---------------------------------------------------------------------------


class _DuplicateKeyError(yaml.YAMLError):
    """Raised mid-construct when a mapping spells one key twice."""

    def __init__(self, mark: Any, key: Any) -> None:
        super().__init__(f"duplicate mapping key {key!r}")
        self.mark = mark
        self.key = key


class _ConfigLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate mapping keys.

    PyYAML's default ``construct_mapping`` lets the last duplicate silently
    win — for a security-relevant config tree (permission rules, plugin trust
    flags) that is exactly the drift ADR-015 exists to prevent. YAML merge
    keys (``<<``) keep their normal multi-entry semantics.
    """

    def construct_mapping(
        self, node: yaml.MappingNode, deep: bool = False
    ) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            if key_node.value == "<<":
                continue
            try:
                key = self.construct_object(key_node, deep=False)
                hash(key)
            except TypeError:
                continue  # unhashable key: super() reports the type problem
            if key in seen:
                raise _DuplicateKeyError(key_node.start_mark, key) from None
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _kind_name(value: Any) -> str:
    return {dict: "mapping", list: "sequence", type(None): "null"}.get(
        type(value), type(value).__name__
    )


# ---------------------------------------------------------------------------
# Loaded result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoadedConfig:
    """Outcome of one loader pass.

    ``has_errors`` is the gate: when false, every model below is populated
    (missing optional files yield their schema defaults). When true, the
    model of an affected file is ``None`` and *only the issue list is
    trustworthy* — T1.3 refuses to build a snapshot in that state.

    ``sources`` lists ``(display path, sha256 of raw bytes)`` for every file
    actually read, in load order; §4's snapshot copies it verbatim.
    """

    settings: Settings | None = None
    tools: ToolConfig | None = None
    permissions: PermissionsConfig | None = None
    hooks: HookConfig | None = None
    mcp: MCPConfig | None = None
    plugins: PluginsConfig | None = None
    issues: tuple[ConfigIssue, ...] = ()
    sources: tuple[tuple[str, str], ...] = ()

    @property
    def has_errors(self) -> bool:
        return any(i.level == "error" for i in self.issues)

    @property
    def errors(self) -> tuple[ConfigIssue, ...]:
        return tuple(i for i in self.issues if i.level == "error")

    @property
    def warnings(self) -> tuple[ConfigIssue, ...]:
        return tuple(i for i in self.issues if i.level == "warning")


# ---------------------------------------------------------------------------
# Stage 1: read (sentinel-tagged so "missing" differs from "unreadable")
# ---------------------------------------------------------------------------


class _MissingFile:
    """Sentinel: the path does not exist (vs. exists-but-unreadable)."""


_MISSING_FILE: Final = _MissingFile()


def display_name(path: Path, config_dir: Path, repo_root: Path) -> str:
    """Shortest honest reference: repo-relative when the config dir lives in
    the repo, else config-relative — §4's ``source`` shape. A *path* is
    always ``config_dir/filename``, so the second form cannot fail."""
    try:
        return str(path.relative_to(repo_root))
    except ValueError:
        return str(path.relative_to(config_dir))


def _read_file(path: Path) -> tuple[bytes | _MissingFile | None, ConfigIssue | None]:
    """``(_MISSING_FILE, None)`` when absent; ``(bytes, None)``; or
    ``(None, error issue)`` for unreadable entries."""
    try:
        return path.read_bytes(), None
    except FileNotFoundError:
        return _MISSING_FILE, None
    except IsADirectoryError:
        return None, ConfigIssue.error(
            "", "", "expected a YAML file, found a directory"
        )
    except OSError as exc:
        reason = exc.strerror or type(exc).__name__
        return None, ConfigIssue.error("", "", f"cannot read file ({reason})")


# ---------------------------------------------------------------------------
# Stage 2: parse YAML
# ---------------------------------------------------------------------------


def _parse_yaml(text: str, source: str) -> tuple[Any, list[ConfigIssue]]:
    """Return ``(mapping-or-None, issues)``; issues non-empty means failed.

    ``None`` is the empty-document case (folds into model defaults).
    """
    if not text.strip():
        return None, []
    try:
        docs = list(yaml.load_all(text, Loader=_ConfigLoader))
    except _DuplicateKeyError as exc:
        return {}, [
            ConfigIssue.error(
                f"{source}:{exc.mark.line + 1}",
                "",
                f"duplicate mapping key {exc.key!r} — PyYAML's silent last-wins "
                "is refused in config files",
            )
        ]
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"{source}:{mark.line + 1}" if mark is not None else source
        detail = getattr(exc, "problem", None) or str(exc).splitlines()[0]
        return {}, [ConfigIssue.error(where, "", f"YAML syntax error: {detail}")]
    if len(docs) > 1:
        return {}, [
            ConfigIssue.error(
                source,
                "",
                f"expected a single YAML document, found {len(docs)} "
                "(`---` separators are not allowed in config files)",
            )
        ]
    data = docs[0]
    if data is None:
        return None, []
    if not isinstance(data, dict):
        return {}, [
            ConfigIssue.error(
                source, "", f"top level must be a mapping, got {_kind_name(data)}"
            )
        ]
    return data, []


# ---------------------------------------------------------------------------
# Stage 3: expand ${env:...}
# ---------------------------------------------------------------------------


def _expand_string(
    value: str,
    *,
    env: Mapping[str, str],
    source: str,
    path: str,
    issues: list[ConfigIssue],
) -> str:
    def _replace(match: re.Match[str]) -> str:
        scheme, body = match.group(1), match.group(2)
        if scheme == "file":
            issues.append(
                ConfigIssue.error(
                    source,
                    path,
                    "unsupported reference ${file:...} — file indirection is "
                    "forbidden; inline the value or use ${env:VAR} (plan §5)",
                )
            )
            return match.group(0)
        if scheme != "env":
            hint = (
                " — did you mean ${env:...}?"
                if scheme.lower() == "env"
                else " (only ${env:VAR} references are supported)"
            )
            issues.append(
                ConfigIssue.error(
                    source, path, f"unknown reference scheme '${{{scheme}:...}}'{hint}"
                )
            )
            return match.group(0)
        if not _ENV_NAME_RE.match(body):
            issues.append(
                ConfigIssue.error(
                    source,
                    path,
                    f"malformed env reference '${{env:{body}}}' — expected "
                    "${env:VAR_NAME}; defaults and indirection suffixes are "
                    "not supported",
                )
            )
            return match.group(0)
        found = env.get(body, _MISS)
        if found is _MISS:
            issues.append(
                ConfigIssue.error(
                    source,
                    path,
                    f"environment variable {body} is not set (referenced as "
                    f"${{env:{body}}})",
                )
            )
            return match.group(0)
        if not str(found).strip():
            issues.append(
                ConfigIssue.error(
                    source,
                    path,
                    f"environment variable {body} is set but empty (referenced "
                    f"as ${{env:{body}}})",
                )
            )
            return match.group(0)
        return str(found)

    return _REF_RE.sub(_replace, value)


def _expand_node(
    node: Any,
    *,
    env: Mapping[str, str],
    source: str,
    path: str,
    issues: list[ConfigIssue],
) -> Any:
    """Recursively expand references in *values*; keys are never expanded.

    Single pass: substituted content is returned verbatim, never re-scanned.
    """
    if isinstance(node, str):
        if "${" not in node:
            return node  # overwhelmingly the common case
        return _expand_string(node, env=env, source=source, path=path, issues=issues)
    if isinstance(node, dict):
        return {
            key: _expand_node(
                value,
                env=env,
                source=source,
                path=f"{path}.{key}" if path else str(key),
                issues=issues,
            )
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [
            _expand_node(
                item, env=env, source=source, path=f"{path}[{index}]", issues=issues
            )
            for index, item in enumerate(node)
        ]
    return node


# ---------------------------------------------------------------------------
# Stage 4: schema-validate one document
# ---------------------------------------------------------------------------


def _validate_model(
    model_cls: type[BaseModel], data: Any, source: str
) -> tuple[BaseModel | None, list[ConfigIssue]]:
    try:
        return model_cls.model_validate(data), []
    except ValidationError as exc:
        issues = [
            ConfigIssue.error(source, loc_to_path(tuple(err["loc"])), str(err["msg"]))
            for err in exc.errors(include_url=False)
        ]
        return None, issues


# ---------------------------------------------------------------------------
# Stage 5: path containment (typed pass over the validated models)
# ---------------------------------------------------------------------------


def resolve_repo_relative(repo_root: Path, value: str) -> str:
    """Lexical join + normalization (no filesystem access, no symlink
    resolution — that threat model is T1.7's). An absolute *value* replaces
    the root on join (pathlib semantics), so it fails containment downstream
    even though the schema already blocks that shape."""
    return os.path.normpath(str(PurePosixPath(str(repo_root)) / value))


def path_within(base: str | Path, normalized: str) -> bool:
    """True iff the already-normalized *normalized* sits at/under *base*."""
    b = os.path.normpath(str(base)).rstrip("/") or "/"
    return normalized == b or normalized.startswith(b + "/")


def _path_containment_issues(
    loaded: LoadedConfig,
    *,
    config_dir: Path,
    repo_root: Path,
    source_of: Mapping[str, str],
) -> list[ConfigIssue]:
    issues: list[ConfigIssue] = []
    base = os.path.normpath(str(config_dir))
    if loaded.plugins is not None:
        for index, plugin in enumerate(loaded.plugins.plugins):
            resolved = resolve_repo_relative(repo_root, plugin.path)
            if not path_within(base, resolved):
                issues.append(
                    ConfigIssue.error(
                        source_of["plugins.yaml"],
                        f"plugins[{index}].path",
                        f"path {plugin.path!r} resolves to {resolved!r}, "
                        f"outside the config directory {base!r} — declared paths "
                        "must stay within config/ (plan §5)",
                    )
                )
    if loaded.hooks is not None:
        for index, hook in enumerate(loaded.hooks.hooks):
            if hook.kind is not HookKind.COMMAND:
                continue  # python targets are module:attr, not paths
            resolved = resolve_repo_relative(repo_root, hook.target)
            if not path_within(base, resolved):
                issues.append(
                    ConfigIssue.error(
                        source_of["hooks.yaml"],
                        f"hooks[{index}].target",
                        f"hook script {hook.target!r} resolves to {resolved!r}, "
                        f"outside the config directory {base!r} (plan §5)",
                    )
                )
    return issues


# ---------------------------------------------------------------------------
# Single-document ingestion (roster files and *.local.yaml share this path)
# ---------------------------------------------------------------------------


def _ingest_document(
    *,
    path: Path,
    display: str,
    model_cls: type[BaseModel],
    env: Mapping[str, str],
) -> tuple[Any | _MissingFile | None, list[ConfigIssue], tuple[str, str] | None]:
    """read -> decode -> parse -> expand -> validate one document.

    Returns ``(model, [], source)`` on success, ``(_MISSING_FILE, [], None)``
    when absent, ``(None, issues, source)`` on any failure. ``source``
    records the sha256 of every file whose bytes were actually read —
    including broken ones: *which bytes* failed matters (T1.6 scans local
    overlays for plaintext secrets whether or not they loaded).
    """
    raw, read_issue = _read_file(path)
    if read_issue is not None:
        return None, [ConfigIssue.error(display, "", read_issue.message)], None
    if isinstance(raw, _MissingFile):
        return _MISSING_FILE, [], None
    assert raw is not None  # both read failure modes returned above
    source = (display, hashlib.sha256(raw).hexdigest())
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return (
            None,
            [
                ConfigIssue.error(
                    display, "", f"file is not valid UTF-8 (byte offset {exc.start})"
                )
            ],
            source,
        )
    data, parse_issues = _parse_yaml(text, display)
    if parse_issues:
        return None, parse_issues, source
    expand_issues: list[ConfigIssue] = []
    expanded = _expand_node(
        data or {}, env=env, source=display, path="", issues=expand_issues
    )
    if any(issue.level == "error" for issue in expand_issues):
        # Expansion failed: placeholders remain in `expanded`; validating it
        # would only invent follow-on errors (fault isolation).
        return None, expand_issues, source
    model, validation_issues = _validate_model(model_cls, expanded, display)
    if validation_issues:
        return None, [*expand_issues, *validation_issues], source
    assert model is not None  # validate_model's own pairing contract
    return model, expand_issues, source


# ---------------------------------------------------------------------------
# Roster orchestration
# ---------------------------------------------------------------------------


def load_config_files(
    *, repo_root: Path, config_dir: Path, environ: Mapping[str, str] | None = None
) -> LoadedConfig:
    """Read, expand, validate and collect the six roster documents.

    Never raises for config problems — inspect :attr:`LoadedConfig.issues`
    and ``has_errors``. ``environ`` defaults to ``os.environ``; tests pass an
    explicit mapping for hermeticity. File stems map to attribute names
    (``settings.yaml`` -> ``settings``). Local overlays are a separate pass —
    see :func:`load_local_overlays`.
    """
    env: Mapping[str, str] = os.environ if environ is None else environ
    issues: list[ConfigIssue] = []
    sources: list[tuple[str, str]] = []
    source_of: dict[str, str] = {}
    models: dict[str, Any] = {}

    for filename, model_cls, required in CONFIG_FILES:
        stem = filename.removesuffix(".yaml")
        path = config_dir / filename
        display = display_name(path, config_dir, repo_root)
        source_of[filename] = display
        model, file_issues, source = _ingest_document(
            path=path, display=display, model_cls=model_cls, env=env
        )
        if source is not None:
            sources.append(source)
        if isinstance(model, _MissingFile):
            if required:
                issues.append(
                    ConfigIssue.error(display, "", "required config file is missing")
                )
                models[stem] = None
            else:
                issues.append(
                    ConfigIssue.warning(
                        display,
                        "",
                        "file absent; schema defaults apply (ship the file even "
                        "when empty — ADR-015)",
                    )
                )
                models[stem] = model_cls.model_validate({})
            continue
        issues.extend(file_issues)
        models[stem] = model

    loaded = LoadedConfig(**models, issues=tuple(issues), sources=tuple(sources))
    containment = _path_containment_issues(
        loaded, config_dir=config_dir, repo_root=repo_root, source_of=source_of
    )
    if containment:
        loaded = LoadedConfig(
            **models, issues=(*issues, *containment), sources=tuple(sources)
        )
    return loaded


# ---------------------------------------------------------------------------
# *.local.yaml deployment overrides (T1.4; gated)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OverlayBundle:
    """What ``config/*.local.yaml`` contributed to one load.

    ``overrides`` carries a stem only when the gate is enabled AND the file
    ingested cleanly; every other case falls through to the base layer with
    the reason in ``issues`` — error for broken or unknown-target files,
    warning when the gate is closed. "Ignored" never means "silent".
    ``sources`` follows the successfully overridden stems exactly.
    """

    overrides: Mapping[str, Any]
    issues: tuple[ConfigIssue, ...]
    sources: tuple[tuple[str, str], ...]


def load_local_overlays(
    *,
    repo_root: Path,
    config_dir: Path,
    environ: Mapping[str, str] | None = None,
    allow_local: bool = False,
) -> OverlayBundle:
    """Discover and ingest ``<stem>.local.yaml`` for the six roster stems.

    A local file REPLACES its target document wholesale — deep YAML merging
    is exactly the silent-override swamp ADR-015 exists to avoid. Union
    resources (permission rules, hooks, MCP servers) therefore shed the
    replaced document's entries by design; the replacement is visible in
    ``sources`` and, at deployment, in file review.
    """
    env: Mapping[str, str] = os.environ if environ is None else environ
    issues: list[ConfigIssue] = []
    overrides: dict[str, Any] = {}
    sources: list[tuple[str, str]] = []
    # Roster-stem order first, filename as tiebreaker: sources/issue lists
    # stay same-shaped with the base pass (determinism is contract).
    stem_order = {stem: i for i, stem in enumerate(_ROSTER_BY_STEM)}
    try:
        candidates = sorted(
            config_dir.glob(f"*{LOCAL_SUFFIX}"),
            key=lambda p: (
                stem_order.get(p.name[: -len(LOCAL_SUFFIX)], len(stem_order)),
                p.name,
            ),
        )
    except OSError:
        candidates = []  # unreadable config dir: the base pass reports it loudly
    for path in candidates:
        stem = path.name[: -len(LOCAL_SUFFIX)]
        display = display_name(path, config_dir, repo_root)
        model_cls = _ROSTER_BY_STEM.get(stem)
        if model_cls is None:
            issues.append(
                ConfigIssue.error(
                    display,
                    "",
                    f"'{path.name}' targets no known config file (valid stems: "
                    + ", ".join(sorted(_ROSTER_BY_STEM))
                    + ") — local files replace a document, never add one",
                )
            )
            continue
        if not allow_local:
            issues.append(
                ConfigIssue.warning(
                    display,
                    "",
                    "local override present but the CONFIG_ALLOW_LOCAL gate is "
                    "disabled — ignored (plan §5)",
                )
            )
            continue
        model, file_issues, source = _ingest_document(
            path=path, display=display, model_cls=model_cls, env=env
        )
        if file_issues or model is None or isinstance(model, _MissingFile):
            issues.extend(file_issues)
            continue  # broken overlay: base layer stays; the errors refuse load
        overrides[stem] = model
        assert source is not None
        sources.append(source)
    return OverlayBundle(
        overrides=overrides, issues=tuple(issues), sources=tuple(sources)
    )
