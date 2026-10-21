from __future__ import annotations

import functools
import hashlib
import http.server
import json
import threading
from pathlib import Path

import pytest
from typer.testing import CliRunner

from autoair.cli import app as cli
from autoair.marketplace import (
    Marketplace,
    MarketplaceError,
    MarketplaceSource,
    build_index,
    generate_keypair,
    sign_index,
    verify_index,
)
from autoair.templates import TemplateStore

FRAGMENT = {
    "id": "notify-slack",
    "title": "Notify Slack",
    "description": "Post a message when the DAG ends",
    "category": "alerts",
    "kind": "fragment",
    "version": "1.2.0",
    "author": "Data Platform",
    "fragment": {
        "nodes": [
            {
                "kind": "operator",
                "id": "n1",
                "task_id": "notify",
                "operator": "airflow.providers.standard.operators.empty.EmptyOperator",
            }
        ]
    },
}
DAG = {
    "id": "nightly-etl",
    "title": "Nightly ETL",
    "kind": "dag",
    "spec": {"dag_id": "nightly_etl", "nodes": []},
}
EXTENSIONS = [
    {"id": "acme-operators", "title": "Acme operators", "package": "acme-airflow-operators>=2"}
]


@pytest.fixture
def keys() -> tuple[bytes, str]:
    return generate_keypair()


@pytest.fixture
def repo(tmp_path: Path, keys: tuple[bytes, str]) -> Path:
    root = tmp_path / "market"
    (root / "templates").mkdir(parents=True)
    (root / "templates" / "notify-slack.json").write_text(json.dumps(FRAGMENT))
    (root / "templates" / "nightly-etl.json").write_text(json.dumps(DAG))
    (root / "extensions.json").write_text(json.dumps(EXTENSIONS))
    index = build_index(root, "acme", keys[0])
    (root / "index.json").write_text(json.dumps(index))
    return root


def market(tmp_path: Path, url: str, public_key: str | None, **kw: object) -> Marketplace:
    source = MarketplaceSource(name="acme", url=url, public_key=public_key, **kw)  # type: ignore[arg-type]
    return Marketplace([source], TemplateStore(str(tmp_path / "templates-store")))


def test_index_signature_roundtrip(keys):
    index = sign_index({"format": 1, "name": "x", "entries": []}, keys[0])
    assert verify_index(index, keys[1])
    assert not verify_index({**index, "name": "tampered"}, keys[1])
    assert not verify_index(index, generate_keypair()[1])
    assert not verify_index({k: v for k, v in index.items() if k != "signature"}, keys[1])


def test_build_index_lists_templates_and_extensions(repo):
    index = json.loads((repo / "index.json").read_text())
    by_id = {e["id"]: e for e in index["entries"]}
    assert by_id["notify-slack"]["version"] == "1.2.0"
    assert (
        by_id["notify-slack"]["sha256"]
        == hashlib.sha256((repo / "templates" / "notify-slack.json").read_bytes()).hexdigest()
    )
    assert by_id["acme-operators"]["kind"] == "extension"


def test_listing_and_install_from_file_url(tmp_path, repo, keys):
    m = market(tmp_path, (repo / "index.json").as_uri(), keys[1])
    listing = m.listing()
    assert listing.sources[0].ok
    assert listing.sources[0].verified
    assert {e.id for e in listing.entries} == {"notify-slack", "nightly-etl", "acme-operators"}

    tpl = m.install("acme", "notify-slack")
    assert "marketplace:acme" in tpl.tags
    saved = m.templates.get("notify-slack")
    assert saved is not None
    assert saved.fragment is not None
    assert next(e for e in m.listing().entries if e.id == "notify-slack").installed


def test_tampered_payload_is_refused(tmp_path, repo, keys):
    (repo / "templates" / "notify-slack.json").write_text(json.dumps({**FRAGMENT, "title": "evil"}))
    m = market(tmp_path, (repo / "index.json").as_uri(), keys[1])
    with pytest.raises(MarketplaceError, match="hash mismatch"):
        m.install("acme", "notify-slack")


def test_tampered_or_unsigned_index_is_refused(tmp_path, repo, keys):
    index = json.loads((repo / "index.json").read_text())
    index["entries"][0]["sha256"] = "0" * 64
    (repo / "index.json").write_text(json.dumps(index))
    m = market(tmp_path, (repo / "index.json").as_uri(), keys[1])
    status = m.listing().sources[0]
    assert not status.ok
    assert "signature" in (status.error or "")

    unsigned = market(tmp_path, (repo / "index.json").as_uri(), None)
    assert not unsigned.listing().sources[0].ok
    lenient = market(tmp_path, (repo / "index.json").as_uri(), None, require_signature=False)
    assert lenient.listing().sources[0].ok
    assert not lenient.listing().entries[0].verified


def test_path_traversal_and_extensions_are_not_installed(tmp_path, repo, keys):
    index = json.loads((repo / "index.json").read_text())
    index["entries"][0]["path"] = "../secrets.json"
    (repo / "index.json").write_text(json.dumps(sign_index(index, keys[0])))
    m = market(tmp_path, (repo / "index.json").as_uri(), keys[1])
    with pytest.raises(MarketplaceError, match="escapes"):
        m.install("acme", index["entries"][0]["id"])
    with pytest.raises(MarketplaceError, match="pip install acme-airflow-operators"):
        m.install("acme", "acme-operators")


def test_install_over_http(tmp_path, repo, keys):
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(repo))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/index.json"
        m = market(tmp_path, url, keys[1])
        assert m.install("acme", "nightly-etl").spec is not None
    finally:
        server.shutdown()


def test_cli_keygen_build_verify(tmp_path, repo):
    runner = CliRunner()
    key = tmp_path / "k.pem"
    result = runner.invoke(cli, ["marketplace", "keygen", "--out", str(key)])
    assert result.exit_code == 0, result.output
    public = result.output.strip()
    result = runner.invoke(
        cli, ["marketplace", "build", str(repo), "--name", "acme", "--key", str(key)]
    )
    assert result.exit_code == 0
    assert "3 entries" in result.output
    ok = runner.invoke(
        cli, ["marketplace", "verify", str(repo / "index.json"), "--public-key", public]
    )
    assert ok.exit_code == 0
    bad = runner.invoke(
        cli,
        ["marketplace", "verify", str(repo / "index.json"), "--public-key", generate_keypair()[1]],
    )
    assert bad.exit_code == 1
