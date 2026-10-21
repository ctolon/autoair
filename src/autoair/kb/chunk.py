"""Heading-aware chunking of parsed pages.

Every chunk stays inside one section (or a run of short sibling sections merged up to
``min_chars``), so its heading path and anchor say exactly where it comes from; long
sections are split on paragraphs with overlap (:func:`autoair.ai.chunking.chunk_text`).
Identical chunks of a page are kept once.
"""

from __future__ import annotations

import hashlib

from autoair.ai.chunking import chunk_text
from autoair.kb.config import KbChunkSettings
from autoair.kb.models import Chunk, PageDoc, Section


def _merge_short(sections: list[Section], min_chars: int, size: int) -> list[Section]:
    if min_chars <= 0:
        return sections
    out: list[Section] = []
    for section in sections:
        prev = out[-1] if out else None
        if (
            prev is not None
            and len(prev.text) < min_chars
            and len(prev.text) + len(section.text) + 2 <= size
        ):
            body = f"{prev.text}\n\n{section.heading.rsplit(' > ', 1)[-1]}\n{section.text}"
            out[-1] = prev.model_copy(update={"text": body.strip()})
            continue
        out.append(section)
    return out


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def chunk_page(page: PageDoc, settings: KbChunkSettings) -> list[Chunk]:
    chunks: list[Chunk] = []
    seen: set[str] = set()
    base_url = page.url.split("#", 1)[0]
    for section in _merge_short(page.sections, settings.min_chars, settings.size):
        for piece in chunk_text(section.text, settings.size, settings.overlap):
            piece = piece.strip()
            if not piece:
                continue
            digest = text_hash(piece)
            if digest in seen:
                continue
            seen.add(digest)
            n = len(chunks)
            chunks.append(
                Chunk(
                    id=f"{page.id}#{n}",
                    page_id=page.id,
                    n=n,
                    source=page.source,
                    component=page.component,
                    package=page.package,
                    version=page.version,
                    url=f"{base_url}#{section.anchor}" if section.anchor else base_url,
                    title=page.title,
                    heading=section.heading,
                    text=piece,
                    hash=digest,
                    updated=page.updated,
                )
            )
            if len(chunks) >= settings.max_chunks_per_page:
                return chunks
    return chunks


__all__ = ["chunk_page", "text_hash"]
