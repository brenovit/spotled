"""
Turn images, GIFs, ASCII pixel art and text into RGB pixel lists for the panel.
"""
from __future__ import annotations

from typing import Sequence

from .font import GLYPH_H, load_font
from .protocol import Pixel

BLACK: Pixel = (0, 0, 0)
WHITE: Pixel = (255, 255, 255)

NAMED_COLORS: dict[str, Pixel] = {
    "white": (255, 255, 255), "black": (0, 0, 0), "red": (255, 0, 0),
    "green": (0, 255, 0), "blue": (0, 0, 255), "yellow": (255, 255, 0),
    "cyan": (0, 255, 255), "magenta": (255, 0, 255), "orange": (255, 128, 0),
    "purple": (128, 0, 255), "pink": (255, 100, 180),
}


def parse_color(value: str | Sequence[int]) -> Pixel:
    if not isinstance(value, str):
        r, g, b = value
        return (int(r), int(g), int(b))
    v = value.strip().lower()
    if v in NAMED_COLORS:
        return NAMED_COLORS[v]
    v = v.lstrip("#")
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    if len(v) != 6:
        raise ValueError(f"bad colour {value!r}; use a name, #rgb or #rrggbb")
    return (int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16))


def blank(width: int, height: int) -> list[Pixel]:
    return [BLACK] * (width * height)


# ----------------------------------------------------------------------------
# Pixel art
# ----------------------------------------------------------------------------
def pixel_art(
    rows: Sequence[str],
    width: int,
    height: int,
    palette: dict[str, str | Sequence[int]] | None = None,
) -> list[Pixel]:
    """
    rows: strings, one per panel row. '.' or ' ' is off; any other char looks up
    `palette` (default: every other char is white). Rows/cols beyond the panel are
    cropped; missing ones are off.
    """
    pal = {k: parse_color(v) for k, v in (palette or {}).items()}
    out = blank(width, height)
    for y, row in enumerate(rows[:height]):
        for x, ch in enumerate(row[:width]):
            if ch in ". ":
                continue
            out[y * width + x] = pal.get(ch, WHITE)
    return out


# ----------------------------------------------------------------------------
# Images
# ----------------------------------------------------------------------------
def _fit(img, width: int, height: int, mode: str):
    from PIL import Image

    img = img.convert("RGB")
    if mode == "stretch":
        return img.resize((width, height), Image.LANCZOS)
    scale = (min if mode == "contain" else max)(width / img.width, height / img.height)
    w, h = max(1, round(img.width * scale)), max(1, round(img.height * scale))
    img = img.resize((w, h), Image.LANCZOS)
    canvas = Image.new("RGB", (width, height))
    canvas.paste(img, ((width - w) // 2, (height - h) // 2))
    return canvas


def _to_pixels(img, mono: bool, threshold: int) -> list[Pixel]:
    px = list(img.getdata())
    if not mono:
        return px
    # Mono panels light any non-zero pixel; threshold on luminance so photos stay readable.
    return [WHITE if (r * 299 + g * 587 + b * 114) // 1000 >= threshold else BLACK for r, g, b in px]


def image_frames(
    path: str,
    width: int,
    height: int,
    *,
    max_frames: int = 20,
    fit: str = "contain",
    mono: bool = False,
    threshold: int = 128,
) -> tuple[list[list[Pixel]], int]:
    """Load a still image or animated GIF. Returns (frames, frame_duration_ms)."""
    from PIL import Image, ImageSequence

    img = Image.open(path)
    frames: list[list[Pixel]] = []
    durations: list[int] = []
    for frame in ImageSequence.Iterator(img):
        frames.append(_to_pixels(_fit(frame, width, height, fit), mono, threshold))
        durations.append(int(frame.info.get("duration", 200) or 200))
        if len(frames) >= max_frames:
            break
    duration = sum(durations) // len(durations) if durations else 200
    return frames, duration


# ----------------------------------------------------------------------------
# Text rasterised on the host (for pixel-exact static text / pages)
# ----------------------------------------------------------------------------
def text_pixels(
    text: str,
    width: int,
    height: int,
    color: Pixel = WHITE,
    *,
    style: str = "regular",
    spacing: int = 1,
    align: str = "center",
) -> list[list[Pixel]]:
    """
    Render text with the app's 12x12 font using proportional spacing and split it
    into panel-sized pages (one page per frame). Lines are separated by '\\n'.
    Each page holds as many lines as fit vertically.
    """
    font = load_font(style)
    space_w = 4

    def line_cols(line: str) -> list[list[bool]]:
        cols: list[list[bool]] = []
        for ch in line:
            w = font.ink_width(ch) or space_w
            for x in range(w):
                cols.append([font.pixel(ch, x, y) for y in range(GLYPH_H)])
            cols.extend([[False] * GLYPH_H for _ in range(spacing)])
        return cols[:-spacing] if spacing and cols else cols

    # word-wrap each paragraph to the panel width
    lines: list[list[list[bool]]] = []
    for para in text.split("\n"):
        current = ""
        for word in para.split(" "):
            cand = f"{current} {word}" if current else word
            if current and len(line_cols(cand)) > width:
                lines.append(line_cols(current))
                current = word
            else:
                current = cand
        lines.append(line_cols(current))

    per_page = max(1, height // GLYPH_H)
    pages: list[list[Pixel]] = []
    for i in range(0, len(lines), per_page):
        page = blank(width, height)
        group = lines[i:i + per_page]
        top = (height - len(group) * GLYPH_H) // 2
        for li, cols in enumerate(group):
            cols = cols[:width]
            if align == "center":
                left = (width - len(cols)) // 2
            elif align == "right":
                left = width - len(cols)
            else:
                left = 0
            for x, col in enumerate(cols):
                for y, on in enumerate(col):
                    yy = top + li * GLYPH_H + y
                    if on and 0 <= yy < height:
                        page[yy * width + left + x] = color
        pages.append(page)
    return pages or [blank(width, height)]
