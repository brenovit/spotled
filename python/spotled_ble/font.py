"""
Loader for the app's DZK bitmap fonts (assets/Unicode_12x12_*.DZK).

A DZK file is 65536 glyphs x 24 bytes, indexed by UTF-16 code unit. Each glyph
is 12 rows of 2 bytes, MSB = leftmost pixel (only the top 12 bits are used).
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

GLYPH_W = 12
GLYPH_H = 12
GLYPH_BYTES = 24
ROW_BYTES = 2

FONT_DIR = Path(__file__).parent / "fonts"
STYLES = {
    "regular": "Unicode_12x12_FontSize9_MsSong_Mode1_124.DZK",
    "bold": "Unicode_12x12_FontSize9_MsSong_Mode1_124_Bold.DZK",
    "italic": "Unicode_12x12_FontSize9_MsSong_Mode1_124_Italics.DZK",
    "bold-italic": "Unicode_12x12_FontSize9_MsSong_Mode1_124_BoldItalics.DZK",
}


class DzkFont:
    def __init__(self, path: str | os.PathLike):
        self.data = Path(path).read_bytes()
        if len(self.data) != 65536 * GLYPH_BYTES:
            raise ValueError(f"{path} is not a 12x12 DZK font")

    def glyph(self, ch: str) -> bytes:
        code = ord(ch)
        if code > 0xFFFF:
            code = ord("?")
        return self.data[code * GLYPH_BYTES:(code + 1) * GLYPH_BYTES]

    def pixel(self, ch: str, x: int, y: int) -> bool:
        g = self.glyph(ch)
        return bool(g[y * ROW_BYTES + x // 8] & (0x80 >> (x % 8)))

    def ink_width(self, ch: str) -> int:
        """Rightmost lit column + 1 (0 for blank glyphs like space)."""
        w = 0
        for y in range(GLYPH_H):
            for x in range(GLYPH_W):
                if self.pixel(ch, x, y):
                    w = max(w, x + 1)
        return w


@lru_cache(maxsize=4)
def load_font(style: str = "regular") -> DzkFont:
    override = os.environ.get("SPOTLED_FONT")
    if override:
        return DzkFont(override)
    path = FONT_DIR / STYLES[style]
    if not path.exists():
        raise FileNotFoundError(
            f"font {path.name} not found. The vendor fonts are not in the repo; run "
            "`python scripts/extract_fonts.py <SPOTLED .apk or .xapk>` or set SPOTLED_FONT."
        )
    return DzkFont(path)
