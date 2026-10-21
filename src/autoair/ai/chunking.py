"""Chunking for retrieval: Markdown by headings, Python by syntax tree, text by paragraphs.

Chunks stay below ``size`` characters where possible and overlap by ``overlap``
characters, so a passage that straddles a boundary is still found. Code is split at
class / function boundaries (never inside a statement) and every code chunk carries its
qualified name, which is also what users search for.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Chunk:
    title: str
    text: str
    #: Qualified symbol (code) or heading path (Markdown).
    anchor: str = ""
    start_line: int = 1


def chunk_text(text: str, size: int = 1200, overlap: int = 150) -> list[str]:
    """Paragraph-aware windows of at most ~``size`` characters."""
    text = text.strip()
    size = max(1, size)
    # An overlap of half the window or more could stall the hard split below.
    overlap = max(0, min(overlap, size // 2 - 1)) if size > 2 else 0
    if len(text) <= size:
        return [text] if text else []
    paragraphs = re.split(r"\n\s*\n", text)
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        while len(para) > size:  # one huge paragraph: hard split on whitespace
            cut = para.rfind(" ", 0, size)
            cut = cut if cut > size // 2 else size
            if current:
                chunks.append(current)
                current = ""
            chunks.append(para[:cut].strip())
            # Always advance by at least one character, whatever the overlap.
            para = para[max(1, cut - overlap) :].strip()
        if current and len(current) + len(para) + 2 > size:
            chunks.append(current)
            tail = current[-overlap:] if overlap else ""
            space = tail.find(" ")
            current = (tail[space + 1 :] + "\n\n" if space >= 0 and tail else "") + para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current:
        chunks.append(current)
    return chunks


def chunk_markdown(text: str, size: int = 1200, overlap: int = 150) -> list[Chunk]:
    """One chunk per ``#``/``##``/``###`` section (split further when too long); code
    fences are kept intact when they fit."""
    lines = text.splitlines()
    title = ""
    if lines and lines[0].startswith("# "):
        title = lines[0][2:].strip()
    sections: list[tuple[list[str], int, list[str]]] = []
    path: list[str] = [title] if title else []
    current: list[str] = []
    start = 1
    in_fence = False
    for no, line in enumerate(lines, start=1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        match = None if in_fence else re.match(r"^(#{1,3})\s+(.*)", line)
        if match:
            if current:
                sections.append((list(path), start, current))
            level = len(match.group(1))
            heading = match.group(2).strip()
            path = [*path[: max(0, level - 1)], heading] if level > 1 else [heading]
            current, start = [], no + 1
            continue
        current.append(line)
    if current:
        sections.append((list(path), start, current))
    out: list[Chunk] = []
    for heading_path, line_no, body in sections:
        joined = "\n".join(body).strip()
        if not joined:
            continue
        name = " - ".join(h for h in heading_path if h) or title
        for piece in chunk_text(joined, size, overlap):
            out.append(Chunk(name, piece, " > ".join(heading_path), line_no))
    return out


def _segment(lines: list[str], node: ast.AST) -> str:
    start = getattr(node, "lineno", 1)
    decorators = getattr(node, "decorator_list", [])
    if decorators:
        start = min(start, *(d.lineno for d in decorators))
    end = getattr(node, "end_lineno", None) or start
    return "\n".join(lines[start - 1 : end])


def chunk_python(source: str, module: str = "", size: int = 2400) -> list[Chunk]:
    """Module docstring + header, then one chunk per top-level function / class (classes
    split per method when they exceed ``size``)."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return [Chunk(module or "source", piece, module) for piece in chunk_text(source, size)]
    lines = source.splitlines()
    out: list[Chunk] = []
    doc = ast.get_docstring(tree)
    if doc:
        out.append(Chunk(f"{module} (module)", doc[:size], module, 1))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            qual = f"{module}.{node.name}" if module else node.name
            text = _segment(lines, node)
            if len(text) <= size or not isinstance(node, ast.ClassDef):
                out.append(Chunk(qual, text[: size * 2], qual, node.lineno))
                continue
            head_end = node.body[0].lineno - 1 if node.body else node.lineno
            header = "\n".join(lines[node.lineno - 1 : head_end])
            cls_doc = ast.get_docstring(node) or ""
            out.append(Chunk(qual, (header + "\n" + cls_doc)[:size], qual, node.lineno))
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    member = f"{qual}.{item.name}"
                    out.append(
                        Chunk(member, _segment(lines, item)[: size * 2], member, item.lineno)
                    )
    return out


def docstring_summary(source: str, module: str = "") -> list[Chunk]:
    """Only docstrings and signatures (cheap index of large packages, no code bodies)."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    out: list[Chunk] = []
    doc = ast.get_docstring(tree)
    if doc:
        out.append(Chunk(module, doc[:1500], module, 1))
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            qual = f"{module}.{node.name}" if module else node.name
            bases = ", ".join(ast.unparse(b) for b in node.bases)
            parts = [f"class {node.name}({bases})", ast.get_docstring(node) or ""]
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                    parts.append(f"__init__{ast.unparse(item.args)}"[:1500])
            out.append(Chunk(qual, "\n".join(p for p in parts if p)[:3000], qual, node.lineno))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith(
            "_"
        ):
            qual = f"{module}.{node.name}" if module else node.name
            sig = f"def {node.name}({ast.unparse(node.args)})"
            out.append(
                Chunk(qual, f"{sig}\n{ast.get_docstring(node) or ''}"[:2000], qual, node.lineno)
            )
    return out


__all__ = ["Chunk", "chunk_markdown", "chunk_python", "chunk_text", "docstring_summary"]
