"""A real git smart-HTTP server for tests: ``git http-backend`` behind a token check.

Bare repositories under ``root`` are served at ``http://127.0.0.1:<port>/<name>``; every
request must carry ``Authorization: Basic base64(<user>:<token>)`` (otherwise 401, as a
git host refusing the credentials). The headers seen are kept in :attr:`seen`, with their
paths in :attr:`requests` (pushes go to ``git-receive-pack``).
"""

from __future__ import annotations

import base64
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class GitHttpServer:
    def __init__(self, root: Path, token: str, user: str = "x-access-token") -> None:
        self.root = root
        self.expected = "Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()
        self.seen: list[str | None] = []
        self.paths: list[str] = []
        self.requests: list[tuple[str, str | None]] = []  # (path, Authorization)
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def _handle(self) -> None:
                auth = self.headers.get("Authorization")
                server.seen.append(auth)
                server.paths.append(self.path)
                server.requests.append((self.path, auth))
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                if auth != server.expected:
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="git"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                path, _, query = self.path.partition("?")
                env = {
                    "PATH": os.environ.get("PATH", ""),
                    "GIT_PROJECT_ROOT": str(server.root),
                    "GIT_HTTP_EXPORT_ALL": "1",
                    "PATH_INFO": path,
                    "QUERY_STRING": query,
                    "REQUEST_METHOD": self.command,
                    "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                    "CONTENT_LENGTH": str(len(body)),
                    "REMOTE_USER": "tester",
                    "REMOTE_ADDR": "127.0.0.1",
                    "HTTP_CONTENT_ENCODING": self.headers.get("Content-Encoding", ""),
                    "GIT_PROTOCOL": self.headers.get("Git-Protocol", ""),
                }
                out = subprocess.run(
                    ["git", "http-backend"], input=body, env=env, capture_output=True, check=False
                ).stdout
                sep = b"\r\n\r\n" if b"\r\n\r\n" in out else b"\n\n"
                head, _, payload = out.partition(sep)
                status = 200
                headers: list[tuple[str, str]] = []
                for line in head.decode().splitlines():
                    name, _, value = line.partition(":")
                    if name.lower() == "status":
                        status = int(value.strip().split()[0])
                    elif name:
                        headers.append((name, value.strip()))
                self.send_response(status)
                for name, value in headers:
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = _handle
            do_POST = _handle

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def url(self, name: str) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/{name}"

    def __enter__(self) -> GitHttpServer:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def bare_repo(root: Path, name: str = "dags.git") -> Path:
    """A bare repository with one commit on ``main`` (and ``dev``)."""
    bare = root / name
    bare.parent.mkdir(parents=True, exist_ok=True)

    def git(*args: str, cwd: Path | None = None) -> None:
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    git("init", "--bare", "-q", "-b", "main", str(bare))
    work = root / f".seed-{name.replace('/', '_')}"
    git("clone", "-q", str(bare), str(work))
    (work / "README.md").write_text("seed\n")
    git("-C", str(work), "add", ".")
    git("-C", str(work), "-c", "user.name=S", "-c", "user.email=s@x", "commit", "-qm", "seed")
    git("-C", str(work), "push", "-q", "origin", "HEAD:main")
    git("-C", str(work), "push", "-q", "origin", "HEAD:dev")
    return bare
