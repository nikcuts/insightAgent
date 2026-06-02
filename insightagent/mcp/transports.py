"""MCP transport implementations."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from collections import deque
from queue import Empty, Queue
from typing import Any

from .errors import MCPRequestTimeout, MCPTransportError
from .protocol import JsonRpcIdGenerator, build_notification, build_request, parse_response


class StdioTransport:
    def __init__(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.name = name
        self.command = command
        self.args = args or []
        self.env = env or {}
        self.process: subprocess.Popen[str] | None = None
        self._ids = JsonRpcIdGenerator()
        self._stdout_queue: Queue[dict[str, Any]] = Queue()
        self._stderr_lines: deque[str] = deque(maxlen=20)
        self._write_lock = threading.Lock()
        self._running = False

    def start(self) -> None:
        if self.is_running():
            return
        process_env = os.environ.copy()
        process_env.update(self.env)
        try:
            self.process = subprocess.Popen(
                [self.command] + self.args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=process_env,
            )
        except OSError as error:
            raise MCPTransportError(f"failed to start MCP server {self.name}: {error}") from error
        self._running = True
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def stop(self) -> None:
        self._running = False
        process = self.process
        if process is None:
            return
        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        for stream in (process.stdout, process.stderr):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass
        self.process = None

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def send_request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 60,
    ) -> Any:
        message_id = self._ids.next()
        self._write_message(build_request(message_id, method, params))
        while True:
            try:
                message = self._stdout_queue.get(timeout=timeout)
            except Empty as error:
                raise MCPRequestTimeout(f"MCP request timed out: {self.name}.{method}") from error
            if "id" not in message:
                continue
            if message.get("id") != message_id:
                self._stdout_queue.put(message)
                continue
            return parse_response(message, expected_id=message_id)

    def send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._write_message(build_notification(method, params))

    def stderr_summary(self) -> str:
        return "\n".join(self._stderr_lines)

    def _write_message(self, message: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None or process.poll() is not None:
            raise MCPTransportError(f"MCP server is not running: {self.name}")
        payload = json.dumps(message, ensure_ascii=False)
        with self._write_lock:
            try:
                process.stdin.write(payload + "\n")
                process.stdin.flush()
            except OSError as error:
                raise MCPTransportError(f"failed to write MCP message: {self.name}: {error}") from error

    def _read_stdout(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        while self._running and process.poll() is None:
            line = process.stdout.readline()
            if not line:
                break
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                self._stderr_lines.append(f"non-json stdout: {line.strip()}")
                continue
            if isinstance(message, dict):
                self._stdout_queue.put(message)

    def _read_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        while self._running and process.poll() is None:
            line = process.stderr.readline()
            if not line:
                break
            self._stderr_lines.append(line.rstrip())


class StreamableHttpTransport:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise MCPTransportError("StreamableHttpTransport is not implemented yet")
