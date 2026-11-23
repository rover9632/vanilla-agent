"""`ConfigIssue`: the config layer's shared finding type (docs/plan.md §4).

Produced by the loader (T1.2), collected by snapshot ``validate()`` (T1.3),
reported by config-check (T1.6). Lives in its own module so every layer can
import it without cycles; ``app.config.snapshot`` will re-export it to match
the §4 contract's nominal home.

Field spellings follow §4 verbatim:

- ``source`` — where the finding points, e.g. ``config/permissions.yaml`` or
  ``config/permissions.yaml:41`` when a line is known (YAML syntax errors);
  ``<unknown>`` only for findings not tied to a file.
- ``path`` — dotted/bracketed location inside the document, e.g.
  ``rules[3].effect``; empty for file-level findings.

Messages must never echo config *values* that could be secrets (§8.6): pydantic
structured-error ``msg`` fields do not include inputs (except for custom
validators, which schema.py writes value-free on purpose), and the loader's
own messages name env vars but never their contents. A regression test pins
the leak-free property end to end.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class ConfigIssue(BaseModel):
    """One finding from load or validation. Errors refuse the snapshot;
    warnings ride along and are surfaced by dump/config-check."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    level: Literal["error", "warning"]
    source: str
    path: str
    message: str

    @classmethod
    def error(cls, source: str, path: str, message: str) -> ConfigIssue:
        return cls(level="error", source=source, path=path, message=message)

    @classmethod
    def warning(cls, source: str, path: str, message: str) -> ConfigIssue:
        return cls(level="warning", source=source, path=path, message=message)


def loc_to_path(loc: tuple[Any, ...]) -> str:
    """Render a pydantic ``ValidationError`` loc as the §4 path shape.

    ``("rules", 1, "effect") -> "rules[1].effect"``. Pydantic renders a
    failing dict *key* as ``("presets", "plan", "[key]")`` — the redundant
    ``[key]`` marker is dropped so the path names the offending key. Some
    versions type-prefix key parts (``"str:plan"``); that prefix is stripped
    too, keeping paths stable and greppable.
    """
    out = ""
    for part in loc:
        piece = str(part)
        if piece == "[key]":
            continue
        for marker in ("str:", "int:", "bytes:", "json:", "dataclass:"):
            if piece.startswith(marker):
                piece = piece[len(marker) :]
                break
        if piece.isdigit():
            out += f"[{piece}]"
        else:
            out += f".{piece}" if out else piece
    return out
