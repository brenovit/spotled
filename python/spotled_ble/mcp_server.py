"""
MCP server so an AI assistant (Claude Code / Claude Desktop / any MCP client)
can drive the panel.

    spotled-mcp                      # stdio transport
    SPOTLED_DEVICE=SPOTLED_3E11 spotled-mcp

Keeps one BLE connection open and reconnects on demand.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import asdict

from mcp.server.mcpserver import MCPServer

from . import protocol as p
from .client import SpotLED, SpotLEDError, scan
from .render import image_frames, parse_color, pixel_art, text_pixels

mcp = MCPServer(
    "spotled",
    instructions=(
        "Controls a SPOTLED Bluetooth LED matrix panel. Call panel_info first to learn the "
        "resolution (often 32x16 or 48x12) and whether it is monochrome. Use show_text for "
        "scrolling messages, show_pixel_art for drawings/icons (one string per row, '.' = off), "
        "show_image for files. Effects: static, scroll_up, scroll_down, scroll_left, "
        "scroll_right, snow, reel, laser."
    ),
)

_led: SpotLED | None = None
_lock = asyncio.Lock()


async def _get() -> SpotLED:
    global _led
    async with _lock:
        if _led is None or not _led.connected:
            _led = await SpotLED.discover_and_connect(os.environ.get("SPOTLED_DEVICE") or None)
        return _led


def _effect(name: str) -> p.Effect:
    try:
        return p.Effect[name.upper().replace("-", "_")]
    except KeyError:
        raise ValueError(f"unknown effect {name!r}; choose from {[e.name.lower() for e in p.Effect]}")


@mcp.tool()
async def find_panels(timeout_s: float = 5.0) -> list[dict]:
    """Scan for nearby SPOTLED panels (name and BLE address)."""
    return [{"name": d.name, "address": d.address} for d in await scan(timeout_s)]


@mcp.tool()
async def panel_info() -> dict:
    """Connect (if needed) and return resolution, colour type, firmware, max frames/chars."""
    led = await _get()
    s = led.state
    return {
        "width": s.width,
        "height": s.height,
        "monochrome": s.color_type == p.ColorType.MONO,
        "color_type": hex(s.color_type),
        "max_frames": s.max_frames,
        "max_chars": s.max_chars,
        "brightness": s.screen.brightness if s.screen else None,
        "version": asdict(s.version) if s.version else None,
    }


@mcp.tool()
async def show_text(text: str, color: str = "white", effect: str = "scroll_left", step_ms: int = 200,
                    host_rendered: bool = False) -> str:
    """
    Show text. effect: static|scroll_up|scroll_down|scroll_left|scroll_right|snow|reel|laser.
    step_ms: animation step / page time (100..3400, smaller = faster).
    host_rendered=True renders with proportional spacing and word-wrapped pages on the computer
    (good for short static words); False uses the panel's own text engine (best for long scrolling text).
    """
    led = await _get()
    if host_rendered:
        frames = text_pixels(text, led.state.width, led.state.height, parse_color(color))
        await led.show_frames(frames, interval_ms=step_ms, effect=_effect(effect))
    else:
        await led.show_text(text, color=parse_color(color), effect=_effect(effect), interval_ms=step_ms)
    return "ok"


@mcp.tool()
async def show_pixel_art(frames: list[list[str]], palette: dict[str, str] | None = None,
                         frame_ms: int = 300, effect: str = "static") -> str:
    """
    Draw pixel art. frames: list of frames, each a list of row strings (one char per pixel,
    '.' or ' ' = off). palette maps chars to colours ("red", "#ff8800"); unmapped chars are white.
    Several frames make an animation (frame_ms each, max 1000). Call panel_info for the size.
    """
    led = await _get()
    w, h = led.state.width, led.state.height
    await led.show_frames([pixel_art(rows, w, h, palette) for rows in frames], interval_ms=frame_ms,
                          effect=_effect(effect))
    return "ok"


@mcp.tool()
async def show_image(path: str, effect: str = "static", fit: str = "contain") -> str:
    """Show a local image or animated GIF file, resized to the panel. fit: contain|cover|stretch."""
    led = await _get()
    s = led.state
    frames, dur = image_frames(path, s.width, s.height, max_frames=s.max_frames, fit=fit,
                               mono=s.color_type == p.ColorType.MONO)
    await led.show_frames(frames, interval_ms=dur, effect=_effect(effect))
    return f"ok ({len(frames)} frame(s))"


@mcp.tool()
async def set_brightness(level: int) -> str:
    """Brightness 0..100."""
    await (await _get()).set_brightness(level)
    return "ok"


@mcp.tool()
async def set_orientation(mode: str = "landscape") -> str:
    """mode: landscape, landscape_flip_h, landscape_flip_v, landscape_flip_hv, portrait, portrait_flip_h, ..."""
    await (await _get()).set_flip(p.Flip[mode.upper()])
    return "ok"


@mcp.tool()
async def set_speed(rate: str = "x1_0") -> str:
    """Global playback rate: x0_5, x1_0, x1_5, x2_0, x3_0."""
    await (await _get()).set_playback_rate(p.PlaybackRate[rate.upper()])
    return "ok"


@mcp.tool()
async def set_screen(on: bool) -> str:
    """Turn the display on or off."""
    await (await _get()).set_screen(on)
    return "ok"


@mcp.tool()
async def show_bars(values: list[int]) -> str:
    """Bar graph / music visualiser: usually 16 values, each 0..panel height."""
    await (await _get()).show_bars(values)
    return "ok"


@mcp.tool()
async def disconnect() -> str:
    """Release the Bluetooth connection (so the phone app can connect again)."""
    global _led
    if _led:
        await _led.disconnect()
        _led = None
    return "ok"


def main() -> None:
    """
    stdio (default): launched by the MCP client.
    --http [--port N]: run from a terminal that has Bluetooth permission. Needed on
    macOS with Claude Code, because a Bluetooth process spawned by the `claude`
    binary is killed by TCC (no NSBluetoothAlwaysUsageDescription). Register with:
        claude mcp add --transport http spotled http://127.0.0.1:8765/mcp
    """
    import argparse

    ap = argparse.ArgumentParser(prog="spotled-mcp")
    ap.add_argument("--http", action="store_true", help="serve streamable HTTP on 127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    try:
        if args.http:
            print(f"spotled MCP on http://127.0.0.1:{args.port}/mcp  (Ctrl-C to stop)", flush=True)
            mcp.run("streamable-http", host="127.0.0.1", port=args.port)
        else:
            mcp.run("stdio")
    except SpotLEDError as e:
        raise SystemExit(str(e))


if __name__ == "__main__":
    main()
