"""MCP configuration loading and validation."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import MCPConfigError


SENSITIVE_KEY_PARTS = ("authorization", "token", "secret", "password", "api_key", "apikey", "key")
ENV_PATTERN = re.compile(r"\$\{([^}]+)\}")


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    transport: str = "stdio"
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    startup_timeout: float = 20
    request_timeout: float = 60
    tool_prefix: str | None = None
    disabled_tools: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> "MCPServerConfig":
        if not isinstance(data, dict):
            raise MCPConfigError(f"MCP server config must be an object: {name}")
        transport = str(data.get("transport", "stdio"))
        if transport not in {"stdio", "streamable_http"}:
            raise MCPConfigError(f"unsupported MCP transport for {name}: {transport}")

        command = _optional_string(data.get("command"))
        url = _optional_string(data.get("url"))
        if transport == "stdio" and not command:
            raise MCPConfigError(f"stdio MCP server requires command: {name}")
        if transport == "streamable_http" and not url:
            raise MCPConfigError(f"streamable_http MCP server requires url: {name}")

        return cls(
            name=name,
            transport=transport,
            command=command,
            args=_string_list(data.get("args", []), f"{name}.args"),
            env=_string_mapping(data.get("env", {}), f"{name}.env"),
            url=url,
            headers=_string_mapping(data.get("headers", {}), f"{name}.headers"),
            enabled=bool(data.get("enabled", True)),
            startup_timeout=float(data.get("startup_timeout", 20)),
            request_timeout=float(data.get("request_timeout", 60)),
            tool_prefix=_optional_string(data.get("tool_prefix")) or f"mcp_{_safe_name(name)}",
            disabled_tools=_string_list(data.get("disabled_tools", []), f"{name}.disabled_tools"),
        )

    def expanded_env(self, environ: dict[str, str] | None = None) -> dict[str, str]:
        return expand_mapping(self.env, environ)

    def expanded_headers(self, environ: dict[str, str] | None = None) -> dict[str, str]:
        return expand_mapping(self.headers, environ)


@dataclass(frozen=True)
class MCPConfig:
    servers: dict[str, MCPServerConfig] = field(default_factory=dict)
    loaded_files: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any], loaded_files: tuple[str, ...] = ()) -> "MCPConfig":
        servers_data = data.get("mcpServers", {})
        if not isinstance(servers_data, dict):
            raise MCPConfigError("mcpServers must be an object")
        servers = {name: MCPServerConfig.from_dict(name, value) for name, value in servers_data.items()}
        return cls(servers=servers, loaded_files=loaded_files)

    def enabled_servers(self) -> dict[str, MCPServerConfig]:
        return {name: config for name, config in self.servers.items() if config.enabled}


def load_mcp_config(
    workspace: str | Path,
    user_config_home: str | Path | None = None,
    start_dir: str | Path | None = None,
    *,
    allow_workspace_config: bool = False,
) -> MCPConfig:
    """Load MCP configuration with an explicit trust boundary.

    User-level configuration is the default source. Workspace and process
    start-directory files can launch arbitrary local processes or authorize
    network tools, so callers must opt in to loading them explicitly.
    """
    workspace_path = Path(workspace).expanduser().resolve()
    home = Path(user_config_home).expanduser() if user_config_home else Path.home() / ".insightagent"
    candidate_paths = [
        home / "mcp_config.json",
    ]
    if allow_workspace_config:
        if start_dir is not None:
            start_path = Path(start_dir).expanduser().resolve()
            candidate_paths.extend(
                [
                    start_path / ".insightagent" / "mcp_config.json",
                    start_path / "mcp_config.json",
                ]
            )
        candidate_paths.extend(
            [
                workspace_path / ".insightagent" / "mcp_config.json",
                workspace_path / "mcp_config.json",
            ]
        )
    merged: dict[str, Any] = {}
    loaded: list[str] = []
    seen: set[Path] = set()
    for path in candidate_paths:
        if path in seen:
            continue
        seen.add(path)
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise MCPConfigError(f"invalid MCP config JSON: {path}: {error}") from error
        if not isinstance(data, dict):
            raise MCPConfigError(f"MCP config must contain a JSON object: {path}")
        merged = _deep_merge(merged, data)
        loaded.append(str(path))
    return MCPConfig.from_dict(merged, tuple(loaded))


def expand_env_value(value: str, environ: dict[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    return ENV_PATTERN.sub(lambda match: env.get(match.group(1), ""), value)


def expand_mapping(values: dict[str, str], environ: dict[str, str] | None = None) -> dict[str, str]:
    return {key: expand_env_value(value, environ) for key, value in values.items()}


def redact_mapping(values: dict[str, str]) -> dict[str, str]:
    redacted: dict[str, str] = {}
    for key, value in values.items():
        lowered = key.lower()
        if any(part in lowered for part in SENSITIVE_KEY_PARTS):
            redacted[key] = "<redacted>"
        else:
            redacted[key] = value
    return redacted


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _safe_name(name: str) -> str:
    safe = re.sub(r"[^0-9A-Za-z_]+", "_", name).strip("_").lower()
    return safe or "server"


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MCPConfigError(f"expected string value, got {type(value).__name__}")
    return value


def _string_list(value: Any, field_name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise MCPConfigError(f"{field_name} must be a list of strings")
    return list(value)


def _string_mapping(value: Any, field_name: str) -> dict[str, str]:
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise MCPConfigError(f"{field_name} must be an object with string values")
    return dict(value)
