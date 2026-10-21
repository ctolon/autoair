"""Generate ``docs/configuration-reference.md`` from the :class:`~autoair.settings.Settings` model.

One table per section (nested models are recursed): environment variable, configuration-file
key, type, default, description and whether the value is secret (masked by ``/api/config``)::

    uv run python scripts/gen_config_docs.py           # write the file
    uv run python scripts/gen_config_docs.py --check   # exit 1 when the file is stale
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "configuration-reference.md"
EMPTY = '`""`'


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render() -> str:
    from autoair.config_reference import iter_options, iter_sections
    from autoair.settings import Settings

    options = list(iter_options(Settings))
    lines = [
        "# Configuration reference",
        "",
        "Generated from the `Settings` model by `scripts/gen_config_docs.py`; do not edit by hand",
        "(a test fails when it is stale). The narrative guide is",
        "[configuration.md](configuration.md).",
        "",
        "Every option can be set as an **environment variable** (`AUTOAIR_<KEY>`, nested",
        "sections joined with `__`), in a **configuration file** (YAML/JSON/Python: the",
        "dotted key as nested mappings; INI: `[autoair]` / `[autoair.<section>]` sections)",
        "or, for some, as a CLI option. Precedence: init arguments > environment > `.env` >",
        "configuration files > defaults. Lists and maps are JSON in environment variables",
        "and INI files.",
        "",
        "**Secret** values are masked as `***` by `GET /api/config` (`expose_config=true`) and",
        "left out with `expose_config=non-sensitive-only`.",
        "",
        f"{len(options)} options.",
        "",
    ]
    for path, _model, description in iter_sections(Settings):
        rows = [o for o in options if o.section == path]
        if not rows:
            continue
        title = f"`{path}`" if path else "Top level"
        lines += [f"## {title}", ""]
        if description:
            lines += [description, ""]
        lines += [
            "| Environment variable | Config key | Type | Default | Description | Secret |",
            "|---|---|---|---|---|---|",
        ]
        for o in rows:
            default = o.default_text
            # Always a code span with escaped pipes: defaults include regexes such as
            # "(^|\.)conftest$" whose bare "|" would split the table row.
            default = f"`{_cell(default)}`" if default else EMPTY
            secret = "yes" if o.secret else ""
            lines.append(
                f"| `{o.env}` | `{o.key}` | `{_cell(o.type)}` | {default} | "
                f"{_cell(o.description)} | {secret} |"
            )
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--check", action="store_true", help="fail when the file is stale")
    args = parser.parse_args(argv)
    text = render() + "\n"
    if args.check:
        if not OUT.exists() or OUT.read_text(encoding="utf-8") != text:
            print(
                f"{OUT.relative_to(ROOT)} is stale: run scripts/gen_config_docs.py", file=sys.stderr
            )
            return 1
        return 0
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
