"""Documentation pages -> clean text sections with heading paths and anchors.

* **HTML** (Sphinx pages of airflow.apache.org and mirrors): only the main content
  (``div[role=main]``, ``<main>``, ``<article>``); navigation, scripts and the ``¶`` header
  links are dropped, ``<pre>`` blocks become fenced code, ``<section id>`` / heading ids
  become anchors.
* **reStructuredText** (the apache/airflow sources, Sphinx ``_sources``): over/underlined
  titles, code blocks, admonitions; ``exampleinclude`` / ``literalinclude`` / ``include``
  are resolved through a callback (repository checkouts) or dropped; roles become text.
* **Markdown** (Astronomer pages, files): ATX headings, front matter title, fences kept.

The parsers never execute or import anything and are bounded by the input size.
"""

from __future__ import annotations

import html
import re
import textwrap
from collections.abc import Callable
from html.parser import HTMLParser

from autoair.kb.models import Section

IncludeResolver = Callable[[str, dict[str, str]], str | None]

_WS = re.compile(r"[ \t\r\f\v]+")
_BLANKS = re.compile(r"\n\s*\n+")
_LIST_GAP = re.compile(r"(?m)^- *\n\s*(?=\S)")


def slugify(text: str) -> str:
    """docutils / GitHub style fragment id of a heading."""
    value = re.sub(r"[^\w\s-]", "", text.lower(), flags=re.UNICODE)
    value = re.sub(r"[\s_]+", "-", value.strip())
    return re.sub(r"-{2,}", "-", value).strip("-")


def _clean(text: str) -> str:
    out: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append(line.strip())
            continue
        out.append(line.rstrip() if in_fence else _WS.sub(" ", line).strip())
    joined = _LIST_GAP.sub("- ", "\n".join(out))
    return _BLANKS.sub("\n\n", joined).strip()


class _Builder:
    """Collects sections from a stream of headings and text."""

    def __init__(self) -> None:
        self.sections: list[Section] = []
        self.path: list[str] = []
        self.anchor: str | None = None
        self.level = 1
        self.buf: list[str] = []
        self.title = ""

    def heading(self, level: int, text: str, anchor: str | None) -> None:
        self.flush()
        text = _WS.sub(" ", text).strip()
        if not text:
            return
        if not self.title:
            self.title = text
        level = max(1, min(level, 6))
        self.path = [*self.path[: level - 1], text]
        self.level = level
        self.anchor = anchor or slugify(text) or None

    def text(self, value: str) -> None:
        self.buf.append(value)

    def flush(self) -> None:
        body = _clean("".join(self.buf))
        self.buf = []
        if body:
            self.sections.append(
                Section(
                    heading=" > ".join(self.path),
                    anchor=self.anchor,
                    level=self.level,
                    text=body,
                )
            )


# --------------------------------------------------------------------------- HTML

_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}
)
_SKIP = frozenset(
    {"script", "style", "nav", "header", "footer", "noscript", "svg", "form", "button", "template"}
)
_BLOCK = frozenset(
    {
        "p", "div", "section", "article", "li", "ul", "ol", "dl", "dt", "dd", "tr", "table",
        "blockquote", "figure", "figcaption", "aside", "br", "hr",
    }
)  # fmt: skip
_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}


def _main_container(raw: str) -> tuple[str, dict[str, str]] | None:
    """The element holding the page's content, most specific first."""
    if re.search(r"<div\b[^>]*\brole=[\"']main[\"']", raw, re.IGNORECASE):
        return "div", {"role": "main"}
    if re.search(r"<main\b", raw, re.IGNORECASE):
        return "main", {}
    if re.search(r"<article\b", raw, re.IGNORECASE):
        return "article", {}
    return None


class _HtmlExtractor(HTMLParser):
    def __init__(self, container: tuple[str, dict[str, str]] | None) -> None:
        super().__init__(convert_charrefs=True)
        self.container = container
        self.capturing = container is None
        self.depth = 0  # open elements of the container's tag while capturing
        self.done = False
        self.skip = 0
        self.pre = 0
        self.heading: tuple[int, str | None] | None = None
        self.heading_text: list[str] = []
        self.section_id: str | None = None
        self.page_title: list[str] = []
        self.in_title = False
        self.builder = _Builder()

    @staticmethod
    def _attrs(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {k.lower(): (v or "") for k, v in attrs}

    def _starts_container(self, tag: str, attrs: dict[str, str]) -> bool:
        assert self.container is not None
        want_tag, want_attrs = self.container
        return tag == want_tag and all(attrs.get(k) == v for k, v in want_attrs.items())

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = self._attrs(attrs_list)
        if tag == "title":
            self.in_title = True
        if self.done:
            return
        if not self.capturing:
            if self.container is not None and self._starts_container(tag, attrs):
                self.capturing = True
                self.depth = 1
            return
        if self.container is not None and tag == self.container[0] and tag not in _VOID:
            self.depth += 1
        classes = attrs.get("class", "").split()
        if tag in _SKIP or "headerlink" in classes or self.skip:
            if tag not in _VOID:
                self.skip += 1
            return
        if tag == "section" and attrs.get("id"):
            self.section_id = attrs["id"]
        if tag in _HEADINGS:
            self.heading = (_HEADINGS[tag], attrs.get("id") or self.section_id)
            self.heading_text = []
            return
        if tag == "pre":
            self.pre += 1
            self.builder.text("\n\n```\n")
            return
        if tag == "li":
            self.builder.text("\n- ")
        elif tag in ("td", "th"):
            self.builder.text(" | ")
        elif tag in _BLOCK:
            self.builder.text("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        if self.done or not self.capturing:
            return
        if self.skip:
            if tag not in _VOID:
                self.skip -= 1
            return
        if self.heading is not None and tag in _HEADINGS:
            level, anchor = self.heading
            self.heading = None
            self.builder.heading(level, "".join(self.heading_text), anchor)
            return
        if tag == "pre" and self.pre:
            self.pre -= 1
            self.builder.text("\n```\n\n")
            return
        if tag in _BLOCK:
            self.builder.text("\n")
        if self.container is not None and tag == self.container[0]:
            self.depth -= 1
            if self.depth <= 0:
                self.done = True

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.page_title.append(data)
            return
        if self.done or not self.capturing or self.skip:
            return
        if self.heading is not None:
            self.heading_text.append(data)
            return
        if self.pre:
            self.builder.text(data)
        else:
            self.builder.text(data.replace("\n", " "))


def parse_html(raw: str) -> tuple[str, list[Section]]:
    """(title, sections) of an HTML page."""
    parser = _HtmlExtractor(_main_container(raw))
    parser.feed(raw)
    parser.close()
    parser.builder.flush()
    sections = parser.builder.sections
    # Widgets before the first heading (search boxes, banners) are not content.
    if len(sections) > 1 and not sections[0].heading and len(sections[0].text) < 120:
        sections = sections[1:]
    title = parser.builder.title
    if not title:
        title = html.unescape("".join(parser.page_title)).split(" — ")[0].strip()
    return title, sections


# --------------------------------------------------------------------------- reStructuredText

_ADORN = re.compile(r"^([=\-~^\"'`#*+:.<>_])\1{2,}\s*$")
_DIRECTIVE = re.compile(r"^(\s*)\.\.\s+([\w:-]+)::\s*(.*)$")
_TARGET = re.compile(r"^\s*\.\.\s+_[^:]+:\s*\S*\s*$")
_COMMENT = re.compile(r"^\s*\.\.(\s|$)")
_FIELD = re.compile(r"^\s*:([\w-]+):\s*(.*)$")
_ROLE_TEXT_TARGET = re.compile(r":[\w:+-]+:`([^`<]*?)\s*<[^`>]*>`")
_ROLE = re.compile(r":[\w:+-]+:`~?([^`]*)`")
_LINK = re.compile(r"`([^`<]+?)\s*<([^`>]+)>`__?")
_LITERAL = re.compile(r"``([^`]+)``")
_EMPH = re.compile(r"(?<![\w*])\*{1,2}([^*\n]+)\*{1,2}(?![\w*])")

_DROP_DIRECTIVES = frozenset(
    {
        "toctree", "image", "figure", "contents", "autoclass", "automodule", "autofunction",
        "automethod", "autoattribute", "autosummary", "autoapisummary", "module", "currentmodule",
        "index", "raw", "only", "jinja", "spelling:word-list", "operators-hooks-ref",
        "transfers-ref", "providers-packages-ref", "airflow-providers-commits", "redirect-from",
        "meta", "highlight", "default-domain", "role", "list-table", "csv-table",
    }
)  # fmt: skip
_CODE_DIRECTIVES = frozenset({"code-block", "code", "sourcecode", "parsed-literal", "prompt"})
_INCLUDE_DIRECTIVES = frozenset({"exampleinclude", "literalinclude"})
_ADMONITIONS = frozenset(
    {
        "note", "warning", "tip", "important", "caution", "danger", "attention", "hint",
        "seealso", "deprecated", "versionadded", "versionchanged", "admonition", "error",
    }
)  # fmt: skip


def _inline(text: str) -> str:
    text = _ROLE_TEXT_TARGET.sub(r"\1", text)
    text = _LINK.sub(r"\1 (\2)", text)
    text = _ROLE.sub(lambda m: m.group(1).lstrip("~!"), text)
    text = _LITERAL.sub(r"`\1`", text)
    text = _EMPH.sub(r"\1", text)
    return text.replace("\\", "")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _block(lines: list[str], start: int, base: int) -> tuple[list[str], int]:
    """The indented block after a directive line (lines indented deeper than ``base``)."""
    out: list[str] = []
    i = start
    while i < len(lines):
        line = lines[i]
        if line.strip() and _indent(line) <= base:
            break
        out.append(line)
        i += 1
    while out and not out[-1].strip():
        out.pop()
    return out, i


def _options(body: list[str]) -> tuple[dict[str, str], list[str]]:
    opts: dict[str, str] = {}
    i = 0
    while i < len(body) and not body[i].strip():
        i += 1
    while i < len(body):
        m = _FIELD.match(body[i])
        if not m:
            break
        opts[m.group(1)] = m.group(2).strip()
        i += 1
    return opts, body[i:]


def _fence(code: str, lang: str = "") -> str:
    return f"\n\n```{lang}\n{code.rstrip()}\n```\n\n"


def _slice_example(text: str, opts: dict[str, str]) -> str:
    after, before = opts.get("start-after"), opts.get("end-before")
    if after and after in text:
        text = text.split(after, 1)[1]
        text = text.split("\n", 1)[1] if "\n" in text else ""
    if before and before in text:
        text = text.split(before, 1)[0]
        text = text.rsplit("\n", 1)[0] if "\n" in text else text
    return textwrap.dedent(text).strip("\n")


def _expand_includes(
    lines: list[str], resolve: IncludeResolver | None, depth: int = 0
) -> list[str]:
    """Replace ``.. include::`` with the included rst (resolved by ``resolve``)."""
    if resolve is None or depth > 3:
        return lines
    out: list[str] = []
    i = 0
    while i < len(lines):
        m = _DIRECTIVE.match(lines[i])
        if m and m.group(2) == "include":
            body, nxt = _block(lines, i + 1, _indent(lines[i]))
            opts, _ = _options(body)
            included = resolve(m.group(3).strip(), opts)
            if included is not None:
                out.extend(_expand_includes(included.splitlines(), resolve, depth + 1))
            i = nxt
            continue
        out.append(lines[i])
        i += 1
    return out


def parse_rst(text: str, resolve: IncludeResolver | None = None) -> tuple[str, list[Section]]:
    """(title, sections) of a reStructuredText document."""
    lines = _expand_includes(text.replace("\t", "    ").splitlines(), resolve)
    builder = _Builder()
    styles: list[tuple[str, bool]] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()
        # --- section titles (optional overline, title, underline)
        if (
            i + 2 < n
            and _ADORN.match(line)
            and lines[i + 1].strip()
            and _ADORN.match(lines[i + 2])
            and line.strip()[0] == lines[i + 2].strip()[0]
            and not line.startswith(" ")
        ):
            style = (line.strip()[0], True)
            if style not in styles:
                styles.append(style)
            builder.heading(styles.index(style) + 1, _inline(lines[i + 1].strip()), None)
            i += 3
            continue
        if (
            i + 1 < n
            and stripped
            and not line.startswith(" ")
            and _ADORN.match(lines[i + 1])
            and len(lines[i + 1].strip()) >= len(stripped)
            and not _DIRECTIVE.match(line)
            and not _COMMENT.match(line)
        ):
            style = (lines[i + 1].strip()[0], False)
            if style not in styles:
                styles.append(style)
            builder.heading(styles.index(style) + 1, _inline(stripped), None)
            i += 2
            continue
        # --- directives
        m = _DIRECTIVE.match(line)
        if m:
            base, name, arg = _indent(line), m.group(2).lower(), m.group(3).strip()
            body, nxt = _block(lines, i + 1, base)
            opts, content = _options(body)
            dedented = textwrap.dedent("\n".join(content)).strip("\n")
            if name in _CODE_DIRECTIVES:
                builder.text(_fence(dedented, arg.split()[0] if arg else ""))
            elif name in _INCLUDE_DIRECTIVES:
                included = resolve(arg, opts) if resolve is not None else None
                if included:
                    lang = opts.get("language", "python" if arg.endswith(".py") else "")
                    builder.text(_fence(_slice_example(included, opts), lang))
            elif name in _ADMONITIONS:
                label = name.replace("seealso", "see also").capitalize()
                head = f"{label}: {_inline(arg)}".rstrip(": ") if arg else f"{label}:"
                builder.text(f"\n\n{head} {_inline(dedented)}\n\n")
            elif name in _DROP_DIRECTIVES or name.startswith(("auto", "jinja")):
                pass
            elif dedented:
                builder.text("\n\n" + _inline(dedented) + "\n\n")
            i = nxt
            continue
        if _TARGET.match(line):
            i += 1
            continue
        if _COMMENT.match(line) and not _DIRECTIVE.match(line):
            _, i = _block(lines, i + 1, _indent(line))
            continue
        # --- literal blocks after a paragraph ending with "::"
        if stripped.endswith("::") and not stripped.startswith(".."):
            prose = stripped[:-2].rstrip()
            if prose:
                builder.text(_inline(line.rstrip()[:-1]) + "\n")
            body, nxt = _block(lines, i + 1, _indent(line))
            code = textwrap.dedent("\n".join(body)).strip("\n")
            if code:
                builder.text(_fence(code))
            i = nxt
            continue
        builder.text(_inline(line) + "\n")
        i += 1
    builder.flush()
    return builder.title, builder.sections


# --------------------------------------------------------------------------- Markdown

_FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_ATX = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)[^)]*\)")
_TAG = re.compile(r"</?[A-Za-z][\w.-]*(?:\s[^<>]*)?/?>")


def parse_markdown(text: str) -> tuple[str, list[Section]]:
    """(title, sections) of a Markdown (or MDX) document."""
    title = ""
    m = _FRONT.match(text)
    if m:
        for line in m.group(1).splitlines():
            key, _, value = line.partition(":")
            if key.strip() == "title" and value.strip():
                title = value.strip().strip("'\"")
        text = text[m.end() :]
    builder = _Builder()
    if title:
        builder.heading(1, title, slugify(title))
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            # Keep the language only: MDX fences carry display options ("python wrap ...").
            fence = line.strip()
            lang = fence[3:].split()[0] if len(fence) > 3 and fence[3:].split() else ""
            builder.text("```" + re.sub(r"[^\w+#.-]", "", lang) + "\n")
            continue
        if in_fence:
            builder.text(line + "\n")
            continue
        h = _ATX.match(line)
        if h:
            heading = _MD_LINK.sub(r"\1", h.group(2))
            level = len(h.group(1))
            if title and level == 1 and heading == title:
                continue
            builder.heading(level + (1 if title and level == 1 else 0), heading, None)
            continue
        line = _MD_IMAGE.sub("", line)
        line = _MD_LINK.sub(r"\1", line)
        line = _TAG.sub("", line)
        builder.text(line + "\n")
    builder.flush()
    return title or builder.title, builder.sections


def parse_text(text: str, title: str = "") -> tuple[str, list[Section]]:
    body = _clean(text)
    return title, [Section(heading=title, text=body)] if body else []


def parse(
    content: str, fmt: str, *, title: str = "", resolve: IncludeResolver | None = None
) -> tuple[str, list[Section]]:
    """Dispatch on the format: ``html``, ``rst``, ``md`` or ``text``."""
    if fmt == "html":
        found, sections = parse_html(content)
    elif fmt == "rst":
        found, sections = parse_rst(content, resolve)
    elif fmt == "md":
        found, sections = parse_markdown(content)
    else:
        found, sections = parse_text(content, title)
    return (found or title).strip(), sections


def format_of(path: str, content_type: str = "") -> str:
    lower = path.lower().split("?", 1)[0]
    if lower.endswith((".html", ".htm")) or "html" in content_type:
        return "html"
    if lower.endswith((".rst", ".rst.txt")):
        return "rst"
    if lower.endswith((".md", ".mdx", ".markdown")) or "markdown" in content_type:
        return "md"
    return "text"


__all__ = [
    "IncludeResolver",
    "format_of",
    "parse",
    "parse_html",
    "parse_markdown",
    "parse_rst",
    "parse_text",
    "slugify",
]
