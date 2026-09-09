"""Generate the Recurse SDK reference documentation.

The API reference is rendered from the installed ``recurse`` package and its
docstrings; the manifest reference is rendered from the same machine-readable
specification the SDK validates with. Run with ``--check`` to fail when the
committed references are stale.
"""

import argparse
import inspect
import json
import sys
from importlib.resources import files
from pathlib import Path
from typing import Any, cast

import yaml

import recurse

DOCS = Path(__file__).parents[1] / "docs" / "reference"
MANIFEST_SCHEMA = json.loads(
    files(recurse).joinpath("agent.schema.json").read_text(encoding="utf-8")
)


def render_api_reference() -> str:
    """Render the public API reference from the installed package.

    Returns:
        The Markdown API reference.
    """
    lines = [
        "# Recurse SDK API reference",
        "",
        "Generated from the installed `recurse` package. Do not edit by hand.",
        "",
        "## `recurse`",
        "",
        str(inspect.getdoc(recurse)),
        "",
    ]
    for name in recurse.__all__:
        member = getattr(recurse, name)
        docstring = inspect.getdoc(member)
        if not docstring:
            raise SystemExit(f"public member recurse.{name} has no docstring")
        if inspect.isclass(member):
            heading = f"## `recurse.{name}`"
            if not issubclass(member, BaseException):
                signature = f"class {name}{inspect.signature(member)}"
                heading += f"\n\n```python\n{signature}\n```"
        else:
            heading = f"## `recurse.{name}`\n\n```python\n{name}{inspect.signature(member)}\n```"
        lines.extend([heading, "", docstring, ""])
    return "\n".join(lines)


def _resolve_schema(field: dict[str, Any]) -> dict[str, Any]:
    """Resolve one local manifest-schema reference while retaining site metadata."""
    reference = field.get("$ref")
    if not isinstance(reference, str):
        return field
    prefix = "#/$defs/"
    target = MANIFEST_SCHEMA["$defs"][reference.removeprefix(prefix)]
    return {**target, **field}


def _schema_kind(field: dict[str, Any]) -> str:
    """Return a concise author-facing kind for one schema field."""
    if "const" in field:
        return "literal"
    if "enum" in field:
        return "choice"
    if field.get("format") == "relative-path":
        return "path"
    kind = cast("str", field["type"])
    return {"boolean": "bool", "object": "mapping"}.get(kind, kind)


def _render_field(name: str, field: dict[str, Any], required: bool, depth: int) -> list[str]:
    """Render one manifest field entry.

    Args:
        name: Dotted field path.
        field: The field's JSON Schema entry.
        required: Whether the containing object requires the property.
        depth: Heading depth for the entry.

    Returns:
        Markdown lines for the field and its subfields.
    """
    resolved = _resolve_schema(field)
    lines = [f"{'#' * depth} `{name}`", ""]
    kind = _schema_kind(resolved)
    lines.append(f"*{kind}, {'required' if required else 'optional'}*")
    lines.extend(["", field.get("description", resolved["description"]), ""])
    if "const" in resolved:
        lines.extend([f"Allowed value: `{resolved['const']}`", ""])
    if "enum" in resolved:
        rendered = ", ".join(f"`{choice}`" for choice in resolved["enum"])
        lines.extend([f"Allowed values: {rendered}", ""])
    if "default" in resolved:
        lines.extend([f"Default: `{resolved['default']}`", ""])
    if "examples" in resolved:
        leaf = name.rsplit(".", maxsplit=1)[-1]
        rendered = yaml.safe_dump({leaf: resolved["examples"][0]}, sort_keys=False).rstrip()
        lines.extend(["Example:", "", "```yaml", rendered, "```", ""])
    required_children = set(resolved.get("required", []))
    for child_name, child in resolved.get("properties", {}).items():
        lines.extend(
            _render_field(f"{name}.{child_name}", child, child_name in required_children, depth + 1)
        )
    return lines


def render_manifest_reference() -> str:
    """Render the manifest reference from the packaged JSON Schema.

    Returns:
        The Markdown manifest reference.
    """
    lines = [
        "# `agent.yaml` manifest reference",
        "",
        "Generated from the manifest specification the SDK validates with. Do not edit by hand.",
        "",
        MANIFEST_SCHEMA["description"],
        "",
    ]
    required = set(MANIFEST_SCHEMA["required"])
    for name, field in MANIFEST_SCHEMA["properties"].items():
        lines.extend(_render_field(name, field, name in required, 2))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Generate or verify the reference documentation.

    Args:
        argv: Command-line arguments; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit status: 0 on success, 1 when ``--check`` finds drift.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify instead of write")
    arguments = parser.parse_args(argv)
    rendered = {
        DOCS / "api.md": render_api_reference(),
        DOCS / "manifest.md": render_manifest_reference(),
    }
    if arguments.check:
        stale = [
            str(path)
            for path, content in rendered.items()
            if not path.is_file() or path.read_text() != content
        ]
        if stale:
            print(f"stale generated documentation: {', '.join(stale)}", file=sys.stderr)
            return 1
        return 0
    DOCS.mkdir(parents=True, exist_ok=True)
    for path, content in rendered.items():
        path.write_text(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
