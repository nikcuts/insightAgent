"""Python code analysis tools."""

from __future__ import annotations

import ast
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..runtime.tool_context import ToolContext
from .base import read_workspace_text


@dataclass(frozen=True)
class ParseAstTool:
    context: ToolContext
    name: str = "parse_ast"
    description: str = "Parse a Python file and summarize imports, classes, functions, and globals."
    input_schema: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_schema", _path_schema())

    def run(self, arguments: dict[str, Any]) -> str:
        loaded = _require_python_source(self.context, str(arguments["path"]))
        tree = loaded.tree
        assert tree is not None
        payload = {
            "file": loaded.relative_path,
            "imports": _extract_imports(tree),
            "classes": _extract_classes(tree),
            "functions": _extract_top_level_functions(tree),
            "global_variables": _extract_global_variables(tree),
        }
        return _json(payload)


@dataclass(frozen=True)
class GetFunctionSignatureTool:
    context: ToolContext
    name: str = "get_function_signature"
    description: str = "Return signature metadata for a Python function or class method."
    input_schema: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "input_schema",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Python file path."},
                    "function_name": {"type": "string", "description": "Function or method name."},
                    "class_name": {"type": "string", "description": "Optional class name to disambiguate methods."},
                },
                "required": ["path", "function_name"],
                "additionalProperties": False,
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        function_name = str(arguments["function_name"])
        class_name = arguments.get("class_name")
        class_name = str(class_name) if class_name else None
        loaded = _require_python_source(self.context, str(arguments["path"]))
        tree = loaded.tree
        assert tree is not None
        matches = [
            _function_payload(node, kind, owner)
            for node, kind, owner in _iter_functions(tree)
            if node.name == function_name and (class_name is None or owner == class_name)
        ]
        if len(matches) == 1:
            return _json(matches[0])
        if matches:
            return _json(
                {
                    "name": function_name,
                    "matches": matches,
                    "message": "multiple matches; call again with class_name to select the intended method",
                }
            )
        return f"function not found: {function_name}"


@dataclass(frozen=True)
class FindDependenciesTool:
    context: ToolContext
    name: str = "find_dependencies"
    description: str = "Classify Python imports as stdlib, third-party, local, or relative."
    input_schema: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_schema", _path_schema())

    def run(self, arguments: dict[str, Any]) -> str:
        loaded = _require_python_source(self.context, str(arguments["path"]))
        tree = loaded.tree
        assert tree is not None
        local_modules = _local_module_names(self.context.workspace)
        result = {"stdlib": [], "third_party": [], "local": [], "relative": []}
        for item in _extract_imports(tree):
            module = item["module"]
            if not module:
                continue
            root = module.split(".", 1)[0]
            if item.get("level", 0) > 0:
                _append_unique(result["relative"], root)
            elif root in local_modules:
                _append_unique(result["local"], root)
            elif _is_stdlib(root):
                _append_unique(result["stdlib"], root)
            else:
                _append_unique(result["third_party"], root)
        return _json(result)


@dataclass(frozen=True)
class GetCodeMetricsTool:
    context: ToolContext
    name: str = "get_code_metrics"
    description: str = "Return basic Python code metrics for one file."
    input_schema: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_schema", _path_schema())

    def run(self, arguments: dict[str, Any]) -> str:
        loaded = _require_python_source(self.context, str(arguments["path"]))
        tree = loaded.tree
        assert tree is not None
        lines = loaded.source.splitlines()
        payload = {
            "file": loaded.relative_path,
            "total_lines": len(lines),
            "blank_lines": sum(1 for line in lines if not line.strip()),
            "comment_lines": sum(1 for line in lines if line.strip().startswith("#")),
            "imports": len(_extract_imports(tree)),
            "classes": sum(1 for node in ast.walk(tree) if isinstance(node, ast.ClassDef)),
            "functions": sum(1 for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))),
        }
        return _json(payload)


@dataclass(frozen=True)
class LoadedPythonSource:
    path: Path | None
    relative_path: str
    source: str
    tree: ast.Module | None
    error: str | None = None


def _path_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Python file path."}},
        "required": ["path"],
        "additionalProperties": False,
    }


def _load_python_source(context: ToolContext, raw_path: str) -> LoadedPythonSource:
    path = context.resolve_workspace_path(raw_path)
    rel = path.relative_to(context.workspace).as_posix()
    if not path.exists():
        return LoadedPythonSource(path, rel, "", None, f"file does not exist: {rel}")
    if not path.is_file():
        return LoadedPythonSource(path, rel, "", None, f"path is not a file: {rel}")
    if path.suffix != ".py":
        return LoadedPythonSource(path, rel, "", None, f"not a Python file: {rel}")
    try:
        source = read_workspace_text(context, path)
    except Exception as error:  # noqa: BLE001 - tool output should explain local read failures
        return LoadedPythonSource(path, rel, "", None, str(error))
    try:
        tree = ast.parse(source, filename=rel)
    except SyntaxError as error:
        return LoadedPythonSource(path, rel, source, None, f"{rel}: SyntaxError: {error}")
    return LoadedPythonSource(path, rel, source, tree)


def _require_python_source(context: ToolContext, raw_path: str) -> LoadedPythonSource:
    loaded = _load_python_source(context, raw_path)
    if loaded.error is None:
        return loaded
    if loaded.error.startswith(("file does not exist:", "path is not a file:")):
        raise FileNotFoundError(loaded.error)
    if "SyntaxError:" in loaded.error:
        raise SyntaxError(loaded.error)
    raise ValueError(loaded.error)


def _extract_imports(tree: ast.AST) -> list[dict[str, Any]]:
    imports: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(
                    {
                        "type": "import",
                        "module": alias.name,
                        "name": alias.name,
                        "alias": alias.asname,
                        "line": node.lineno,
                        "level": 0,
                    }
                )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                imports.append(
                    {
                        "type": "from_import",
                        "module": module or alias.name,
                        "name": alias.name,
                        "alias": alias.asname,
                        "line": node.lineno,
                        "level": node.level,
                    }
                )
    return sorted(imports, key=lambda item: (item["line"], item["module"], item["name"]))


def _extract_classes(tree: ast.AST) -> list[dict[str, Any]]:
    classes: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            methods = [
                {
                    "name": item.name,
                    "line": item.lineno,
                    "args": [arg.arg for arg in item.args.args],
                    "is_async": isinstance(item, ast.AsyncFunctionDef),
                }
                for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            classes.append(
                {
                    "name": node.name,
                    "line": node.lineno,
                    "bases": [_node_name(base) for base in node.bases],
                    "methods": methods,
                    "decorators": [_node_name(decorator) for decorator in node.decorator_list],
                    "docstring": ast.get_docstring(node),
                }
            )
    return sorted(classes, key=lambda item: item["line"])


def _extract_top_level_functions(tree: ast.Module) -> list[dict[str, Any]]:
    functions: list[dict[str, Any]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(
                {
                    "name": node.name,
                    "line": node.lineno,
                    "signature": _signature(node),
                    "is_async": isinstance(node, ast.AsyncFunctionDef),
                    "docstring": ast.get_docstring(node),
                    "decorators": [_node_name(decorator) for decorator in node.decorator_list],
                }
            )
    return functions


def _extract_global_variables(tree: ast.Module) -> list[dict[str, Any]]:
    variables: list[dict[str, Any]] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    variables.append({"name": target.id, "line": node.lineno, "type": type(node.value).__name__})
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            variables.append({"name": node.target.id, "line": node.lineno, "type": type(node.value).__name__})
    return variables


def _iter_functions(tree: ast.Module) -> list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, str, str | None]]:
    items: list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, str, str | None]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            items.append((node, "function", None))
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    items.append((child, "method", node.name))
    return items


def _function_payload(node: ast.FunctionDef | ast.AsyncFunctionDef, kind: str, class_name: str | None) -> dict[str, Any]:
    return {
        "name": node.name,
        "signature": _signature(node),
        "line": node.lineno,
        "docstring": ast.get_docstring(node),
        "is_async": isinstance(node, ast.AsyncFunctionDef),
        "kind": kind,
        "class_name": class_name,
    }


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    return_type = f" -> {_node_name(node.returns)}" if node.returns else ""
    return f"{prefix} {node.name}({_arguments_signature(node.args)}){return_type}"


def _arguments_signature(args: ast.arguments) -> str:
    parts = []
    positional = list(args.posonlyargs) + list(args.args)
    defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    for arg, default in zip(positional, defaults):
        parts.append(_argument_signature(arg, default))
    if args.vararg:
        parts.append("*" + _argument_signature(args.vararg, None))
    elif args.kwonlyargs:
        parts.append("*")
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        parts.append(_argument_signature(arg, default))
    if args.kwarg:
        parts.append("**" + _argument_signature(args.kwarg, None))
    return ", ".join(parts)


def _argument_signature(arg: ast.arg, default: ast.expr | None) -> str:
    text = arg.arg
    if arg.annotation:
        text += f": {_node_name(arg.annotation)}"
    if default:
        text += f" = {_node_name(default)}"
    return text


def _node_name(node: ast.AST | None) -> str:
    if node is None:
        return ""
    return ast.unparse(node) if hasattr(ast, "unparse") else type(node).__name__


def _local_module_names(workspace: Path) -> set[str]:
    modules = {path.stem for path in workspace.glob("*.py")}
    modules.update(path.name for path in workspace.iterdir() if path.is_dir() and (path / "__init__.py").is_file())
    return modules


def _is_stdlib(module: str) -> bool:
    stdlib = getattr(sys, "stdlib_module_names", set())
    return module in stdlib


def _append_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)
        items.sort()


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)
