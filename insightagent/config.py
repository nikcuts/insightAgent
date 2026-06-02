"""Runtime configuration loading for InsightAgent V5.0."""

from __future__ import annotations

import json
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
    max_tool_output_chars: int = 8000
    compact_tool_output_chars: int = 600
    permission_mode: str = "workspace-write"
    trace_max_chars: int = 1000
    session_dir: str = ".insightagent/sessions"
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
    home = Path(user_config_home).expanduser() if user_config_home else Path.home() / ".insightagent"
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


def _config_from_dict(data: dict[str, Any], loaded_files: tuple[str, ...]) -> RuntimeConfig:
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
        "max_tool_iterations": runtime.get("max_tool_iterations", data.get("max_tool_iterations")),
        "max_tool_output_chars": runtime.get("max_tool_output_chars", data.get("max_tool_output_chars")),
        "compact_tool_output_chars": runtime.get("compact_tool_output_chars", data.get("compact_tool_output_chars")),
        "permission_mode": permissions.get("mode", data.get("permission_mode")),
        "trace_max_chars": tracing.get("max_chars", data.get("trace_max_chars")),
        "session_dir": sessions.get("dir", data.get("session_dir")),
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


def _object_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


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
        elif key in {"timeout", "max_tool_iterations", "max_tool_output_chars", "compact_tool_output_chars"}:
            normalized = _deep_merge(normalized, {"runtime": {key: value}})
        elif key == "permission_mode":
            normalized = _deep_merge(normalized, {"permissions": {"mode": value}})
        elif key == "trace_max_chars":
            normalized = _deep_merge(normalized, {"tracing": {"max_chars": value}})
        elif key == "session_dir":
            normalized = _deep_merge(normalized, {"sessions": {"dir": value}})
        else:
            normalized[key] = value
    return normalized
