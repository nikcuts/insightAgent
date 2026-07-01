"""Workspace file tools."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..runtime.tool_context import ToolContext
from .base import read_workspace_text


_FULL_READ_LINE_LIMIT = 400


@dataclass(frozen=True)
class ReadFileTool:
    context: ToolContext
    name: str = "read_file"
    description: str = "Read a UTF-8 text file inside the workspace."
    input_schema: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "input_schema",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the text file to read."},
                    "start_line": {
                        "type": "integer",
                        "description": "Optional 1-based first line to read for large files.",
                    },
                    "max_lines": {
                        "type": "integer",
                        "description": "Optional maximum number of lines to return when start_line is used.",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        path = self.context.resolve_workspace_path(str(arguments["path"]))
        if "start_line" in arguments or "max_lines" in arguments:
            return _read_workspace_line_range(
                self.context,
                path,
                display_path=str(arguments["path"]),
                start_line=int(arguments.get("start_line") or 1),
                max_lines=int(arguments.get("max_lines") or 200),
            )
        return _read_workspace_text_or_navigation_hint(self.context, path, display_path=str(arguments["path"]))


@dataclass(frozen=True)
class WriteFileTool:
    context: ToolContext
    name: str = "write_file"
    description: str = "Overwrite a UTF-8 text file inside the workspace. Prefer edit_file for local changes."
    input_schema: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "input_schema",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to write."},
                    "content": {
                        "type": "string",
                        "description": "Full file content. Existing file content is replaced.",
                    },
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        self.context.check_write_allowed()
        path = self.context.resolve_workspace_path(str(arguments["path"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        content = str(arguments["content"])
        if len(content) > self.context.max_write_chars:
            raise ValueError(f"content too large to write in V3: {len(content)} chars")
        previous = path.read_text(encoding="utf-8") if path.exists() else ""
        _reject_python_syntax_regression(path, previous, content)
        path.write_text(content, encoding="utf-8")
        return f"wrote {len(content)} bytes to {path}"


@dataclass(frozen=True)
class EditFileTool:
    context: ToolContext
    name: str = "edit_file"
    description: str = "Apply a local string replacement to a UTF-8 text file inside the workspace."
    input_schema: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "input_schema",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to edit."},
                    "old": {"type": "string", "description": "Exact text to replace."},
                    "new": {"type": "string", "description": "Replacement text."},
                    "replace_all": {"type": "boolean", "description": "Replace all occurrences. Defaults to false."},
                },
                "required": ["path", "old", "new"],
                "additionalProperties": False,
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        self.context.check_write_allowed()
        path = self.context.resolve_workspace_path(str(arguments["path"]))
        old = str(arguments["old"])
        new = str(arguments["new"])
        if old == new:
            raise ValueError("no-op edit_file rejected: old and new text are identical")
        replace_all = bool(arguments.get("replace_all", False))
        text = path.read_text(encoding="utf-8")
        count = text.count(old)
        if count == 0:
            quote_match = _unique_quote_normalized_span(text, old)
            whitespace_match = None if quote_match is not None else _unique_whitespace_insensitive_span(text, old)
            if quote_match is None and whitespace_match is None:
                raise ValueError("old text not found")
            if quote_match is not None:
                match_kind = "quote_normalized"
                start, end = quote_match
            else:
                match_kind = "whitespace_insensitive"
                start, end = whitespace_match
            end = _extend_python_assert_message_span(text, start, end, old)
            replacement = _apply_context_indentation(text, start, new)
            updated = f"{text[:start]}{replacement}{text[end:]}"
            if len(updated) > self.context.max_write_chars:
                raise ValueError(f"edited content too large to write in V3: {len(updated)} chars")
            _reject_python_syntax_regression(path, text, updated)
            path.write_text(updated, encoding="utf-8")
            return f"edited {path}; replacements=1; match={match_kind}"
        if count > 1 and not replace_all:
            raise ValueError(f"old text appears {count} times; set replace_all=true or make old text unique")
        if replace_all:
            updated = text.replace(old, new)
        else:
            start = text.find(old)
            end = _extend_python_assert_message_span(text, start, start + len(old), old)
            replacement = _apply_context_indentation(text, start, new)
            updated = f"{text[:start]}{replacement}{text[end:]}"
        if len(updated) > self.context.max_write_chars:
            raise ValueError(f"edited content too large to write in V3: {len(updated)} chars")
        _reject_python_syntax_regression(path, text, updated)
        path.write_text(updated, encoding="utf-8")
        return f"edited {path}; replacements={count if replace_all else 1}"


def _read_workspace_line_range(
    context: ToolContext,
    path: Any,
    *,
    display_path: str,
    start_line: int,
    max_lines: int,
) -> str:
    data = path.read_bytes()
    if b"\x00" in data[:4096]:
        raise ValueError(f"refusing to read binary-looking file: {path}")
    lines = data.decode("utf-8").splitlines()
    start = max(1, start_line)
    limit = max(1, max_lines)
    end = min(len(lines), start + limit - 1)
    selected = lines[start - 1 : end]
    normalized_path = display_path.replace("\\", "/")
    body = "\n".join(f"{line_number}: {line}" for line_number, line in enumerate(selected, start=start))
    suffix = "\n" if body else ""
    return f"{normalized_path} lines {start}-{end} of {len(lines)}\n{body}{suffix}"


def _read_workspace_text_or_navigation_hint(context: ToolContext, path: Path, *, display_path: str) -> str:
    text = read_workspace_text(context, path)
    lines = text.splitlines()
    if len(lines) <= _FULL_READ_LINE_LIMIT:
        return text
    normalized_path = display_path.replace("\\", "/")
    return (
        f"{normalized_path} has {len(lines)} lines, which is too large for an unbounded read_file call.\n"
        "Use grep_search to find a unique symbol or error text, then call read_file with "
        "start_line and max_lines around the relevant section. Example: "
        f'{{"path": "{normalized_path}", "start_line": 100, "max_lines": 80}}'
    )


def _unique_quote_normalized_span(text: str, old: str) -> tuple[int, int] | None:
    normalized_text = _normalize_quotes(text)
    normalized_old = _normalize_quotes(old)
    start = normalized_text.find(normalized_old)
    if start == -1:
        return None
    if normalized_text.find(normalized_old, start + 1) != -1:
        return None
    return start, start + len(old)


def _apply_context_indentation(text: str, start: int, replacement: str) -> str:
    if "\n" not in replacement or not replacement:
        return replacement
    line_start = text.rfind("\n", 0, start) + 1
    prefix = text[line_start:start]
    if not prefix or prefix.strip():
        return replacement
    lines = replacement.splitlines(keepends=True)
    if lines[0].startswith(prefix):
        lines[0] = lines[0][len(prefix) :]
        return "".join(lines)
    if replacement[0].isspace():
        return replacement
    if _replacement_uses_absolute_indentation(lines[1:], prefix):
        return lines[0] + "".join(_indent_only_unindented_line(line, prefix) for line in lines[1:])
    return lines[0] + "".join(_indent_replacement_line(line, prefix) for line in lines[1:])


def _replacement_uses_absolute_indentation(lines: list[str], prefix: str) -> bool:
    if not prefix:
        return False
    threshold = len(prefix) * 2
    return any(_leading_whitespace_width(line) >= threshold for line in lines if line.strip())


def _leading_whitespace_width(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _indent_only_unindented_line(line: str, prefix: str) -> str:
    if not line.strip() or line.startswith(prefix):
        return line
    return prefix + line


def _indent_replacement_line(line: str, prefix: str) -> str:
    if not line.strip():
        return line
    return prefix + line


def _unique_whitespace_insensitive_span(text: str, old: str) -> tuple[int, int] | None:
    compact_text, offsets = _compact_code_with_offsets(text)
    compact_old, _old_offsets = _compact_code_with_offsets(old)
    if not compact_old:
        return None
    start = compact_text.find(compact_old)
    if start == -1:
        return None
    if compact_text.find(compact_old, start + 1) != -1:
        return None
    end = start + len(compact_old) - 1
    return offsets[start], offsets[end] + 1


def _extend_python_assert_message_span(text: str, start: int, end: int, old: str) -> int:
    compact_old, _old_offsets = _compact_code_with_offsets(old)
    if not compact_old.startswith("assert"):
        return end
    index = end
    while index < len(text) and text[index] in " \t":
        index += 1
    if index >= len(text) or text[index] != ",":
        return end
    line_end = text.find("\n", index)
    return len(text) if line_end == -1 else line_end


def _compact_code_with_offsets(value: str) -> tuple[str, list[int]]:
    chars: list[str] = []
    offsets: list[int] = []
    for index, char in enumerate(value):
        if char.isspace():
            continue
        if char == "," and _next_non_whitespace(value, index + 1) in {")", "]", "}"}:
            continue
        chars.append(char)
        offsets.append(index)
    return "".join(chars), offsets


def _next_non_whitespace(value: str, start: int) -> str | None:
    for char in value[start:]:
        if not char.isspace():
            return char
    return None


def _normalize_quotes(value: str) -> str:
    return value.replace("'", '"')


def _reject_python_syntax_regression(path: Path, old_text: str, new_text: str) -> None:
    if path.suffix != ".py":
        return
    try:
        old_tree = ast.parse(old_text)
    except SyntaxError:
        return
    try:
        new_tree = ast.parse(new_text)
    except SyntaxError as error:
        raise ValueError(
            "invalid Python syntax after edit_file; edit rejected and file left unchanged. "
            f"{path.name}:{error.lineno}:{error.offset}: {error.msg}"
        ) from error
    _reject_header_row_format_mismatch(path, new_tree)
    _reject_super_init_keyword_expansion(path, old_tree, new_tree)


def _reject_super_init_keyword_expansion(path: Path, old_tree: ast.AST, new_tree: ast.AST) -> None:
    old_keywords = _super_init_keyword_names(old_tree)
    new_keywords = _super_init_keyword_names(new_tree)
    added = sorted(new_keywords - old_keywords)
    if not added:
        return
    names = ", ".join(added[:5])
    suffix = "..." if len(added) > 5 else ""
    raise ValueError(
        "super().__init__ keyword expansion rejected after edit_file; edit rejected and file left unchanged. "
        f"{path.name}: added keyword(s) {names}{suffix}. Inspect the base class signature instead of "
        "passing unrelated constructor arguments through super()."
    )


def _super_init_keyword_names(tree: ast.AST) -> set[str]:
    keywords: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "__init__":
            continue
        receiver = node.func.value
        if not isinstance(receiver, ast.Call):
            continue
        if not isinstance(receiver.func, ast.Name) or receiver.func.id != "super":
            continue
        for keyword in node.keywords:
            if keyword.arg:
                keywords.add(keyword.arg)
    return keywords


def _reject_header_row_format_mismatch(path: Path, tree: ast.AST) -> None:
    header_count: int | None = None
    widths_count: int | None = None
    row_columns: int | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        target_names = [target.id for target in node.targets if isinstance(target, ast.Name)]
        if "headers" in target_names and isinstance(node.value, ast.Tuple):
            if all(isinstance(item, ast.Constant) and isinstance(item.value, str) for item in node.value.elts):
                header_count = len(node.value.elts)
        if "row" in target_names:
            template = _row_format_template(node.value)
            if template is not None:
                row_columns = _row_format_column_count(template)
        if "widths" in target_names and isinstance(node.value, ast.Tuple):
            widths_count = len(node.value.elts)
    if row_columns is None:
        return
    if header_count is not None and header_count != row_columns:
        raise ValueError(
            "invalid table format after edit_file; edit rejected and file left unchanged. "
            f"{path.name}: row format has {row_columns} columns but headers defines {header_count}. "
            "Update headers, widths, row format, and row.format arguments together."
        )
    if widths_count is not None and widths_count != row_columns:
        raise ValueError(
            "invalid table format after edit_file; edit rejected and file left unchanged. "
            f"{path.name}: row format has {row_columns} columns but widths defines {widths_count}. "
            "Update headers, widths, row format, and row.format arguments together."
        )


def _row_format_template(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    if not isinstance(node.func, ast.Attribute) or node.func.attr != "format":
        return None
    if isinstance(node.func.value, ast.Constant) and isinstance(node.func.value.value, str):
        return node.func.value.value
    return None


def _row_format_column_count(template: str) -> int:
    indices = {int(match.group(1)) for match in re.finditer(r"\{\{(\d+)(?=[:}])", template)}
    if indices:
        return max(indices) + 1
    indices = {int(match.group(1)) for match in re.finditer(r"(?<!\{)\{(\d+)(?=[:}])", template)}
    if indices:
        return max(indices) + 1
    return 0
