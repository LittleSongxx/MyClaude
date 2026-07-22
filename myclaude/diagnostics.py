from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Diagnostic:
    line: int
    column: int
    message: str
    severity: str = "error"
    source: str = "lsp"

    def render(self) -> str:
        return f"{self.severity} {self.line}:{self.column} [{self.source}] {self.message}"


@dataclass(frozen=True)
class DiagnosticReport:
    diagnostics: list[Diagnostic]
    engine: str

    def render(self) -> str:
        if not self.diagnostics:
            return ""
        return "\n".join(diagnostic.render() for diagnostic in self.diagnostics)


_LANGUAGES: dict[str, tuple[str, list[str]]] = {
    ".py": ("python", ["pyright-langserver", "--stdio"]),
    ".js": ("javascript", ["typescript-language-server", "--stdio"]),
    ".jsx": ("javascriptreact", ["typescript-language-server", "--stdio"]),
    ".ts": ("typescript", ["typescript-language-server", "--stdio"]),
    ".tsx": ("typescriptreact", ["typescript-language-server", "--stdio"]),
    ".rs": ("rust", ["rust-analyzer"]),
    ".go": ("go", ["gopls", "serve"]),
    ".c": ("c", ["clangd"]),
    ".cc": ("cpp", ["clangd"]),
    ".cpp": ("cpp", ["clangd"]),
}
_SEVERITY = {1: "error", 2: "warning", 3: "information", 4: "hint"}


async def _read_message(reader: asyncio.StreamReader) -> dict[str, Any]:
    headers: dict[str, str] = {}
    while True:
        line = await reader.readline()
        if not line:
            raise EOFError("language server closed stdout")
        if line in {b"\r\n", b"\n"}:
            break
        key, _, value = line.decode("ascii", errors="replace").partition(":")
        headers[key.strip().lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    if length <= 0:
        raise ValueError("language server message missing Content-Length")
    payload = await reader.readexactly(length)
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("language server returned a non-object message")
    return value


async def _write_message(
    writer: asyncio.StreamWriter, payload: dict[str, Any]
) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    writer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    await writer.drain()


class LSPDiagnostics:
    """One-shot LSP diagnostics with deterministic syntax fallbacks."""

    def __init__(self, timeout: float = 5.0) -> None:
        self.timeout = timeout

    @staticmethod
    def _command(language: str, default: list[str]) -> list[str] | None:
        override = os.environ.get(f"MYCLAUDE_LSP_{language.upper()}", "").strip()
        command = shlex.split(override) if override else list(default)
        if not command:
            return None
        executable = command[0]
        if Path(executable).is_absolute():
            return command if Path(executable).is_file() else None
        return command if shutil.which(executable) else None

    async def diagnose(self, path: Path, *, workspace: Path) -> DiagnosticReport:
        path = path.expanduser().resolve()
        language_config = _LANGUAGES.get(path.suffix.lower())
        if os.environ.get("MYCLAUDE_LSP_DIAGNOSTICS", "1") == "0":
            return DiagnosticReport([], "disabled")
        if language_config is not None:
            language, default = language_config
            command = self._command(language, default)
            if command is not None:
                try:
                    diagnostics = await asyncio.wait_for(
                        self._run_lsp(command, language, path, workspace.resolve()),
                        timeout=self.timeout,
                    )
                    return DiagnosticReport(diagnostics, "lsp")
                except (asyncio.TimeoutError, EOFError, OSError, ValueError, json.JSONDecodeError):
                    pass
        return await asyncio.to_thread(self._fallback, path)

    async def _run_lsp(
        self,
        command: list[str],
        language: str,
        path: Path,
        workspace: Path,
    ) -> list[Diagnostic]:
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=str(workspace),
        )
        assert proc.stdin is not None and proc.stdout is not None
        uri = path.as_uri()
        try:
            await _write_message(proc.stdin, {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "processId": os.getpid(),
                    "rootUri": workspace.as_uri(),
                    "capabilities": {"textDocument": {"publishDiagnostics": {}}},
                    "workspaceFolders": [{"uri": workspace.as_uri(), "name": workspace.name}],
                },
            })
            await self._wait_for(proc, response_id=1)
            await _write_message(proc.stdin, {
                "jsonrpc": "2.0", "method": "initialized", "params": {}
            })
            text = path.read_text(encoding="utf-8")
            await _write_message(proc.stdin, {
                "jsonrpc": "2.0",
                "method": "textDocument/didOpen",
                "params": {
                    "textDocument": {
                        "uri": uri, "languageId": language, "version": 1, "text": text,
                    }
                },
            })
            message = await self._wait_for(proc, method="textDocument/publishDiagnostics", uri=uri)
            return self._parse_diagnostics(message.get("params", {}).get("diagnostics", []))
        finally:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()

    async def _wait_for(
        self,
        proc: asyncio.subprocess.Process,
        *,
        response_id: int | None = None,
        method: str = "",
        uri: str = "",
    ) -> dict[str, Any]:
        assert proc.stdin is not None and proc.stdout is not None
        while True:
            message = await _read_message(proc.stdout)
            if response_id is not None and message.get("id") == response_id:
                return message
            if method and message.get("method") == method:
                params = message.get("params", {})
                if not uri or params.get("uri") == uri:
                    return message
            if "id" in message and "method" in message:
                requested = message.get("method")
                result: Any = None
                if requested == "workspace/configuration":
                    items = message.get("params", {}).get("items", [])
                    result = [None for _ in items]
                await _write_message(proc.stdin, {
                    "jsonrpc": "2.0", "id": message["id"], "result": result
                })

    @staticmethod
    def _parse_diagnostics(rows: Any) -> list[Diagnostic]:
        diagnostics: list[Diagnostic] = []
        if not isinstance(rows, list):
            return diagnostics
        for row in rows:
            if not isinstance(row, dict):
                continue
            start = row.get("range", {}).get("start", {})
            diagnostics.append(Diagnostic(
                line=int(start.get("line", 0)) + 1,
                column=int(start.get("character", 0)) + 1,
                message=str(row.get("message", "diagnostic")),
                severity=_SEVERITY.get(row.get("severity"), "warning"),
                source=str(row.get("source") or "lsp"),
            ))
        return diagnostics

    @staticmethod
    def _fallback(path: Path) -> DiagnosticReport:
        if path.suffix.lower() == ".py":
            try:
                source = path.read_text(encoding="utf-8")
                compile(source, str(path), "exec")
            except SyntaxError as error:
                return DiagnosticReport([
                    Diagnostic(
                        line=error.lineno or 1,
                        column=error.offset or 1,
                        message=error.msg,
                        severity="error",
                        source="python-syntax",
                    )
                ], "python-syntax")
            except OSError:
                return DiagnosticReport([], "unavailable")
        elif path.suffix.lower() == ".json":
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as error:
                return DiagnosticReport([
                    Diagnostic(error.lineno, error.colno, error.msg, source="json")
                ], "json")
            except OSError:
                return DiagnosticReport([], "unavailable")
        return DiagnosticReport([], "unavailable")


async def append_post_edit_diagnostics(
    result: Any,
    path: Path,
    diagnostics: LSPDiagnostics | None,
    *,
    workspace: Path,
) -> Any:
    if diagnostics is None or result.is_error:
        return result
    report = await diagnostics.diagnose(path, workspace=workspace)
    result.metadata["diagnostics_engine"] = report.engine
    result.metadata["diagnostics"] = [diagnostic.render() for diagnostic in report.diagnostics]
    rendered = report.render()
    if rendered:
        result.output = result.output.rstrip() + "\n\nDiagnostics after edit:\n" + rendered
    return result
