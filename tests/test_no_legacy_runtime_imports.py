"""防止 LangGraph 运行时重新依赖已删除的旧智能体模块。"""

from __future__ import annotations

import ast
from pathlib import Path


LEGACY_MODULES = frozenset(
    {
        "insightagent.agent.context",
        "insightagent.agent.core",
        "insightagent.agent.memory",
        "insightagent.agent.repository_snapshot",
        "insightagent.agent.session",
        "insightagent.agent.task_contracts",
        "insightagent.agent.task_state",
        "insightagent.api.messages",
        "insightagent.api.providers",
        "insightagent.api.resilience",
        "insightagent.telemetry.trace",
        "insightagent.telemetry.usage",
        "insightagent.tools.registry",
    }
)

_LEGACY_PACKAGE_EXPORTS = {
    "insightagent.agent": {"context", "core", "memory", "repository_snapshot", "session", "task_contracts", "task_state"},
    "insightagent.api": {"messages", "providers", "resilience"},
    "insightagent.telemetry": {"trace", "usage"},
    "insightagent.tools": {"ToolRegistry", "default_tools", "registry"},
}


def test_production_runtime_has_no_legacy_runtime_imports_or_files() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "insightagent"
    hits: list[str] = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        current_package = _package_for_path(root, path)
        for hit in _legacy_static_import_hits(tree, current_package=current_package):
            hits.append(f"{path}: {hit}")
        for module in _legacy_dynamic_imports(tree):
            hits.append(f"{path}: dynamic import {module}")

    remaining_files = [
        str(root / module.removeprefix("insightagent.").replace(".", "/")).replace("/", "/") + ".py"
        for module in LEGACY_MODULES
        if (root / module.removeprefix("insightagent.").replace(".", "/")).with_suffix(".py").exists()
    ]
    assert hits == []
    assert remaining_files == []


def test_dynamic_legacy_import_guard_detects_standard_aliases() -> None:
    tree = ast.parse(
        "\n".join(
            [
                "import importlib as il",
                'il.import_module("insightagent.agent.core")',
                'il.import_module(name="insightagent.api.providers")',
                "from importlib import import_module as loader",
                'loader("insightagent.api.providers")',
                'builtin = __import__("insightagent.telemetry.trace")',
                '__import__(name="insightagent.api.messages")',
                "assigned_loader = il.import_module",
                'assigned_loader("insightagent.agent.memory")',
                "destructured_loader, unused = il.import_module, None",
                'destructured_loader("insightagent.agent.session")',
                'getattr_loader = getattr(il, "import_module")',
                'getattr_loader("insightagent.agent.session")',
                "import builtins as bi",
                "builtin_loader = bi.__import__",
                'builtin_loader("insightagent.agent.task_state")',
                "holder = object()",
                "holder.loader = il.import_module",
                'holder.loader("insightagent.agent.context")',
                "holder.builtin_loader = bi.__import__",
                'holder.builtin_loader("insightagent.api.resilience")',
                "class Holder:",
                "    module = il",
                "cross_layer_loader = Holder.module.import_module",
                'cross_layer_loader("insightagent.agent.core")',
                "class Loader:",
                "    class_loader = il.import_module",
                'Loader.class_loader("insightagent.agent.task_contracts")',
                'getattr_builtin_loader = getattr(bi, "__import__")',
                'getattr_builtin_loader("insightagent.api.messages")',
                'getattr(il, "import_module")("insightagent.agent.core")',
                'getattr(bi, "__import__")("insightagent.api.resilience")',
                "lookup = getattr",
                'lookup_loader = lookup(il, "import_module")',
                'lookup_loader("insightagent.telemetry.usage")',
                'bi.getattr(il, "import_module")("insightagent.tools.registry")',
                "from builtins import getattr as imported_lookup",
                'imported_lookup_loader = imported_lookup(il, "import_module")',
                'imported_lookup_loader("insightagent.agent.context")',
                "importlib_module_alias = il",
                'importlib_module_alias.import_module("insightagent.agent.repository_snapshot")',
                'getattr(importlib_module_alias, "import_module")("insightagent.agent.task_contracts")',
                "builtins_module_alias = bi",
                'builtins_module_alias.__import__("insightagent.telemetry.trace")',
                'getattr(builtins_module_alias, "__import__")("insightagent.telemetry.usage")',
            ]
        )
    )

    assert _legacy_dynamic_imports(tree) == [
        "insightagent.agent.core",
        "insightagent.api.providers",
        "insightagent.api.providers",
        "insightagent.telemetry.trace",
        "insightagent.api.messages",
        "insightagent.agent.memory",
        "insightagent.agent.session",
        "insightagent.agent.session",
        "insightagent.agent.task_state",
        "insightagent.agent.context",
        "insightagent.api.resilience",
        "insightagent.agent.core",
        "insightagent.agent.task_contracts",
        "insightagent.api.messages",
        "insightagent.agent.core",
        "insightagent.api.resilience",
        "insightagent.telemetry.usage",
        "insightagent.tools.registry",
        "insightagent.agent.context",
        "insightagent.agent.repository_snapshot",
        "insightagent.agent.task_contracts",
        "insightagent.telemetry.trace",
        "insightagent.telemetry.usage",
    ]


def test_legacy_import_guard_resolves_relative_imports() -> None:
    tree = ast.parse(
        "\n".join(
            [
                "from .core import CodeAgent",
                "from . import core",
                "from ..api import providers",
            ]
        )
    )

    assert _legacy_static_import_hits(tree, current_package="insightagent.agent") == [
        "from insightagent.agent.core",
        "from insightagent.agent import core",
        "from insightagent.api import providers",
    ]


def _is_legacy_module(module: str) -> bool:
    return any(module == legacy or module.startswith(f"{legacy}.") for legacy in LEGACY_MODULES)


def _legacy_static_import_hits(tree: ast.AST, *, current_package: str) -> list[str]:
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = _resolve_import_from_module(node, current_package=current_package)
            if _is_legacy_module(module):
                hits.append(f"from {module}")
            exports = _LEGACY_PACKAGE_EXPORTS.get(module, set())
            for alias in node.names:
                if alias.name in exports:
                    hits.append(f"from {module} import {alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if _is_legacy_module(alias.name):
                    hits.append(f"import {alias.name}")
    return hits


def _package_for_path(root: Path, path: Path) -> str:
    relative_parent = path.relative_to(root).parent
    return ".".join(("insightagent", *relative_parent.parts))


def _resolve_import_from_module(node: ast.ImportFrom, *, current_package: str) -> str:
    if node.level == 0:
        return node.module or ""
    package_parts = current_package.split(".")
    parent_count = node.level - 1
    if parent_count >= len(package_parts):
        return node.module or ""
    base = package_parts[: len(package_parts) - parent_count]
    if node.module:
        base.extend(node.module.split("."))
    return ".".join(base)


def _legacy_dynamic_imports(tree: ast.AST) -> list[str]:
    importlib_aliases = {"importlib"}
    import_module_aliases = {"import_module"}
    builtin_aliases = {"builtins"}
    builtin_import_aliases = {"__import__"}
    getattr_aliases = {"getattr"}
    parents = _parent_nodes(tree)
    nodes = list(ast.walk(tree))
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "importlib":
                    importlib_aliases.add(alias.asname or "importlib")
                if alias.name == "builtins":
                    builtin_aliases.add(alias.asname or "builtins")
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if node.module == "importlib" and alias.name == "import_module":
                    import_module_aliases.add(alias.asname or alias.name)
                if node.module == "builtins" and alias.name == "__import__":
                    builtin_import_aliases.add(alias.asname or alias.name)
                if node.module == "builtins" and alias.name == "getattr":
                    getattr_aliases.add(alias.asname or alias.name)
    for _ in range(len(nodes) + 1):
        alias_sizes = (
            len(importlib_aliases),
            len(import_module_aliases),
            len(builtin_aliases),
            len(builtin_import_aliases),
            len(getattr_aliases),
        )
        for node in nodes:
            if isinstance(node, ast.Assign):
                class_scope = _direct_class_scope(node, parents)
                for target in node.targets:
                    _register_assignment_aliases(
                        target,
                        node.value,
                        importlib_aliases,
                        import_module_aliases,
                        builtin_aliases,
                        builtin_import_aliases,
                        getattr_aliases,
                        class_scope=class_scope,
                    )
            elif isinstance(node, ast.AnnAssign):
                _register_assignment_aliases(
                    node.target,
                    node.value,
                    importlib_aliases,
                    import_module_aliases,
                    builtin_aliases,
                    builtin_import_aliases,
                    getattr_aliases,
                    class_scope=_direct_class_scope(node, parents),
                )
        if alias_sizes == (
            len(importlib_aliases),
            len(import_module_aliases),
            len(builtin_aliases),
            len(builtin_import_aliases),
            len(getattr_aliases),
        ):
            break
    else:
        raise AssertionError("dynamic import alias propagation did not converge")

    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        module = _static_import_module_argument(node)
        if module is not None and _is_legacy_module(module) and _is_dynamic_import_call(
            node,
            importlib_aliases,
            import_module_aliases,
            builtin_aliases,
            builtin_import_aliases,
            getattr_aliases,
        ):
            hits.append(module)
    return hits


def _static_import_module_argument(node: ast.Call) -> str | None:
    if node.args:
        first_argument = node.args[0]
        if isinstance(first_argument, ast.Constant) and isinstance(first_argument.value, str):
            return first_argument.value
    for keyword in node.keywords:
        if (
            keyword.arg == "name"
            and isinstance(keyword.value, ast.Constant)
            and isinstance(keyword.value.value, str)
        ):
            return keyword.value.value
    return None


def _is_dynamic_import_call(
    node: ast.Call,
    importlib_aliases: set[str],
    import_module_aliases: set[str],
    builtin_aliases: set[str],
    builtin_import_aliases: set[str],
    getattr_aliases: set[str],
) -> bool:
    aliases = _dynamic_import_aliases(
        node.func,
        importlib_aliases,
        import_module_aliases,
        builtin_aliases,
        builtin_import_aliases,
        getattr_aliases,
    )
    return aliases["import_module"] or aliases["builtin_import"]


def _dynamic_import_aliases(
    value: ast.AST | None,
    importlib_aliases: set[str],
    import_module_aliases: set[str],
    builtin_aliases: set[str],
    builtin_import_aliases: set[str],
    getattr_aliases: set[str],
) -> dict[str, bool]:
    if value is None:
        return _no_dynamic_import_aliases()
    alias_key = _alias_key(value)
    if isinstance(value, ast.Attribute):
        base_key = _alias_key(value.value)
        return {
            "importlib_module": False,
            "builtins_module": False,
            "import_module": (
                alias_key in import_module_aliases
                or (value.attr == "import_module" and base_key in importlib_aliases)
            ),
            "builtin_import": (
                alias_key in builtin_import_aliases
                or (value.attr == "__import__" and base_key in builtin_aliases)
            ),
            "getattr": (
                alias_key in getattr_aliases
                or (value.attr == "getattr" and base_key in builtin_aliases)
            ),
        }
    if alias_key is not None:
        return {
            "importlib_module": alias_key in importlib_aliases,
            "builtins_module": alias_key in builtin_aliases,
            "import_module": alias_key in import_module_aliases,
            "builtin_import": alias_key in builtin_import_aliases,
            "getattr": alias_key in getattr_aliases,
        }
    if (
        isinstance(value, ast.Call)
        and _is_getattr_call(value.func, getattr_aliases, builtin_aliases)
        and len(value.args) >= 2
        and (source_key := _alias_key(value.args[0])) is not None
        and isinstance(value.args[1], ast.Constant)
        and isinstance(value.args[1].value, str)
    ):
        return {
            "importlib_module": False,
            "builtins_module": False,
            "import_module": (
                source_key in importlib_aliases
                and value.args[1].value == "import_module"
            ),
            "builtin_import": (
                source_key in builtin_aliases and value.args[1].value == "__import__"
            ),
            "getattr": False,
        }
    return _no_dynamic_import_aliases()


def _is_getattr_call(
    function: ast.AST, getattr_aliases: set[str], builtin_aliases: set[str]
) -> bool:
    function_key = _alias_key(function)
    if function_key in getattr_aliases:
        return True
    return (
        isinstance(function, ast.Attribute)
        and function.attr == "getattr"
        and _alias_key(function.value) in builtin_aliases
    )


def _register_assignment_aliases(
    target: ast.AST,
    value: ast.AST | None,
    importlib_aliases: set[str],
    import_module_aliases: set[str],
    builtin_aliases: set[str],
    builtin_import_aliases: set[str],
    getattr_aliases: set[str],
    *,
    class_scope: str | None = None,
) -> None:
    if (
        isinstance(target, ast.Tuple | ast.List)
        and isinstance(value, ast.Tuple | ast.List)
        and len(target.elts) == len(value.elts)
    ):
        for nested_target, nested_value in zip(target.elts, value.elts):
            _register_assignment_aliases(
                nested_target,
                nested_value,
                importlib_aliases,
                import_module_aliases,
                builtin_aliases,
                builtin_import_aliases,
                getattr_aliases,
                class_scope=class_scope,
            )
        return

    target_keys = _assignment_target_keys(target, class_scope=class_scope)
    if not target_keys:
        return
    aliases = _dynamic_import_aliases(
        value,
        importlib_aliases,
        import_module_aliases,
        builtin_aliases,
        builtin_import_aliases,
        getattr_aliases,
    )
    for target_key in target_keys:
        if aliases["importlib_module"]:
            importlib_aliases.add(target_key)
        if aliases["builtins_module"]:
            builtin_aliases.add(target_key)
        if aliases["import_module"]:
            import_module_aliases.add(target_key)
        if aliases["builtin_import"]:
            builtin_import_aliases.add(target_key)
        if aliases["getattr"]:
            getattr_aliases.add(target_key)


def _parent_nodes(tree: ast.AST) -> dict[int, ast.AST]:
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    return parents


def _direct_class_scope(node: ast.AST, parents: dict[int, ast.AST]) -> str | None:
    parent = parents.get(id(node))
    if not isinstance(parent, ast.ClassDef):
        return None
    names: list[str] = []
    while isinstance(parent, ast.ClassDef):
        names.append(parent.name)
        parent = parents.get(id(parent))
    return ".".join(reversed(names))


def _assignment_target_keys(target: ast.AST, *, class_scope: str | None) -> tuple[str, ...]:
    target_key = _alias_key(target)
    if target_key is None:
        return ()
    if class_scope is not None and isinstance(target, ast.Name):
        return target_key, f"{class_scope}.{target_key}"
    return (target_key,)


def _alias_key(value: ast.AST | None) -> str | None:
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Attribute):
        parent = _alias_key(value.value)
        return f"{parent}.{value.attr}" if parent is not None else None
    return None


def _no_dynamic_import_aliases() -> dict[str, bool]:
    return {
        "importlib_module": False,
        "builtins_module": False,
        "import_module": False,
        "builtin_import": False,
        "getattr": False,
    }
