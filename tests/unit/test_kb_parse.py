from __future__ import annotations

from autoair.kb.chunk import chunk_page
from autoair.kb.config import KbChunkSettings
from autoair.kb.models import PageDoc, Section
from autoair.kb.parse import format_of, parse_html, parse_markdown, parse_rst, slugify
from tests.kb_fake import sphinx_page


def test_html_keeps_main_content_headings_anchors_and_code() -> None:
    html = sphinx_page(
        "BashOperator",
        [
            (
                "templating",
                "Templating",
                "<p>Use <code>bash_command</code>.</p>"
                "<pre>t = BashOperator(\n    task_id='x')</pre>",
            )
        ],
    )
    title, sections = parse_html(html)
    assert title == "BashOperator"
    text = "\n".join(s.text for s in sections)
    assert "Navigation junk" not in text
    assert "Footer junk" not in text
    assert "var x" not in text
    assert "¶" not in text
    assert "¶" not in "".join(s.heading for s in sections)
    templ = next(s for s in sections if s.anchor == "templating")
    assert templ.heading == "BashOperator > Templating"
    assert "```\nt = BashOperator(\n    task_id='x')\n```" in templ.text


def test_html_without_main_container_and_broken_markup_does_not_fail() -> None:
    title, sections = parse_html("<html><title>T — x</title><body><h2>A<p>b</div></body>")
    assert title == "T"  # the unclosed heading never ends: the <title> is used
    assert isinstance(sections, list)


def test_rst_titles_directives_roles_and_includes() -> None:
    rst = """.. Licensed to the Apache Software Foundation
   under the license.

.. _howto/operator:BashOperator:

BashOperator
============

Use the :class:`~airflow.providers.standard.operators.bash.BashOperator`. See
`the docs <https://example.org>`_ and ``bash_command``.

.. exampleinclude:: /../tests/example_bash.py
    :language: python
    :start-after: [START howto]
    :end-before: [END howto]

Templating
----------

.. note::
    Jinja works.

.. toctree::
    :maxdepth: 1

    other

Example::

    run_this = 1
"""
    calls: list[str] = []

    def resolve(path: str, opts: dict[str, str]) -> str | None:
        calls.append(path)
        return "x = 0\n# [START howto]\n    t = BashOperator(task_id='a')\n# [END howto]\n"

    title, sections = parse_rst(rst, resolve)
    assert title == "BashOperator"
    assert calls == ["/../tests/example_bash.py"]
    first, second = sections
    assert first.anchor == "bashoperator"
    assert "airflow.providers.standard.operators.bash.BashOperator" in first.text
    assert "the docs (https://example.org)" in first.text
    assert "```python\nt = BashOperator(task_id='a')\n```" in first.text
    assert "Licensed" not in first.text
    assert second.heading == "BashOperator > Templating"
    assert "Note: Jinja works." in second.text
    assert "maxdepth" not in second.text
    assert "other" not in second.text
    assert "```\nrun_this = 1\n```" in second.text


def test_rst_without_resolver_drops_includes() -> None:
    _, sections = parse_rst("T\n=\n\n.. exampleinclude:: /x.py\n\nbody\n")
    assert "exampleinclude" not in sections[0].text
    assert "body" in sections[0].text


def test_markdown_front_matter_headings_and_fences() -> None:
    md = """---
title: Pass data
---

Intro text with a [link](https://x.y) and ![img](a.png).

## XCom basics

```python
# not a heading
x = 1
```

<Note>Use object storage.</Note>
"""
    title, sections = parse_markdown(md)
    assert title == "Pass data"
    basics = next(s for s in sections if s.anchor == "xcom-basics")
    assert basics.heading == "Pass data > XCom basics"
    assert "# not a heading" in basics.text
    assert "<Note>" not in basics.text
    assert "Use object storage." in basics.text
    intro = sections[0]
    assert "link" in intro.text
    assert "https://x.y" not in intro.text
    assert "img" not in intro.text


def test_slugify_and_formats() -> None:
    assert slugify("Copy an Amazon S3 object") == "copy-an-amazon-s3-object"
    assert format_of("a/_sources/x.rst.txt") == "rst"
    assert format_of("x.html") == "html"
    assert format_of("x.md") == "md"
    assert format_of("x.txt") == "text"


def _page(sections: list[Section]) -> PageDoc:
    return PageDoc(
        id="p1",
        source="s",
        unit="u",
        component="core",
        package="apache-airflow",
        version="3.1.0",
        url="https://d/x.html",
        title="T",
        sections=sections,
    )


def test_chunks_follow_sections_merge_short_ones_and_dedupe() -> None:
    long_text = "\n\n".join(f"Paragraph {i} " + "word " * 60 for i in range(10))
    page = _page(
        [
            Section(heading="T > A", anchor="a", text="short"),
            Section(heading="T > B", anchor="b", text="also short"),
            Section(heading="T > C", anchor="c", text=long_text),
            Section(heading="T > D", anchor="d", text=long_text),
        ]
    )
    chunks = chunk_page(page, KbChunkSettings(size=600, overlap=50, min_chars=100))
    assert chunks[0].url == "https://d/x.html#a"
    assert "also short" in chunks[0].text  # merged into the short section before it
    c_chunks = [c for c in chunks if c.url.endswith("#c")]
    assert len(c_chunks) > 1
    assert all(len(c.text) <= 700 for c in c_chunks)
    assert not [c for c in chunks if c.url.endswith("#d")]  # identical text kept once
    assert [c.n for c in chunks] == list(range(len(chunks)))
    capped = chunk_page(page, KbChunkSettings(size=600, overlap=50, max_chunks_per_page=2))
    assert len(capped) == 2
