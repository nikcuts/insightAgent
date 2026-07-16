"""Runtime configuration loading for InsightAgent V5.0."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RuntimeConfig:
    provider: str = "siliconflow"
    model: str | None = None
    base_url: str | None = None
    timeout: int = 300
    max_tool_iterations: int = 12
    max_output_tokens: int = 4096
    temperature: float = 0.01
    top_p: float = 0.95
    max_retries: int = 3
    max_wall_seconds: float = 0.0
    max_tool_output_chars: int = 8000
    compact_tool_output_chars: int = 600
    permission_mode: str = "workspace-write"
    trace_max_chars: int = 1000
    session_dir: str = ".insightagent/sessions"
    response_language: str = "auto"
    loaded_files: tuple[str, ...] = ()


CONFIG_FILENAMES = (
    "config.json",
    ".insightagent/config.json",
    ".insightagent/local.json",
)


def load_runtime_config(
    workspace: str | Path,
    user_config_home: str | Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> RuntimeConfig:
    workspace_path = Path(workspace).expanduser().resolve()
    home = (
        Path(user_config_home).expanduser()
        if user_config_home
        else Path.home() / ".insightagent"
    )
    candidate_paths = [
        home / "config.json",
        workspace_path / ".insightagent" / "config.json",
        workspace_path / ".insightagent" / "local.json",
    ]
    merged: dict[str, Any] = {}
    loaded = []
    for path in candidate_paths:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError(f"config file must contain a JSON object: {path}")
            merged = _deep_merge(merged, data)
            loaded.append(str(path))
    if overrides:
        merged = _deep_merge(merged, _normalize_overrides(overrides))
    return _config_from_dict(merged, tuple(loaded))


def load_dotenv_files(
    workspace: str | Path,
    start_dir: str | Path | None = None,
) -> tuple[str, ...]:
    """Load simple KEY=VALUE lines from .env files without overriding existing env."""
    workspace_path = Path(workspace).expanduser().resolve()
    candidates: list[Path] = []
    if start_dir is not None:
        start_path = Path(start_dir).expanduser().resolve()
        candidates.append(start_path / ".env")
    candidates.append(workspace_path / ".env")
    loaded: list[str] = []
    seen: set[Path] = set()
    for path in candidates:
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        _load_dotenv_file(path)
        loaded.append(str(path))
    return tuple(loaded)


def _config_from_dict(
    data: dict[str, Any], loaded_files: tuple[str, ...]
) -> RuntimeConfig:
    config = RuntimeConfig(loaded_files=loaded_files)
    runtime = _object_or_empty(data.get("runtime"))
    model = _object_or_empty(data.get("model"))
    permissions = _object_or_empty(data.get("permissions"))
    tracing = _object_or_empty(data.get("tracing"))
    sessions = _object_or_empty(data.get("sessions"))
    flat_model_name = data.get("model") if isinstance(data.get("model"), str) else None
    flattened = {
        "provider": model.get("provider", data.get("provider")),
        "model": model.get("name", flat_model_name),
        "base_url": model.get("base_url", data.get("base_url")),
        "timeout": runtime.get("timeout", data.get("timeout")),
        "max_tool_iterations": runtime.get(
            "max_tool_iterations", data.get("max_tool_iterations")
        ),
        "max_output_tokens": runtime.get(
            "max_output_tokens", data.get("max_output_tokens")
        ),
        "temperature": runtime.get("temperature", data.get("temperature")),
        "top_p": runtime.get("top_p", data.get("top_p")),
        "max_retries": runtime.get("max_retries", data.get("max_retries")),
        "max_wall_seconds": runtime.get(
            "max_wall_seconds", data.get("max_wall_seconds")
        ),
        "max_tool_output_chars": runtime.get(
            "max_tool_output_chars", data.get("max_tool_output_chars")
        ),
        "compact_tool_output_chars": runtime.get(
            "compact_tool_output_chars", data.get("compact_tool_output_chars")
        ),
        "permission_mode": permissions.get("mode", data.get("permission_mode")),
        "trace_max_chars": tracing.get("max_chars", data.get("trace_max_chars")),
        "session_dir": sessions.get("dir", data.get("session_dir")),
        "response_language": runtime.get(
            "response_language", data.get("response_language", data.get("language"))
        ),
    }
    updates = {key: value for key, value in flattened.items() if value is not None}
    return replace(config, **updates)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_dotenv_file(path: Path) -> None:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = _strip_env_value(value.strip())


def _strip_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _object_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def language_directive(language: str | None) -> str:
    """Build a system-prompt directive that controls the model's reply language.

    ``"auto"`` (the default) tells the model to mirror the user's language, so a
    Chinese task yields Chinese plans/summaries without extra configuration. Any
    other value pins the assistant-visible text to that language.
    """

    value = (language or "auto").strip()
    if value.lower() in {"", "auto"}:
        return (
            "Respond in the same language as the user's most recent request. For example, if the "
            "user writes in Chinese, write your plan, explanations, and final summary in Chinese. "
            "Keep code, identifiers, file names, and shell commands in their original form."
        )
    return (
        f"Always write your assistant-visible text (plans, explanations, and summaries) in {value}. "
        "Keep code, identifiers, file names, and shell commands in their original form."
    )


def _normalize_overrides(overrides: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, value in overrides.items():
        if value is None:
            continue
        if key == "model":
            normalized = _deep_merge(normalized, {"model": {"name": value}})
        elif key == "provider":
            normalized = _deep_merge(normalized, {"model": {"provider": value}})
        elif key == "base_url":
            normalized = _deep_merge(normalized, {"model": {"base_url": value}})
        elif key in {
            "timeout",
            "max_tool_iterations",
            "max_output_tokens",
            "temperature",
            "top_p",
            "max_retries",
            "max_wall_seconds",
            "max_tool_output_chars",
            "compact_tool_output_chars",
            "response_language",
        }:
            normalized = _deep_merge(normalized, {"runtime": {key: value}})
        elif key == "language":
            normalized = _deep_merge(
                normalized, {"runtime": {"response_language": value}}
            )
        elif key == "permission_mode":
            normalized = _deep_merge(normalized, {"permissions": {"mode": value}})
        elif key == "trace_max_chars":
            normalized = _deep_merge(normalized, {"tracing": {"max_chars": value}})
        elif key == "session_dir":
            normalized = _deep_merge(normalized, {"sessions": {"dir": value}})
        else:
            normalized[key] = value
    return normalized
