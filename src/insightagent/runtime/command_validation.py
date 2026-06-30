"""Shell command intent classification and validation."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from enum import Enum


class CommandKind(str, Enum):
    READ = "read"
    TEST = "test"
    BUILD = "build"
    INSTALL = "install"
    NETWORK = "network"
    MUTATING = "mutating"
    DESTRUCTIVE = "destructive"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CommandDecision:
    kind: CommandKind
    allowed: bool = True
    reason: str = ""
    retryable_by_default: bool = False


class CommandValidator:
    destructive_commands = {"rm", "rmdir", "mkfs", "shutdown", "reboot", "poweroff", "dd"}
    mutating_commands = {"mv", "cp", "touch", "mkdir", "chmod", "chown", "sed"}
    network_commands = {"curl", "wget", "ping", "ssh", "scp", "rsync", "dig", "nslookup"}
    install_commands = {"pip", "pip3", "npm", "pnpm", "yarn", "apt", "apt-get", "brew"}
    build_commands = {"make", "cmake", "cargo", "go", "gcc", "g++", "javac", "mvn", "gradle"}
    read_commands = {"cat", "sed", "awk", "head", "tail", "ls", "pwd", "find", "rg", "grep", "git"}

    def classify(self, command: str) -> CommandDecision:
        tokens = _split_command(command)
        first = _base_command(tokens[0]) if tokens else ""
        if first in self.destructive_commands or re.search(r"\brm\s+-[^\n;|&]*[rf]", command):
            return CommandDecision(CommandKind.DESTRUCTIVE, retryable_by_default=False)
        if first in self.network_commands or re.search(r"https?://|git\s+clone|git\s+pull|git\s+fetch", command):
            return CommandDecision(CommandKind.NETWORK, retryable_by_default=False)
        if _looks_like_test(command):
            return CommandDecision(CommandKind.TEST, retryable_by_default=False)
        if first in self.install_commands and _looks_like_install(command):
            return CommandDecision(CommandKind.INSTALL, retryable_by_default=False)
        if first in self.build_commands:
            return CommandDecision(CommandKind.BUILD, retryable_by_default=False)
        if first in self.mutating_commands or _has_write_redirection(command):
            return CommandDecision(CommandKind.MUTATING, retryable_by_default=False)
        if first in self.read_commands:
            return CommandDecision(CommandKind.READ, retryable_by_default=False)
        return CommandDecision(CommandKind.UNKNOWN, retryable_by_default=False)

    def validate(self, command: str, permission_mode: str) -> CommandDecision:
        decision = self.classify(command)
        if decision.kind == CommandKind.DESTRUCTIVE:
            return CommandDecision(
                decision.kind,
                allowed=False,
                reason="destructive shell command denied by runtime policy",
                retryable_by_default=False,
            )
        if permission_mode == "read-only" and decision.kind in {
            CommandKind.BUILD,
            CommandKind.INSTALL,
            CommandKind.MUTATING,
            CommandKind.UNKNOWN,
        }:
            return CommandDecision(
                decision.kind,
                allowed=False,
                reason=f"{decision.kind.value} command denied in read-only mode",
                retryable_by_default=False,
            )
        return decision


def _split_command(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _base_command(token: str) -> str:
    return token.rsplit("/", 1)[-1]


def _looks_like_test(command: str) -> bool:
    lowered = command.lower()
    patterns = (
        r"\bpytest\b",
        r"\bunittest\b",
        r"\bnosetests\b",
        r"\bcargo\s+test\b",
        r"\bnpm\s+test\b",
        r"\bpnpm\s+test\b",
        r"\byarn\s+test\b",
        r"\bgo\s+test\b",
    )
    return any(re.search(pattern, lowered) for pattern in patterns)


def _looks_like_install(command: str) -> bool:
    lowered = command.lower()
    return any(word in lowered for word in (" install", " add ", " update", " upgrade"))


def _has_write_redirection(command: str) -> bool:
    return bool(re.search(r"(^|[^<])>{1,2}[^>]", command)) or " tee " in command or " sed -i" in command
