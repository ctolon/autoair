"""Validate version consistency and the contents of release artifacts."""

from __future__ import annotations

import argparse
import json
import os
import re
import tarfile
import zipfile
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", action="store_true")
    args = parser.parse_args()
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    version = project["version"]
    source = (ROOT / "src/autoair/__init__.py").read_text()
    assert re.search(rf"__version__\s*=\s*[\"\']{re.escape(version)}[\"\']", source)
    for package in ["frontend", "website"]:
        assert json.loads((ROOT / package / "package.json").read_text())["version"] == version
    tag = os.environ.get("RELEASE_TAG")
    assert not tag or tag == f"v{version}", f"tag {tag!r} does not match {version}"
    assert (ROOT / "LICENSE").is_file()
    if args.artifacts:
        wheels = list((ROOT / "dist").glob("*.whl"))
        sources = list((ROOT / "dist").glob("*.tar.gz"))
        assert len(wheels) == len(sources) == 1, "expected one wheel and one sdist"
        with zipfile.ZipFile(wheels[0]) as wheel:
            names = wheel.namelist()
            for name in [
                "autoair/py.typed",
                "autoair/static/plugin/main.js",
                "autoair/static/standalone/index.html",
                "autoair/static/plugin/THIRD_PARTY_LICENSES.md",
                "autoair/static/standalone/THIRD_PARTY_LICENSES.md",
                "autoair/docs/pages.json",  # AutoAir's own docs (hatch_build.py)
            ]:
                assert name in names, f"missing wheel asset: {name}"
            assert any("/licenses/LICENSE" in name for name in names)
        with tarfile.open(sources[0]) as source_archive:
            names = source_archive.getnames()
            assert not any("/.backups/" in name or "/.airflow-dev/" in name for name in names)
    print(f"AutoAir {version}: release metadata verified")


if __name__ == "__main__":
    main()
