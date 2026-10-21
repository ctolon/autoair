"""Hatch build hook: package AutoAir's own documentation into the wheel and the sdist.

Writes ``src/autoair/docs/pages.json`` from ``website/docs`` (and the configuration
reference) with :mod:`autoair.docs.build`, loaded by path because the isolated build
environment has none of AutoAir's dependencies. The file is a gitignored build artefact
(``[tool.hatch.build] artifacts``), so every build carries the documentation of the tree it
was built from.

* source checkout (``website/docs`` present): the file is regenerated;
* sdist -> wheel (``uv build``; the sdist has no ``website/``): the file shipped in the
  sdist is used;
* editable installs (``uv sync``) skip the hook: in a checkout the pages are built from
  the sources at run time (:func:`autoair.docs.index.load_data`).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

_BUILDER = Path("src") / "autoair" / "docs" / "build.py"
_DATA = Path("src") / "autoair" / "docs" / "pages.json"


class CustomBuildHook(BuildHookInterface):  # type: ignore[type-arg]
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if version == "editable":
            return
        root = Path(self.root)
        data = root / _DATA
        if (root / "website" / "docs").is_dir():
            spec = importlib.util.spec_from_file_location("_autoair_docs_build", root / _BUILDER)
            if spec is None or spec.loader is None:
                raise RuntimeError(f"cannot load {_BUILDER}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.write(root, data)
        elif not data.is_file():
            raise RuntimeError(
                f"{_DATA} is missing and website/docs is not available to build it: build "
                "from a source checkout or from an sdist made by one"
            )
