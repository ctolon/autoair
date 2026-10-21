"""Project scaffolds (``autoair new-node``).

``custom-node/`` is a cookiecutter-compatible template (``cookiecutter.json`` plus a
``{{cookiecutter.project_slug}}`` tree): ``autoair new-node`` renders it with Jinja2 (no
cookiecutter needed), and ``cookiecutter <this folder>/custom-node`` works as well.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

TEMPLATES = Path(__file__).parent
NODE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,60}$")


class ScaffoldError(ValueError):
    pass


def _env() -> SandboxedEnvironment:
    # Code templates: no HTML escaping; undefined variables are template bugs.
    return SandboxedEnvironment(
        keep_trailing_newline=True, undefined=StrictUndefined, autoescape=False
    )


def context(template: str, values: dict[str, str]) -> dict[str, Any]:
    """cookiecutter.json defaults (rendered in order, so later ones can use earlier ones),
    overridden by ``values``."""
    defaults: dict[str, Any] = json.loads((TEMPLATES / template / "cookiecutter.json").read_text())
    unknown = set(values) - set(defaults)
    if unknown:
        raise ScaffoldError(f"unknown template variables: {sorted(unknown)}")
    env = _env()
    ctx: dict[str, Any] = {}
    for key, default in defaults.items():
        raw = values.get(key, default)
        ctx[key] = env.from_string(str(raw)).render(cookiecutter=ctx) if key not in values else raw
    return ctx


def render(template: str, values: dict[str, str], output: Path) -> Path:
    """Render ``template`` into ``output`` (the project folder is created inside it and must
    not exist yet). Returns the project folder."""
    ctx = context(template, values)
    env = _env()
    root = TEMPLATES / template
    [project] = [p for p in root.iterdir() if p.is_dir() and p.name.startswith("{{")]
    target = output / env.from_string(project.name).render(cookiecutter=ctx)
    if target.exists():
        raise ScaffoldError(f"{target} already exists")
    for src in sorted(project.rglob("*")):
        if "__pycache__" in src.parts or src.suffix == ".pyc":
            continue
        rel = src.relative_to(project)
        dest = target.joinpath(
            *(env.from_string(part).render(cookiecutter=ctx) for part in rel.parts)
        )
        if src.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        text = env.from_string(src.read_text(encoding="utf-8")).render(cookiecutter=ctx)
        dest.write_text(text, encoding="utf-8")
    return target


def new_node(name: str, output: Path, **values: str) -> Path:
    """Scaffold a package registering node kind ``name`` (``autoair new-node``)."""
    if not NODE_NAME.match(name):
        raise ScaffoldError(
            f"invalid node name {name!r}: lowercase letters, digits and '_', starting with a letter"
        )
    return render("custom-node", {"node_name": name, **values}, output)


__all__ = ["ScaffoldError", "new_node", "render"]
