"""Command line: `spotled <command> ...` (run `spotled -h`)."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import asdict

from bleak import BleakClient

from . import protocol as p
from .client import SpotLED, SpotLEDError, looks_like_spotled, scan, scan_all
from .render import image_frames, parse_color, pixel_art, text_pixels


def _enum(enum_cls):
    def conv(v: str):
        try:
            return enum_cls[v.upper().replace("-", "_")]
        except KeyError:
            return enum_cls(int(v, 0))
    return conv


async def _run(args) -> None:
    if args.cmd == "scan":
        if args.all:
            for d, ad in await scan_all(args.timeout):
                mark = "*" if looks_like_spotled(d, ad) else " "
                mfr = {f"0x{k:04x}": v.hex() for k, v in ad.manufacturer_data.items()}
                print(f"{mark} {ad.rssi:4}  {d.address}  name={d.name or ad.local_name!r}  "
                      f"uuids={ad.service_uuids}  mfr={mfr}  sdata={ {k: v.hex() for k, v in ad.service_data.items()} }")
            print("\n* = looks like a SPOTLED panel. Try `spotled -d <address> info` on unnamed devices "
                  "near the top (strongest signal) if nothing is marked.")
            return
        for d in await scan(args.timeout):
            print(f"{d.name}\t{d.address}")
        return

    if args.cmd == "probe":
        for d, ad in await scan_all(args.timeout):
            if d.name or ad.local_name or (ad.rssi or -999) < args.min_rssi:
                continue
            try:
                async with BleakClient(d, timeout=8.0) as c:
                    if c.services.get_service(p.SERVICE_UUID):
                        print(f"FOUND  {d.address}  (has service FF20)  rssi={ad.rssi}")
                        return
                    print(f"no     {d.address}  rssi={ad.rssi}")
            except Exception as e:  # noqa: BLE001
                print(f"skip   {d.address}  ({type(e).__name__})")
        print("no panel found among unnamed connectable devices")
        return

    async with await SpotLED.discover_and_connect(args.device, chunk_size=args.chunk) as led:
        s = led.state
        if args.cmd == "info":
            print(json.dumps({
                "version": asdict(s.version) if s.version else None,
                "screen": asdict(s.screen) if s.screen else None,
                "capability": asdict(s.capability) if s.capability else None,
                "receive_bytes": s.receive_bytes,
                "chunk_size": s.chunk_size,
            }, indent=2))
        elif args.cmd == "text":
            if args.render:
                frames = text_pixels(args.text, s.width, s.height, parse_color(args.color), style=args.style)
                await led.show_frames(frames, interval_ms=args.interval, effect=args.effect)
            else:
                await led.show_text(args.text, color=parse_color(args.color), effect=args.effect,
                                    interval_ms=args.interval, font_style=args.style)
        elif args.cmd == "image":
            frames, dur = image_frames(args.path, s.width, s.height, max_frames=s.max_frames, fit=args.fit,
                                       mono=s.color_type == p.ColorType.MONO, threshold=args.threshold)
            await led.show_frames(frames, interval_ms=args.interval or dur, effect=args.effect)
        elif args.cmd == "pixels":
            rows = open(args.file).read().splitlines() if args.file != "-" else sys.stdin.read().splitlines()
            palette = json.loads(args.palette) if args.palette else None
            await led.show_frames([pixel_art(rows, s.width, s.height, palette)], effect=args.effect)
        elif args.cmd == "brightness":
            await led.set_brightness(args.level)
        elif args.cmd == "flip":
            await led.set_flip(args.mode)
        elif args.cmd == "rate":
            await led.set_playback_rate(args.rate)
        elif args.cmd == "power":
            await led.set_screen(args.state == "on")
        elif args.cmd == "bars":
            await led.show_bars(args.values)
        print("ok")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="spotled", description="Control SPOTLED Bluetooth LED panels")
    ap.add_argument("-d", "--device", help="name (SPOTLED_3E11), address/UUID; default: first SPOTLED found")
    ap.add_argument("--chunk", type=int, help="force BLE chunk size (182 or 20)")
    ap.add_argument("-v", "--verbose", action="store_true", help="log every frame")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sc = sub.add_parser("scan", help="list nearby panels (--all: every BLE device with its advertisement)")
    sc.add_argument("--timeout", type=float, default=8.0)
    sc.add_argument("--all", action="store_true")
    pr = sub.add_parser("probe", help="connect to nearby unnamed devices until one has the SPOTLED service")
    pr.add_argument("--timeout", type=float, default=8.0)
    pr.add_argument("--min-rssi", type=int, default=-80, help="skip weaker devices (default -80)")
    sub.add_parser("info", help="print screen size, colour type, firmware, limits")

    effects = ", ".join(e.name.lower() for e in p.Effect)
    t = sub.add_parser("text", help="show text")
    t.add_argument("text")
    t.add_argument("-c", "--color", default="white")
    t.add_argument("-e", "--effect", type=_enum(p.Effect), default=p.Effect.SCROLL_LEFT, help=effects)
    t.add_argument("-i", "--interval", type=int, default=200, help="step/frame time in ms (app: 100..3400)")
    t.add_argument("--style", default="regular", choices=["regular", "bold", "italic", "bold-italic"])
    t.add_argument("--render", action="store_true",
                   help="rasterise on the computer (proportional font, word-wrapped pages) instead of device text")

    im = sub.add_parser("image", help="show an image or animated GIF")
    im.add_argument("path")
    im.add_argument("-e", "--effect", type=_enum(p.Effect), default=p.Effect.STATIC, help=effects)
    im.add_argument("-i", "--interval", type=int, help="frame time ms (default: GIF timing, max 1000)")
    im.add_argument("--fit", choices=["contain", "cover", "stretch"], default="contain")
    im.add_argument("--threshold", type=int, default=128, help="mono panels: luminance cut-off")

    px = sub.add_parser("pixels", help="show ASCII pixel art from a file ('-' = stdin)")
    px.add_argument("file")
    px.add_argument("--palette", help='JSON map char->colour, e.g. {"r":"red","g":"#00ff00"}')
    px.add_argument("-e", "--effect", type=_enum(p.Effect), default=p.Effect.STATIC)

    b = sub.add_parser("brightness", help="0..100")
    b.add_argument("level", type=int)
    f = sub.add_parser("flip", help=", ".join(m.name.lower() for m in p.Flip))
    f.add_argument("mode", type=_enum(p.Flip))
    r = sub.add_parser("rate", help="global playback rate: " + ", ".join(m.name for m in p.PlaybackRate))
    r.add_argument("rate", type=_enum(p.PlaybackRate))
    pw = sub.add_parser("power", help="screen on/off")
    pw.add_argument("state", choices=["on", "off"])
    bars = sub.add_parser("bars", help="histogram, e.g. `bars 1 4 8 12 ...` (16 values)")
    bars.add_argument("values", type=int, nargs="+")

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(message)s")
    if args.verbose:
        logging.getLogger("spotled").setLevel(logging.DEBUG)  # our frames only, not bleak's scan spam
    try:
        asyncio.run(_run(args))
    except SpotLEDError as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
