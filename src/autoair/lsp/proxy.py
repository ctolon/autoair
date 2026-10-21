"""Language-server proxy: LSP JSON-RPC between the browser (WebSocket) and a per-session
language-server process (``features.ide_language_server``).

* One process per editor session (basedpyright or python-lsp-server for Python,
  bash-language-server for shell), started in a fresh temporary workspace and killed — with
  its process group — when the WebSocket closes, after ``lsp.idle_timeout`` without a
  message, or when it exceeds its resource limits. ``lsp.max_sessions`` /
  ``max_sessions_per_user`` bound the number of processes.
* The browser speaks LSP over the WebSocket (one JSON message per frame); the process speaks
  it on stdio with ``Content-Length`` framing.
* Documents use *virtual* URIs ``file:///autoair/ws/<name>.py?…`` that the proxy maps to files
  of the workspace. A ``@task`` body (``?mode=task_body&args=a,b&loop=1&fn=name``) is wrapped
  in its function signature — like the jedi services do — so arguments and Airflow's context
  (``ti``, ``ds``, ``params``, ``dag_run`` …) resolve; positions, edits and diagnostics are
  translated between the body the user edits and the wrapped file the server analyses.
* ``workspace/configuration`` requests are answered here (the server analyses against the
  Python environment running AutoAir, where Airflow and the providers are installed), and
  the custom request ``autoair/readFile`` returns read-only library sources (``.py``/``.pyi``
  below the Python installation) so the editor can peek at definitions.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import os
import re
import shutil
import signal
import sys
import sysconfig
import tempfile
import textwrap
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, quote, unquote, urlsplit

from autoair._subprocess import user_code_env
from autoair.lsp.config import LspSettings

log = logging.getLogger(__name__)

Language = Literal["python", "bash"]
LANGUAGES: tuple[Language, ...] = ("python", "bash")
CLIENT_ROOT = "file:///autoair/ws/"
CONTEXT_STUB = "_autoair_context"
_INDENT = "    "
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")
_READ_SUFFIXES = (".py", ".pyi")
_READ_MAX = 2 * 1024 * 1024
_WATCH_INTERVAL = 1.0


class LspError(Exception):
    """A session cannot start (unknown language, missing server, limits)."""

    def __init__(self, message: str, code: int = 1011) -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- servers


def resolve_binary(name: str) -> str | None:
    """``name`` on PATH, or installed next to the running Python (a wheel's console script,
    e.g. ``basedpyright-langserver`` from ``pip install basedpyright``)."""
    if os.sep in name or (os.altsep and os.altsep in name):
        return name if Path(name).is_file() else None
    found = shutil.which(name)
    if found:
        return found
    local = Path(sys.executable).parent / name
    return str(local) if local.is_file() else None


def server_name(settings: LspSettings, language: Language) -> str:
    return settings.python_server if language == "python" else "bash-language-server"


def server_command(settings: LspSettings, language: Language) -> list[str] | None:
    """The resolved command of ``language``'s server, None when it is not installed."""
    if language == "python":
        command = (
            settings.basedpyright_command
            if settings.python_server == "basedpyright"
            else settings.pylsp_command
        )
    elif language == "bash":
        command = settings.bash_command
    else:
        return None
    if not command:
        return None
    binary = resolve_binary(command[0])
    return [binary, *command[1:]] if binary else None


def uses_node(settings: LspSettings, language: Language) -> bool:
    """Node-based servers (basedpyright, bash-language-server) reserve a large virtual
    address space up front: their memory limit is V8's heap size, not RLIMIT_AS."""
    return language == "bash" or settings.python_server == "basedpyright"


def server_status(settings: LspSettings) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for language in LANGUAGES:
        command = server_command(settings, language)
        out[language] = {
            "server": server_name(settings, language),
            "available": command is not None,
        }
    return out


# --------------------------------------------------------------------------- framing


class FrameError(ValueError):
    pass


def frame(body: bytes) -> bytes:
    return b"Content-Length: %d\r\n\r\n" % len(body) + body


class Framer:
    """Incremental ``Content-Length`` decoder of a language server's stdout."""

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self.buffer = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self.buffer += data
        messages: list[bytes] = []
        while True:
            end = self.buffer.find(b"\r\n\r\n")
            if end < 0:
                if len(self.buffer) > 8192:
                    raise FrameError("LSP header too long")
                return messages
            length: int | None = None
            for line in bytes(self.buffer[:end]).split(b"\r\n"):
                name, _, value = line.partition(b":")
                if name.strip().lower() == b"content-length":
                    try:
                        length = int(value.strip())
                    except ValueError as exc:
                        raise FrameError("bad Content-Length") from exc
            if length is None or length < 0:
                raise FrameError("missing Content-Length")
            if length > self.max_bytes:
                raise FrameError(f"message of {length} bytes exceeds lsp.max_message_bytes")
            start = end + 4
            if len(self.buffer) < start + length:
                return messages
            messages.append(bytes(self.buffer[start : start + length]))
            del self.buffer[: start + length]


# --------------------------------------------------------------------------- documents


@dataclass
class Document:
    client_uri: str
    server_uri: str
    path: Path
    header: str = ""  # task bodies: the wrapper placed before the (indented) body

    @property
    def header_lines(self) -> int:
        return self.header.count("\n")

    @property
    def wrapped(self) -> bool:
        return bool(self.header)


def _task_header(query: dict[str, list[str]]) -> str:
    from autoair.lsp import wrapper_header

    args = [a for a in ",".join(query.get("args", [])).split(",") if a]
    loop = (query.get("loop") or ["0"])[0] in ("1", "true")
    fn = (query.get("fn") or ["task_fn"])[0]
    return wrapper_header(args, loop, fn, context_module=CONTEXT_STUB)


def indent_body(text: str) -> str:
    return textwrap.indent(text, _INDENT, lambda _line: True) if text else ""


def dedent_text(text: str) -> str:
    """Server text spanning lines of a wrapped body (edits, completions) → the user's text."""
    return text.replace("\n" + _INDENT, "\n")


class DocumentMapper:
    """Virtual client URIs ↔ workspace files, and position translation of task bodies."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.root_uri = workspace.as_uri()
        self.by_client: dict[str, Document] = {}
        self.by_server: dict[str, Document] = {}
        self._names = itertools.count(1)

    # ---- documents
    def document(self, client_uri: str) -> Document | None:
        if not client_uri.startswith(CLIENT_ROOT):
            return None
        known = self.by_client.get(client_uri)
        if known:
            return known
        parts = urlsplit(client_uri)
        name = unquote(parts.path.rsplit("/", 1)[-1]) or "document"
        stem, dot, suffix = name.rpartition(".")
        if not dot:
            stem, suffix = name, "txt"
        stem = _SAFE_NAME.sub("_", stem)[:80] or "document"
        suffix = _SAFE_NAME.sub("", suffix)[:8] or "txt"
        path = self.workspace / f"{stem}_{next(self._names)}.{suffix}"
        query = parse_qs(parts.query)
        header = ""
        if (query.get("mode") or [""])[0] == "task_body":
            header = _task_header(query)
        doc = Document(client_uri, path.as_uri(), path, header)
        self.by_client[client_uri] = doc
        self.by_server[doc.server_uri] = doc
        return doc

    def full_text(self, doc: Document, text: str) -> str:
        return doc.header + indent_body(text) if doc.wrapped else text

    def change_to_server(self, doc: Document, change: dict[str, Any]) -> dict[str, Any]:
        """A ``didChange`` content change of ``doc`` in the server's coordinates."""
        if not doc.wrapped:
            return change
        text = str(change.get("text", ""))
        if "range" not in change:  # full document sync
            return {"text": self.full_text(doc, text)}
        return {
            **change,
            "range": self.to_server(change["range"], doc),
            "text": text.replace("\n", "\n" + _INDENT),
        }

    # ---- generic walk
    def _uri_out(self, value: str) -> str:
        doc = self.by_server.get(value)
        return doc.client_uri if doc else value

    def _uri_in(self, value: str) -> str:
        doc = self.document(value)
        return doc.server_uri if doc else value

    @staticmethod
    def _is_position(value: dict[str, Any]) -> bool:
        return (
            len(value) == 2
            and isinstance(value.get("line"), int)
            and isinstance(value.get("character"), int)
        )

    def _context(
        self, value: dict[str, Any], current: Document | None, out: bool
    ) -> Document | None:
        uri: Any = None
        for key in ("uri", "targetUri"):
            if isinstance(value.get(key), str):
                uri = value[key]
        text_doc = value.get("textDocument")
        if isinstance(text_doc, dict) and isinstance(text_doc.get("uri"), str):
            uri = text_doc["uri"]
        if uri is None:
            return current
        return (self.by_server.get(uri) if out else self.document(uri)) or None

    def to_server(self, value: Any, doc: Document | None = None) -> Any:
        """Client → server: URIs mapped, body positions shifted into the wrapper."""
        if isinstance(value, list):
            return [self.to_server(v, doc) for v in value]
        if not isinstance(value, dict):
            return value
        if self._is_position(value):
            if doc and doc.wrapped:
                return {
                    "line": value["line"] + doc.header_lines,
                    "character": value["character"] + len(_INDENT),
                }
            return value
        doc = self._context(value, doc, out=False)
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key in ("uri", "targetUri") and isinstance(item, str):
                out[key] = self._uri_in(item)
            elif isinstance(item, str) and key == "newText" and doc and doc.wrapped:
                out[key] = item.replace("\n", "\n" + _INDENT)
            else:
                out[key] = self.to_server(item, doc)
        return out

    def to_client(self, value: Any, doc: Document | None = None) -> Any:
        """Server → client: URIs mapped back, wrapper positions shifted into the body."""
        if isinstance(value, list):
            return [self.to_client(v, doc) for v in value]
        if not isinstance(value, dict):
            return value
        if self._is_position(value):
            if doc and doc.wrapped:
                return {
                    "line": max(0, value["line"] - doc.header_lines),
                    "character": max(0, value["character"] - len(_INDENT)),
                }
            return value
        doc = self._context(value, doc, out=True)
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key in ("uri", "targetUri") and isinstance(item, str):
                out[key] = self._uri_out(item)
            elif key == "changes" and isinstance(item, dict):  # WorkspaceEdit.changes
                out[key] = {
                    self._uri_out(uri): self.to_client(edits, self.by_server.get(uri))
                    for uri, edits in item.items()
                }
            elif isinstance(item, str) and key in ("newText", "insertText") and doc and doc.wrapped:
                out[key] = dedent_text(item)
            else:
                out[key] = self.to_client(item, doc)
        return out

    def in_header(self, rng: Any, doc: Document) -> bool:
        try:
            return bool(doc.wrapped and int(rng["start"]["line"]) < doc.header_lines)
        except (KeyError, TypeError, ValueError):
            return False


# --------------------------------------------------------------------------- read-only peek


def _read_roots() -> list[Path]:
    roots = {Path(sys.prefix), Path(sys.base_prefix), Path(sys.exec_prefix)}
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        with contextlib.suppress(KeyError):
            roots.add(Path(sysconfig.get_paths()[key]))
    return [r.resolve() for r in roots if r.exists()]


def read_library_file(uri: str, workspace: Path) -> str | None:
    """Source of a library file for definition peeks: ``.py``/``.pyi`` below the Python
    installation or the session's workspace, nothing else (no arbitrary file reads)."""
    if not uri.startswith("file://"):
        return None
    path = Path(unquote(urlsplit(uri).path))
    if path.suffix not in _READ_SUFFIXES:
        return None
    try:
        real = path.resolve(strict=True)
    except OSError:
        return None
    roots = [*_read_roots(), workspace.resolve()]
    if not any(real.is_relative_to(root) for root in roots):
        return None
    try:
        if real.stat().st_size > _READ_MAX:
            return None
        return real.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


# --------------------------------------------------------------------------- sessions


def _limits(settings: LspSettings, node: bool) -> Callable[[], None] | None:
    if os.name != "posix":
        return None
    memory = settings.memory_limit_mb
    cpu = settings.cpu_limit_seconds

    def apply() -> None:  # runs in the child between fork and exec
        import resource

        if cpu:
            with contextlib.suppress(ValueError, OSError):
                resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 5))
        if memory and not node:
            limit = memory * 1024 * 1024
            with contextlib.suppress(ValueError, OSError):
                resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

    return apply


def _server_env(settings: LspSettings, node: bool) -> dict[str, str]:
    extra: dict[str, str] = {"PYTHONDONTWRITEBYTECODE": "1"}
    if node and settings.memory_limit_mb:
        options = os.environ.get("NODE_OPTIONS", "")
        extra["NODE_OPTIONS"] = f"{options} --max-old-space-size={settings.memory_limit_mb}"
    return user_code_env(extra)


def _workspace_files(workspace: Path, language: Language) -> None:
    if language != "python":
        return
    from autoair.lsp import airflow_context

    (workspace / f"{CONTEXT_STUB}.py").write_text(
        Path(airflow_context.__file__).read_text(encoding="utf-8"), encoding="utf-8"
    )
    config = {
        "typeCheckingMode": "standard",
        "reportWildcardImportFromLibrary": False,
        "reportMissingModuleSource": False,
    }
    (workspace / "pyrightconfig.json").write_text(json.dumps(config), encoding="utf-8")


def _configuration(item: dict[str, Any]) -> Any:
    section = str(item.get("section") or "")
    if section == "python":
        return {"pythonPath": sys.executable}
    if section in ("basedpyright", "python.analysis", "basedpyright.analysis"):
        analysis = {"diagnosticMode": "openFilesOnly", "autoImportCompletions": True}
        return analysis if section.endswith("analysis") else {"analysis": analysis}
    if section == "pylsp":
        return {"plugins": {"jedi": {"environment": sys.executable}}}
    return None


Send = Callable[[str], Awaitable[None]]
Receive = Callable[[], Awaitable[str | None]]


@dataclass
class Session:
    id: int
    user: str
    language: Language
    started: float = field(default_factory=time.monotonic)


class LspSessionManager:
    """Runs and bounds language-server sessions (one per WebSocket)."""

    def __init__(self, settings: LspSettings) -> None:
        self.settings = settings
        self.sessions: dict[int, Session] = {}
        self._ids = itertools.count(1)

    def _reserve(self, user: str, language: Language) -> Session:
        s = self.settings
        if len(self.sessions) >= s.max_sessions:
            raise LspError("too many language-server sessions (lsp.max_sessions)", 1013)
        if sum(1 for x in self.sessions.values() if x.user == user) >= s.max_sessions_per_user:
            raise LspError(
                "too many language-server sessions for this user (lsp.max_sessions_per_user)",
                1013,
            )
        session = Session(next(self._ids), user, language)
        self.sessions[session.id] = session
        return session

    async def run(self, language: str, user: str, receive: Receive, send: Send) -> None:
        """Serve one client until it disconnects, idles out or the server exits.

        Raises :class:`LspError` before anything was started (the caller closes the socket
        with its code)."""
        if language not in LANGUAGES:
            raise LspError(f"unknown language {language!r}", 1008)
        lang: Language = language
        command = server_command(self.settings, lang)
        if command is None:
            raise LspError(f"{server_name(self.settings, lang)} is not installed", 1011)
        session = self._reserve(user, lang)
        workspace = Path(tempfile.mkdtemp(prefix="autoair-lsp-"))
        process: asyncio.subprocess.Process | None = None
        try:
            _workspace_files(workspace, lang)
            node = uses_node(self.settings, lang)
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=workspace,
                env=_server_env(self.settings, node),
                preexec_fn=_limits(self.settings, node),
                start_new_session=True,
            )
            bridge = _Bridge(self.settings, workspace, process, send)
            await bridge.serve(receive)
        finally:
            # Synchronous part first: it also runs when the handler is being cancelled.
            self.sessions.pop(session.id, None)
            if process is not None:
                _signal(process, signal.SIGTERM)
            shutil.rmtree(workspace, ignore_errors=True)
            if process is not None:
                await _reap(process)


def _signal(process: asyncio.subprocess.Process, sig: int) -> None:
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, sig)


async def _reap(process: asyncio.subprocess.Process) -> None:
    """Wait for a terminated server; kill its process group when it lingers (or when this
    task is cancelled while waiting)."""
    try:
        await asyncio.wait_for(process.wait(), timeout=2)
    except asyncio.TimeoutError:
        _signal(process, signal.SIGKILL)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=2)
    except asyncio.CancelledError:
        _signal(process, signal.SIGKILL)
        raise


class _Closed(Exception):
    pass


class _Bridge:
    """Pumps messages both ways for one session, translating documents."""

    def __init__(
        self,
        settings: LspSettings,
        workspace: Path,
        process: asyncio.subprocess.Process,
        send: Send,
    ) -> None:
        self.settings = settings
        self.workspace = workspace
        self.process = process
        self.send_client = send
        self.mapper = DocumentMapper(workspace)
        self.requests: dict[Any, Any] = {}  # client request id -> Document | None
        self.last_activity = time.monotonic()
        self.waiting_since: float | None = None  # first client message not yet answered

    # ---- I/O
    async def _to_server(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        body = json.dumps(message, separators=(",", ":")).encode()
        self.process.stdin.write(frame(body))
        await self.process.stdin.drain()

    async def _to_client(self, message: dict[str, Any]) -> None:
        await self.send_client(json.dumps(message, separators=(",", ":")))

    # ---- client → server
    async def from_client(self, raw: str) -> None:
        if len(raw.encode()) > self.settings.max_message_bytes:
            raise _Closed("message exceeds lsp.max_message_bytes")
        try:
            message = json.loads(raw)
        except ValueError:
            return
        if not isinstance(message, dict):
            return
        self.last_activity = time.monotonic()
        method = message.get("method")
        params = message.get("params")
        if self.waiting_since is None and method == "initialize":
            self.waiting_since = self.last_activity
        if method == "autoair/readFile" and "id" in message:
            uri = params.get("uri") if isinstance(params, dict) else None
            text = read_library_file(self._server_uri(uri), self.workspace) if uri else None
            await self._to_client({"jsonrpc": "2.0", "id": message["id"], "result": text})
            return
        if method == "initialize" and isinstance(params, dict):
            params = {
                **params,
                "rootUri": self.mapper.root_uri,
                "rootPath": str(self.workspace),
                "workspaceFolders": [{"uri": self.mapper.root_uri, "name": "autoair"}],
                "processId": os.getpid(),
            }
        elif method == "textDocument/didOpen" and isinstance(params, dict):
            item = params.get("textDocument") or {}
            doc = self.mapper.document(str(item.get("uri", "")))
            if doc is not None:
                text = self.mapper.full_text(doc, str(item.get("text", "")))
                with contextlib.suppress(OSError):
                    doc.path.write_text(text, encoding="utf-8")
                params = {**params, "textDocument": {**item, "uri": doc.server_uri, "text": text}}
                await self._to_server({**message, "params": params})
                return
        elif method == "textDocument/didChange" and isinstance(params, dict):
            item = params.get("textDocument") or {}
            doc = self.mapper.document(str(item.get("uri", "")))
            if doc is not None:
                changes = [
                    self.mapper.change_to_server(doc, c)
                    for c in params.get("contentChanges") or []
                    if isinstance(c, dict)
                ]
                params = {
                    **params,
                    "textDocument": {**item, "uri": doc.server_uri},
                    "contentChanges": changes,
                }
                await self._to_server({**message, "params": params})
                return
        doc = self._document_of(params)
        if "id" in message and method:
            self.requests[message["id"]] = doc
        mapped = self.mapper.to_server(params, doc) if params is not None else None
        out = {**message}
        if params is not None:
            out["params"] = mapped
        await self._to_server(out)

    def _server_uri(self, uri: Any) -> str:
        if not isinstance(uri, str):
            return ""
        doc = self.mapper.by_client.get(uri)
        return doc.server_uri if doc else uri

    def _document_of(self, params: Any) -> Document | None:
        if not isinstance(params, dict):
            return None
        text_doc = params.get("textDocument")
        uri = text_doc.get("uri") if isinstance(text_doc, dict) else params.get("uri")
        return self.mapper.by_client.get(uri) if isinstance(uri, str) else None

    # ---- server → client
    async def from_server(self, body: bytes) -> None:
        try:
            message = json.loads(body)
        except ValueError:
            return
        if not isinstance(message, dict):
            return
        self.waiting_since = None
        method = message.get("method")
        if method and "id" in message:  # a request from the server
            if method == "workspace/configuration":
                items = (message.get("params") or {}).get("items") or []
                result = [_configuration(i) if isinstance(i, dict) else None for i in items]
                await self._to_server({"jsonrpc": "2.0", "id": message["id"], "result": result})
                return
            if method in ("window/workDoneProgress/create", "client/registerCapability"):
                await self._to_server({"jsonrpc": "2.0", "id": message["id"], "result": None})
                return
            await self._to_client(self.mapper.to_client(message))
            return
        if method == "textDocument/publishDiagnostics":
            params = message.get("params") or {}
            doc = self.mapper.by_server.get(str(params.get("uri", "")))
            if doc is None:
                return  # the context stub or a library file: not the user's document
            diagnostics = [
                d
                for d in params.get("diagnostics") or []
                if not (isinstance(d, dict) and self.mapper.in_header(d.get("range"), doc))
            ]
            message = {**message, "params": {**params, "diagnostics": diagnostics}}
            await self._to_client(self.mapper.to_client(message))
            return
        if "id" in message and not method:  # a response
            doc = self.requests.pop(message.get("id"), None)
            await self._to_client(self.mapper.to_client(message, doc))
            return
        await self._to_client(self.mapper.to_client(message))

    # ---- loops
    async def serve(self, receive: Receive) -> None:
        tasks = [
            asyncio.ensure_future(self._client_loop(receive)),
            asyncio.ensure_future(self._server_loop()),
            asyncio.ensure_future(self._watchdog()),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                exc = task.exception()
                if exc and not isinstance(exc, _Closed):
                    log.debug("language-server session ended: %s", exc)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _client_loop(self, receive: Receive) -> None:
        while True:
            raw = await receive()
            if raw is None:
                return
            await self.from_client(raw)

    async def _server_loop(self) -> None:
        assert self.process.stdout is not None
        framer = Framer(self.settings.max_message_bytes)
        while True:
            data = await self.process.stdout.read(65536)
            if not data:
                return
            for body in framer.feed(data):
                await self.from_server(body)

    async def _watchdog(self) -> None:
        while True:
            await asyncio.sleep(min(_WATCH_INTERVAL, self.settings.idle_timeout))
            now = time.monotonic()
            if now - self.last_activity > self.settings.idle_timeout:
                raise _Closed("idle")
            if (
                self.waiting_since is not None
                and now - self.waiting_since > self.settings.start_timeout
            ):
                raise _Closed("the language server did not answer (lsp.start_timeout)")


def client_document_uri(name: str, **query: str) -> str:
    """A virtual document URI (tests, docs): ``client_document_uri("t.py", mode="task_body")``."""
    params = "&".join(f"{k}={quote(v)}" for k, v in query.items())
    return f"{CLIENT_ROOT}{quote(name)}" + (f"?{params}" if params else "")


__all__ = [
    "CLIENT_ROOT",
    "DocumentMapper",
    "FrameError",
    "Framer",
    "LspError",
    "LspSessionManager",
    "client_document_uri",
    "frame",
    "read_library_file",
    "resolve_binary",
    "server_command",
    "server_status",
]
