"""Air-gapped sources: folders (files / airflow_repo / sphinx_site layouts), git, bundles."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from autoair.kb.bundle import BundleError, export_bundle, import_bundle
from autoair.kb.components import installed_version
from autoair.kb.stores.local import LocalStore
from tests.kb_fake import FakeDocs, make_kb, sphinx_index, sphinx_page


def _repo(root: Path) -> Path:
    """A miniature apache/airflow checkout."""
    core = root / "airflow-core"
    (core / "docs" / "howto" / "operator").mkdir(parents=True)
    (core / "pyproject.toml").write_text(
        '[project]\nname = "apache-airflow-core"\nversion = "3.1.0"\n'
    )
    (core / "docs" / "index.rst").write_text("Apache Airflow\n==============\n\nWelcome.\n")
    (core / "docs" / "howto" / "operator" / "bash.rst").write_text(
        "BashOperator\n============\n\nRun commands.\n\n"
        ".. exampleinclude:: /../src/airflow/example_dags/example_bash.py\n"
        "    :language: python\n    :start-after: [START howto]\n    :end-before: [END howto]\n\n"
        ".. exampleinclude:: /../../../../etc/passwd\n"
    )
    ex = core / "src" / "airflow" / "example_dags"
    ex.mkdir(parents=True)
    (ex / "example_bash.py").write_text(
        "import x\n# [START howto]\nrun = BashOperator(task_id='magic_example')\n# [END howto]\n"
    )
    std = root / "providers" / "standard"
    (std / "docs").mkdir(parents=True)
    (std / "provider.yaml").write_text(
        "package-name: apache-airflow-providers-standard\nversions:\n  - 9.9.9\n  - 9.9.8\n"
    )
    (std / "docs" / "index.rst").write_text("Standard\n========\n\nPythonOperator lives here.\n")
    other = root / "providers" / "notinstalled"
    (other / "docs").mkdir(parents=True)
    (other / "provider.yaml").write_text(
        "package-name: apache-airflow-providers-notinstalled\nversions:\n  - 1.0.0\n"
    )
    (other / "docs" / "index.rst").write_text("Nope\n====\n\nnot installed provider\n")
    return root


def test_path_source_files_layout(tmp_path: Path) -> None:
    docs = tmp_path / "corp-docs"
    (docs / "guides").mkdir(parents=True)
    (docs / "guides" / "deploy.md").write_text("# Deploying DAGs\n\n## Git sync\n\nPush to main.\n")
    (docs / "notes.txt").write_text("Plain notes about pools.\n")
    (docs / ".hidden.md").write_text("# secret\n")
    (docs / "image.png").write_bytes(b"\x89PNG")
    src = {
        "id": "corp",
        "kind": "path",
        "url": str(docs),
        "layout": "files",
        "component": "corp",
        "public_base_url": "https://wiki.corp/docs",
        "versions": ["2026.1"],
    }
    kb = make_kb(tmp_path, None, sources=[src])
    status = kb.refresh()
    assert status.outcome == "ok", status.errors
    hit = kb.search("git sync push").hits[0]
    assert hit.url == "https://wiki.corp/docs/guides/deploy.md#git-sync"
    assert (hit.component, hit.package, hit.version) == ("corp", "corp", "2026.1")
    assert kb.stats().pages == 2  # no hidden file, no image
    (docs / "notes.txt").unlink()
    assert kb.refresh().sources[0].pages_removed == 1


def test_airflow_repo_layout_resolves_examples_inside_the_checkout(tmp_path: Path) -> None:
    root = _repo(tmp_path / "airflow")
    src = {"id": "repo", "kind": "path", "url": str(root), "components": ["core", "providers"]}
    kb = make_kb(tmp_path, None, sources=[src])
    status = kb.refresh()
    assert status.outcome == "ok", status.errors
    hit = kb.search("magic_example").hits[0]
    assert (
        hit.url
        == "https://airflow.apache.org/docs/apache-airflow/3.1.0/howto/operator/bash.html#bashoperator"
    )
    assert hit.version == "3.1.0"
    assert "magic_example" in hit.text
    assert "root:" not in hit.text  # include outside the checkout is refused
    provider = kb.search("PythonOperator lives").hits[0]
    assert provider.package == "apache-airflow-providers-standard"
    assert provider.version == "9.9.9"
    assert not kb.search("not installed provider").hits  # providers=installed


def test_sphinx_site_copy_on_disk(tmp_path: Path) -> None:
    site = tmp_path / "mirror"
    version = installed_version("apache-airflow")
    d = site / "apache-airflow" / str(version)
    d.mkdir(parents=True)
    (d / "searchindex.js").write_text(sphinx_index({"index": "Apache Airflow"}))
    (d / "index.html").write_text(
        sphinx_page("Apache Airflow", [("a", "Pools", "<p>Pools limit parallelism.</p>")])
    )
    src = {
        "id": "copy",
        "kind": "path",
        "url": str(site),
        "components": ["core"],
        "public_base_url": "https://docs.corp/airflow/",
    }
    kb = make_kb(tmp_path, None, sources=[src])
    assert kb.refresh().outcome == "ok"
    hit = kb.search("pools parallelism").hits[0]
    assert hit.url == f"https://docs.corp/airflow/apache-airflow/{version}/index.html#a"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_source_checks_out_a_ref_and_skips_an_unchanged_commit(tmp_path: Path) -> None:
    root = _repo(tmp_path / "upstream")

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            env={
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@x",
                "PATH": "/usr/bin:/bin",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@x",
                "HOME": str(tmp_path),
            },
        )

    git("init", "-q", "-b", "main")
    git("add", ".")
    git("commit", "-q", "-m", "docs")
    git("tag", "3.1.0")
    src = {
        "id": "mirror",
        "kind": "git",
        "url": str(root),
        "ref": "3.1.0",
        "layout": "airflow_repo",
        "components": ["core"],
    }
    kb = make_kb(tmp_path, None, sources=[src], cache_dir=str(tmp_path / "kbcache"))
    first = kb.refresh()
    assert first.outcome == "ok", first.errors
    assert kb.search("magic_example").hits
    second = kb.refresh().sources[0]
    assert second.pages_changed == 0
    assert second.pages_unchanged == second.pages_seen > 0


def test_git_source_refuses_credentials_in_the_url(tmp_path: Path) -> None:
    src = {"id": "g", "kind": "git", "url": "https://u:p@git.corp/x.git"}
    with pytest.raises(ValueError, match="credentials"):
        make_kb(tmp_path, None, sources=[src])


# --------------------------------------------------------------------------- bundles


def _built(tmp_path: Path) -> tuple[object, bytes]:
    kb = make_kb(tmp_path / "connected", FakeDocs())
    assert kb.refresh().outcome == "ok"
    buf = io.BytesIO()
    export_bundle(kb, buf)
    return kb, buf.getvalue()


def test_bundle_round_trip_into_an_air_gapped_store(tmp_path: Path) -> None:
    source_kb, data = _built(tmp_path)
    target = make_kb(tmp_path / "airgap", None, online=False)
    report, _ = import_bundle(target, io.BytesIO(data))
    pages = source_kb.stats().pages  # type: ignore[attr-defined]
    assert report.pages == report.changed == pages
    assert not report.signed
    hit = target.search("jinja template not found").hits[0]
    assert hit.source == "bundle"
    assert "/bash.html#" in hit.url
    again, _ = import_bundle(target, io.BytesIO(data))
    assert again.unchanged == pages
    assert again.changed == 0


def test_bundle_checksum_tamper_and_signatures(tmp_path: Path) -> None:
    from autoair.marketplace import generate_keypair

    source_kb, data = _built(tmp_path)
    # tampered pages.jsonl
    src = zipfile.ZipFile(io.BytesIO(data))
    pages = src.read("pages.jsonl").replace(b"Jinja", b"Ninja")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("manifest.json", src.read("manifest.json"))
        zf.writestr("pages.jsonl", pages)
    target = make_kb(tmp_path / "t1", None, online=False)
    with pytest.raises(BundleError, match="checksum"):
        import_bundle(target, io.BytesIO(out.getvalue()))
    assert target.stats().pages == 0  # nothing imported
    # signatures required by configured public keys
    pem, public = generate_keypair()
    other_pem, other_public = generate_keypair()
    signed = io.BytesIO()
    export_bundle(source_kb, signed, private_key_pem=pem)  # type: ignore[arg-type]
    strict = make_kb(tmp_path / "t2", None, online=False, bundle={"public_keys": [public]})
    with pytest.raises(BundleError, match="not signed"):
        import_bundle(strict, io.BytesIO(data))
    report, _ = import_bundle(strict, io.BytesIO(signed.getvalue()))
    assert report.signed
    assert report.signature_verified
    wrong = make_kb(tmp_path / "t3", None, online=False, bundle={"public_keys": [other_public]})
    with pytest.raises(BundleError, match="signature"):
        import_bundle(wrong, io.BytesIO(signed.getvalue()))
    manifest = json.loads(zipfile.ZipFile(signed).read("manifest.json"))
    assert manifest["kind"] == "autoair-kb-bundle"
    assert manifest["signature"]
    assert other_pem


def test_bundle_rejects_garbage_and_limits(tmp_path: Path) -> None:
    target = make_kb(tmp_path, None, online=False, bundle={"max_pages": 1})
    with pytest.raises(BundleError, match="zip"):
        import_bundle(target, io.BytesIO(b"not a zip"))
    _, data = _built(tmp_path)
    with pytest.raises(BundleError, match="more than 1 pages"):
        import_bundle(target, io.BytesIO(data))


def test_bundle_source_from_a_folder_is_imported_incrementally(tmp_path: Path) -> None:
    _, data = _built(tmp_path)
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "kb.zip").write_bytes(data)
    src = {"id": "drop", "kind": "bundle", "url": str(drop / "kb.zip")}
    kb = make_kb(tmp_path / "airgap", None, online=False, sources=[src])
    first = kb.refresh().sources[0]
    assert first.state == "ok"
    assert first.pages_changed > 0
    second = kb.refresh().sources[0]
    assert second.pages_changed == 0
    assert second.pages_unchanged == first.pages_changed
    assert {h.source for h in kb.search("bash").hits} == {"drop"}
    assert isinstance(kb.store, LocalStore)
