"""Docstring completeness check for the whole SDK Python tree.

Every module, class, function, and method under the SDK source, tests,
documentation tooling, and examples must carry a docstring, including nested
helpers. The check fails with an exact location for every omission.
"""

import ast
import re
import sys
from pathlib import Path

_CHECKED_DIRECTORIES = ("src", "tests", "tools", "examples")
_SECTION = re.compile(r"^\s*(Args|Returns|Raises|Yields|Attributes):\s*$")
_ENTRY = re.compile(r"^\s*([*]{0,2}[A-Za-z_][A-Za-z0-9_]*)(?:\s+\([^)]*\))?:\s*\S")


def _missing_in_module(path: Path) -> list[str]:
    """Return the undocumented definitions of one module.

    Args:
        path: The module file to inspect.

    Returns:
        One ``file:line: name`` entry per missing docstring.
    """
    module = ast.parse(path.read_bytes())
    missing = []
    if not ast.get_docstring(module):
        missing.append(f"{path}:1: module")
    for node in ast.walk(module):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) and not (
            ast.get_docstring(node)
        ):
            missing.append(f"{path}:{node.lineno}: {node.name}")
    return missing


def _docstring_sections(docstring: str) -> dict[str, list[str]]:
    """Split the contract sections of one Google-style docstring.

    Args:
        docstring: Parsed Python docstring.

    Returns:
        Section names mapped to their content lines.
    """
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in docstring.splitlines():
        heading = _SECTION.fullmatch(line)
        if heading:
            current = heading.group(1)
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return sections


def _documented_names(lines: list[str]) -> set[str]:
    """Return the parameter or exception names documented by section lines.

    Args:
        lines: Lines from an ``Args`` or ``Raises`` section.

    Returns:
        Names that have a non-empty description.
    """
    return {
        match.group(1).lstrip("*") for line in lines if (match := _ENTRY.match(line)) is not None
    }


def _function_body_nodes(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[ast.AST]:
    """Return nodes owned by a function without descending into nested definitions.

    Args:
        function: Exported function definition.

    Returns:
        Nodes that contribute to the function's own behavior.
    """
    nodes: list[ast.AST] = []
    pending: list[ast.AST] = list(function.body)
    while pending:
        node = pending.pop()
        nodes.append(node)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            continue
        pending.extend(ast.iter_child_nodes(node))
    return nodes


def _public_function_errors(
    path: Path,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    public_errors: set[str],
) -> list[str]:
    """Return structural docstring errors for one exported function.

    Args:
        path: Module path used in diagnostics.
        function: Exported function definition.
        public_errors: Exported SDK error types.

    Returns:
        Exact ``file:line: symbol: problem`` diagnostics.
    """
    docstring = ast.get_docstring(function)
    if not docstring:
        return []
    sections = _docstring_sections(docstring)
    documented_parameters = _documented_names(sections.get("Args", []))
    arguments = function.args
    parameters = [
        parameter.arg
        for parameter in [
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
        ]
    ]
    for variadic in (arguments.vararg, arguments.kwarg):
        if variadic is not None:
            parameters.append(variadic.arg)
    prefix = f"{path}:{function.lineno}: {function.name}:"
    errors = []
    for parameter in parameters:
        if parameter not in documented_parameters:
            errors.append(f"{prefix} Args must document parameter {parameter}")
    for parameter in sorted(documented_parameters.difference(parameters)):
        errors.append(f"{prefix} Args documents unknown parameter {parameter}")
    nodes = _function_body_nodes(function)
    yields = any(isinstance(node, ast.Yield | ast.YieldFrom) for node in nodes)
    returns = any(isinstance(node, ast.Return) and node.value is not None for node in nodes)
    if yields and not "".join(sections.get("Yields", [])).strip():
        errors.append(f"{prefix} Yields must describe yielded values")
    elif returns and not "".join(sections.get("Returns", [])).strip():
        errors.append(f"{prefix} Returns must describe the return value")
    raised = set()
    for node in nodes:
        if not isinstance(node, ast.Raise) or node.exc is None:
            continue
        raised_expression = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
        if isinstance(raised_expression, ast.Name) and raised_expression.id in public_errors:
            raised.add(raised_expression.id)
    documented_errors = _documented_names(sections.get("Raises", []))
    for exception_name in sorted(raised.difference(documented_errors)):
        errors.append(f"{prefix} Raises must document {exception_name}")
    return errors


def _public_api_errors(path: Path) -> list[str]:
    """Return structural docstring errors in one module's exported functions.

    Args:
        path: Python module that declares a literal ``__all__`` list.

    Returns:
        Exact ``file:line: symbol: problem`` diagnostics.
    """
    if not path.is_file():
        return []
    module = ast.parse(path.read_bytes())
    definitions = {
        node.name: node
        for node in module.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    }
    exports: list[str] = []
    for node in module.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
        ):
            exports = ast.literal_eval(node.value)
            break
    public_errors = {
        name
        for name in exports
        if isinstance(definitions.get(name), ast.ClassDef) and name.endswith("Error")
    }
    errors = [
        error
        for name in exports
        if isinstance(function := definitions.get(name), ast.FunctionDef | ast.AsyncFunctionDef)
        for error in _public_function_errors(path, function, public_errors)
    ]
    for name in exports:
        definition = definitions.get(name)
        if not isinstance(definition, ast.ClassDef):
            continue
        fields = {
            field.target.id
            for field in definition.body
            if isinstance(field, ast.AnnAssign)
            and isinstance(field.target, ast.Name)
            and not field.target.id.startswith("_")
        }
        sections = _docstring_sections(ast.get_docstring(definition) or "")
        documented = _documented_names(sections.get("Attributes", []))
        prefix = f"{path}:{definition.lineno}: {name}: Attributes"
        errors.extend(
            f"{prefix} must document field {field}" for field in sorted(fields - documented)
        )
        errors.extend(
            f"{prefix} documents unknown field {field}" for field in sorted(documented - fields)
        )
    return errors


def main(root: Path | None = None) -> int:
    """Check every SDK-owned Python file for complete docstrings.

    Args:
        root: Tree to check; defaults to the SDK repository root.

    Returns:
        Process exit status: 0 when complete, 1 with one line per omission.
    """
    root = root if root is not None else Path(__file__).parents[1]
    missing = [
        entry
        for directory in _CHECKED_DIRECTORIES
        for path in sorted((root / directory).rglob("*.py"))
        if ".venv" not in path.relative_to(root).parts
        for entry in _missing_in_module(path)
    ]
    public_api_errors = _public_api_errors(root / "src" / "recurse.py")
    for entry in [*missing, *public_api_errors]:
        print(entry, file=sys.stderr)
    print(f"{len(missing)} definitions are missing docstrings")
    print(f"{len(public_api_errors)} public API docstring errors")
    return 1 if missing or public_api_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
