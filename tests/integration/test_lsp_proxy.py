"""Language-server proxy (features.ide_language_server): framing, document mapping, the
WebSocket endpoint with a fake language server, limits and process cleanup."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from autoair.api.app import build_services, create_app
from autoair.auth import AuthBackend
from autoair.lsp import complete, wrapper_header
from autoair.lsp.config import LspSettings
from autoair.lsp.proxy import (
    DocumentMapper,
    FrameError,
    Framer,
    client_document_uri,
    frame,
    read_library_file,
    resolve_binary,
    server_command,
)
from autoair.settings import Settings

# A minimal stdio language server: answers initialize, publishes a diagnostic per opened
# document (one in the wrapper header, one on the body's first character), echoes hover
# positions back, reports its pid and asks the client for its configuration.
FAKE_SERVER = r"""
import json, os, sys

docs = {}
config = None

def read():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            sys.exit(0)
        line = line.strip()
        if not line:
            break
        k, _, v = line.partition(b":")
        headers[k.strip().lower()] = v.strip()
    return json.loads(sys.stdin.buffer.read(int(headers[b"content-length"])))

def send(msg):
    body = json.dumps(msg).encode()
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    sys.stdout.buffer.flush()

while True:
    msg = read()
    method = msg.get("method")
    params = msg.get("params") or {}
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": "cfg", "method": "workspace/configuration",
              "params": {"items": [{"section": "python"}]}})
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "capabilities": {"hoverProvider": True, "textDocumentSync": 2},
            "rootUri": params.get("rootUri")}})
    elif msg.get("id") == "cfg":
        config = msg.get("result")
    elif method == "textDocument/didOpen":
        doc = params["textDocument"]
        docs[doc["uri"]] = doc["text"]
        send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": {
            "uri": doc["uri"], "diagnostics": [
                {"range": {"start": {"line": 0, "character": 0},
                           "end": {"line": 0, "character": 3}}, "message": "header"},
                {"range": {"start": {"line": 3, "character": 4},
                           "end": {"line": 3, "character": 6}}, "message": "body"},
            ]}})
    elif method == "textDocument/didChange":
        uri = params["textDocument"]["uri"]
        for change in params["contentChanges"]:
            if "range" not in change:
                docs[uri] = change["text"]
            else:
                lines = docs[uri].split("\n")
                s, e = change["range"]["start"], change["range"]["end"]
                head = "\n".join(lines[: s["line"]] + [lines[s["line"]][: s["character"]]])
                tail = "\n".join([lines[e["line"]][e["character"]:]] + lines[e["line"] + 1:])
                docs[uri] = head + change["text"] + tail
    elif method == "textDocument/hover":
        uri = params["textDocument"]["uri"]
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "contents": {"kind": "plaintext", "value": json.dumps({
                "position": params["position"], "text": docs.get(uri), "config": config,
                "uri": uri})},
            "range": {"start": params["position"], "end": params["position"]}}})
    elif method == "textDocument/definition":
        uri = params["textDocument"]["uri"]
        send({"jsonrpc": "2.0", "id": msg["id"], "result": [
            {"uri": uri, "range": {"start": {"line": 4, "character": 4},
                                   "end": {"line": 4, "character": 5}}},
            {"uri": "file:///usr/lib/python3/json/__init__.py",
             "range": {"start": {"line": 4, "character": 4},
                       "end": {"line": 4, "character": 5}}},
        ]})
    elif method == "test/pid":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": os.getpid()})
    elif method == "shutdown":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": None})
"""


# --------------------------------------------------------------------------- framing


def test_framer_decodes_split_and_batched_messages():
    one, two = b'{"a":1}', b'{"b":"\xc3\xa9"}'
    data = frame(one) + frame(two)
    framer = Framer(1024)
    out: list[bytes] = []
    for i in range(len(data)):  # byte by byte
        out += framer.feed(data[i : i + 1])
    assert out == [one, two]
    assert Framer(1024).feed(data) == [one, two]


def test_framer_rejects_oversized_and_headerless_messages():
    with pytest.raises(FrameError):
        Framer(10).feed(frame(b"x" * 11))
    with pytest.raises(FrameError):
        Framer(10).feed(b"Content-Type: x\r\n\r\n{}")


# --------------------------------------------------------------------------- documents


def test_task_body_positions_edits_and_diagnostics_are_translated(tmp_path):
    mapper = DocumentMapper(tmp_path)
    uri = client_document_uri("extract.py", mode="task_body", args="ti,limit", loop="1")
    doc = mapper.document(uri)
    assert doc is not None
    assert doc.wrapped
    assert doc.path.parent == tmp_path
    assert mapper.document(uri) is doc  # stable
    header = doc.header
    assert header.splitlines()[-1] == "def task_fn(limit, *, ti=_autoair_ctx.ti, loop=None):"
    assert mapper.full_text(doc, "x = 1\nreturn x") == header + "    x = 1\n    return x"

    h = doc.header_lines
    request = {"textDocument": {"uri": uri}, "position": {"line": 1, "character": 2}}
    mapped = mapper.to_server(request)
    assert mapped["textDocument"]["uri"] == doc.server_uri
    assert mapped["position"] == {"line": 1 + h, "character": 6}

    change = mapper.change_to_server(
        doc,
        {
            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}},
            "text": "a\nb",
        },
    )
    assert change["range"]["start"] == {"line": h, "character": 4}
    assert change["text"] == "a\n    b"
    assert mapper.change_to_server(doc, {"text": "y"}) == {"text": header + "    y"}

    edit = {
        "changes": {
            doc.server_uri: [
                {
                    "range": {
                        "start": {"line": h + 2, "character": 8},
                        "end": {"line": h + 2, "character": 9},
                    },
                    "newText": "if a:\n        b",
                }
            ]
        }
    }
    back = mapper.to_client(edit)
    (edits,) = back["changes"].values()
    assert next(iter(back["changes"])) == uri
    assert edits[0]["range"]["start"] == {"line": 2, "character": 4}
    assert edits[0]["newText"] == "if a:\n    b"
    assert mapper.in_header({"start": {"line": 0, "character": 0}}, doc)
    assert not mapper.in_header({"start": {"line": h, "character": 0}}, doc)


def test_module_documents_and_other_files_are_not_shifted(tmp_path):
    mapper = DocumentMapper(tmp_path)
    uri = client_document_uri("script.py")
    doc = mapper.document(uri)
    assert doc is not None
    assert not doc.wrapped
    pos = {"textDocument": {"uri": uri}, "position": {"line": 3, "character": 1}}
    assert mapper.to_server(pos)["position"] == {"line": 3, "character": 1}
    lib = {"uri": "file:///usr/lib/x.py", "range": {"start": {"line": 1, "character": 1}}}
    assert mapper.to_client(lib) == lib
    assert mapper.document("file:///etc/passwd") is None


def test_library_files_can_be_peeked_but_nothing_else(tmp_path):
    assert read_library_file(Path(json.__file__).as_uri(), tmp_path)
    secret = tmp_path.parent / "secret.py"
    secret.write_text("TOKEN = 1")
    assert read_library_file(secret.as_uri(), tmp_path) is None
    (tmp_path / "doc.py").write_text("x = 1")
    assert read_library_file((tmp_path / "doc.py").as_uri(), tmp_path) == "x = 1"
    assert read_library_file("file:///etc/passwd", tmp_path) is None


def test_wrapper_header_orders_parameters_and_types_context():
    header = wrapper_header(["a", "**context", "ds", "bad name"], True, "fn")
    assert header.splitlines()[-1] == "def fn(a, *, ds=_autoair_ctx.ds, loop=None, **context):"


@pytest.mark.airflow
def test_light_completion_ranks_keywords_and_knows_the_context():
    from autoair.lsp import PythonContext

    def labels(code: str, line: int, column: int, **kw: Any) -> list[str]:
        ctx = PythonContext(code=code, line=line, column=column, **kw)
        return [i.label for i in complete(ctx, 100).items]

    assert labels("ret", 1, 3)[0] == "return"
    assert labels("r", 1, 1).index("return") < 5
    assert labels("import js", 1, 9)[0] == "json"
    assert labels("import json\njson.du", 2, 7)[:2] == ["dump", "dumps"]
    assert "xcom_pull" in labels("ti.xcom_p", 1, 9)
    assert "xcom_pull" in labels("ti.xcom_p", 1, 9, arguments=["ti"])
    assert "ds" in labels("d", 1, 1)
    assert "conf" in labels("dag_run.co", 1, 10)
    assert "get" in labels("params.ge", 1, 9)
    # A pasted body keeps its indentation: the cursor column follows the dedent.
    assert labels("    import json\n    json.du", 2, 11)[:2] == ["dump", "dumps"]
    assert "_autoair_ctx" not in labels("_auto", 1, 5)


# --------------------------------------------------------------------------- endpoint


def _client(tmp_path: Path, **lsp: Any) -> TestClient:
    fake = tmp_path / "fake_lsp.py"
    fake.write_text(FAKE_SERVER)
    features = {"ide_language_server": lsp.pop("enabled", True)}
    settings = Settings(  # type: ignore[call-arg]
        dags_folder=tmp_path / "dags",
        subfolder="",
        ai={"enabled": False},
        features=features,
        lsp=LspSettings(
            python_server="pylsp",
            **{
                "pylsp_command": [sys.executable, str(fake)],
                "bash_command": [sys.executable, str(fake)],
                **lsp,
            },
        ),
    )
    return TestClient(create_app(build_services(settings, AuthBackend())))


def _ticket(c: TestClient, language: str = "python") -> str:
    r = c.post("/api/lsp/session", json={"language": language})
    assert r.status_code == 200, r.text
    assert r.json()["path"] == f"/lsp/ws/{language}"
    return str(r.json()["ticket"])


def _call(ws: Any, msg_id: int, method: str, params: Any) -> Any:
    ws.send_text(json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}))
    while True:
        reply = json.loads(ws.receive_text())
        if reply.get("id") == msg_id and "method" not in reply:
            return reply["result"]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie is gone for our purposes
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().split()[2] != "Z"
    except OSError:
        return True


def test_feature_off_hides_the_routes(tmp_path):
    c = _client(tmp_path, enabled=False)
    assert c.post("/api/lsp/session", json={"language": "python"}).status_code == 404
    assert c.get("/api/lsp/servers").status_code == 404
    with (
        pytest.raises(WebSocketDenialResponse) as denied,
        c.websocket_connect("/api/lsp/ws/python?ticket=x"),
    ):
        pass
    assert denied.value.status_code == 404


def test_socket_needs_a_ticket_for_its_language(tmp_path):
    c = _client(tmp_path)
    for url in ("/api/lsp/ws/python", "/api/lsp/ws/python?ticket=forged.sig"):
        with pytest.raises(WebSocketDenialResponse) as denied, c.websocket_connect(url):
            pass
        assert denied.value.status_code == 403
    bash_ticket = _ticket(c, "bash")
    with (
        pytest.raises(WebSocketDenialResponse),
        c.websocket_connect(f"/api/lsp/ws/python?ticket={bash_ticket}"),
    ):
        pass


def test_ticket_opens_one_socket_on_any_replica(tmp_path):
    """A ticket is single use within its TTL, across replicas sharing the state folder
    (a ticket that leaked through logs or a proxy cannot be replayed)."""
    c = _client(tmp_path)
    ticket = _ticket(c, "bash")
    with c.websocket_connect(f"/api/lsp/ws/bash?ticket={ticket}") as ws:
        _call(ws, 1, "initialize", {"capabilities": {}})
    with (
        pytest.raises(WebSocketDenialResponse) as denied,
        c.websocket_connect(f"/api/lsp/ws/bash?ticket={ticket}"),
    ):
        pass
    assert denied.value.status_code == 403
    # another replica (same state folder and ticket key) refuses a ticket used here
    other = _client(tmp_path)
    with other.websocket_connect(f"/api/lsp/ws/bash?ticket={_ticket(c, 'bash')}") as ws:
        _call(ws, 1, "initialize", {"capabilities": {}})  # tickets validate on every replica
    second = _ticket(c, "bash")
    with c.websocket_connect(f"/api/lsp/ws/bash?ticket={second}") as ws:
        _call(ws, 1, "initialize", {"capabilities": {}})
    with (
        pytest.raises(WebSocketDenialResponse),
        other.websocket_connect(f"/api/lsp/ws/bash?ticket={second}"),
    ):
        pass


def test_servers_report_availability(tmp_path):
    c = _client(tmp_path)
    body = c.get("/api/lsp/servers").json()
    assert body["python"] == {"server": "pylsp", "available": True}
    assert body["bash"]["available"] is True
    missing = LspSettings(pylsp_command=["autoair-no-such-server"], python_server="pylsp")
    assert server_command(missing, "python") is None
    assert resolve_binary("autoair-no-such-server") is None


def test_session_translates_documents_and_kills_the_server(tmp_path):
    c = _client(tmp_path)
    uri = client_document_uri("t.py", mode="task_body", args="ti")
    with c.websocket_connect(f"/api/lsp/ws/python?ticket={_ticket(c)}") as ws:
        init = _call(ws, 1, "initialize", {"rootUri": "file:///elsewhere", "capabilities": {}})
        root = init["rootUri"]
        assert root.startswith("file://")
        assert "autoair-lsp-" in root
        workspace = Path(root[len("file://") :])
        assert (workspace / "_autoair_context.py").is_file()
        ws.send_text(json.dumps({"jsonrpc": "2.0", "method": "initialized", "params": {}}))
        ws.send_text(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "textDocument/didOpen",
                    "params": {
                        "textDocument": {
                            "uri": uri,
                            "languageId": "python",
                            "version": 1,
                            "text": "x = 1\nreturn ti",
                        }
                    },
                }
            )
        )
        diagnostics = json.loads(ws.receive_text())
        assert diagnostics["method"] == "textDocument/publishDiagnostics"
        assert diagnostics["params"]["uri"] == uri
        (diag,) = diagnostics["params"]["diagnostics"]  # the header one is dropped
        assert diag["message"] == "body"
        assert diag["range"]["start"] == {"line": 0, "character": 0}

        ws.send_text(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "textDocument/didChange",
                    "params": {
                        "textDocument": {"uri": uri, "version": 2},
                        "contentChanges": [
                            {
                                "range": {
                                    "start": {"line": 1, "character": 0},
                                    "end": {"line": 1, "character": 0},
                                },
                                "text": "y = 2\n",
                            }
                        ],
                    },
                }
            )
        )
        pos = {"textDocument": {"uri": uri}, "position": {"line": 2, "character": 7}}
        hover = _call(ws, 2, "textDocument/hover", pos)
        seen = json.loads(hover["contents"]["value"])
        header_lines = 3
        assert seen["position"] == {"line": 2 + header_lines, "character": 11}
        assert seen["text"].endswith("    x = 1\n    y = 2\n    return ti")
        assert seen["config"] == [{"pythonPath": sys.executable}]
        assert hover["range"]["start"] == {"line": 2, "character": 7}

        definition = _call(ws, 3, "textDocument/definition", pos)
        assert definition[0]["uri"] == uri
        assert definition[0]["range"]["start"] == {"line": 1, "character": 0}
        assert definition[1]["range"]["start"] == {"line": 4, "character": 4}

        peek = _call(ws, 4, "autoair/readFile", {"uri": Path(json.__file__).as_uri()})
        assert "JSON" in peek
        assert _call(ws, 5, "autoair/readFile", {"uri": "file:///etc/passwd"}) is None
        pid = _call(ws, 6, "test/pid", {})
        assert _alive(pid)
    deadline = time.monotonic() + 10
    while (_alive(pid) or workspace.exists()) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(pid)
    assert not workspace.exists()


def test_session_limits_and_idle_timeout(tmp_path):
    c = _client(tmp_path, max_sessions=1, idle_timeout=0.5)
    with c.websocket_connect(f"/api/lsp/ws/bash?ticket={_ticket(c, 'bash')}") as first:
        _call(first, 1, "initialize", {"capabilities": {}})
        with c.websocket_connect(f"/api/lsp/ws/bash?ticket={_ticket(c, 'bash')}") as second:
            with pytest.raises(WebSocketDisconnect) as closed:
                second.receive_text()
            assert closed.value.code == 1013
        # No message for longer than idle_timeout: the proxy ends the session.
        with pytest.raises(WebSocketDisconnect):
            first.receive_text()


def test_missing_server_closes_the_socket(tmp_path):
    c = _client(tmp_path, bash_command=["autoair-no-such-server"])
    url = f"/api/lsp/ws/bash?ticket={_ticket(c, 'bash')}"
    with c.websocket_connect(url) as ws, pytest.raises(WebSocketDisconnect) as closed:
        ws.receive_text()
    assert closed.value.code == 1011


@pytest.mark.skipif(
    server_command(LspSettings(), "python") is None, reason="basedpyright not installed"
)
# TestClient closes its event loop while the killed server's transport is collected.
@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
def test_basedpyright_completes_a_task_body(tmp_path):
    settings = Settings(  # type: ignore[call-arg]
        dags_folder=tmp_path / "dags",
        subfolder="",
        ai={"enabled": False},
        features={"ide_language_server": True},
    )
    c = TestClient(create_app(build_services(settings, AuthBackend())))
    uri = client_document_uri("t.py", mode="task_body", args="ti")
    with c.websocket_connect(f"/api/lsp/ws/python?ticket={_ticket(c)}") as ws:
        _call(ws, 1, "initialize", {"capabilities": {}})
        ws.send_text(json.dumps({"jsonrpc": "2.0", "method": "initialized", "params": {}}))
        doc = {"uri": uri, "languageId": "python", "version": 1, "text": "ti.xcom_p"}
        opened = {"textDocument": doc}
        ws.send_text(
            json.dumps({"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": opened})
        )
        pos = {"textDocument": {"uri": uri}, "position": {"line": 0, "character": 9}}
        result = _call(ws, 2, "textDocument/completion", pos)
        items = result["items"] if isinstance(result, dict) else result
        assert "xcom_pull" in {i["label"] for i in items}
