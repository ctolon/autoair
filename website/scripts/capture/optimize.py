"""Shrinks the captured PNGs: 256-colour palette, no dithering (UI shots stay crisp).

    uv run --with pillow python scripts/capture/optimize.py <screens dir> [<videos dir>]

Only files written by the capture (static/img/screens/*-light.png / *-dark.png, except the
markdown-* and validate-* shots made elsewhere) are touched, and only when smaller.
Video posters (*.jpg) are left as written.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

from PIL import Image

SKIP = ("markdown-", "validate-")


def shrink(path: Path) -> tuple[int, int]:
    before = path.stat().st_size
    with Image.open(path) as im:
        if im.mode == "P":
            return before, before  # already a palette image (an earlier run)
        rgb = im.convert("RGB")
    pal = rgb.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    buf = io.BytesIO()
    pal.save(buf, format="PNG", optimize=True)
    if buf.tell() < before:
        path.write_bytes(buf.getvalue())
        return before, buf.tell()
    return before, before


def main() -> None:
    screens = Path(sys.argv[1])
    total_before = total_after = 0
    for path in sorted(screens.glob("*.png")):
        if path.name.startswith(SKIP) or not path.stem.endswith(("-light", "-dark")):
            continue
        before, after = shrink(path)
        total_before += before
        total_after += after
    print(f"• optimize: screens {total_before / 2**20:.1f} MB -> {total_after / 2**20:.1f} MB")


if __name__ == "__main__":
    main()
