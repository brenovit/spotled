# spotled webapp: plan

The goal is to control the **SpotLED_3E11** panel (32×16, full 24-bit colour) from a browser over Web Bluetooth, without the vendor APK.

The app is hosted on **Dokploy** behind **Cloudflare**. It has no backend and no gateway: the browser loads the static app over HTTPS and talks to the panel directly. The panel stays near the device you're using.

```
Cloudflare ─► Dokploy (static files)  ──load once──►  Chrome / Edge  ──Bluetooth──►  SpotLED_3E11
```

## Sources of truth

- Protocol spec: [`docs/PROTOCOL.md`](docs/PROTOCOL.md), recovered from SPOTLED APK 1.9.4.
- Working reference implementation, verified on the real panel: [`python/spotled_ble/`](python/spotled_ble/). Its parts:
  - `protocol.py`: encoders, checksum, frames, reply reassembler
  - `client.py`: the transfer sequence (Start → data → End) and flow control
  - `font.py`, `render.py`: glyphs, pixel encoding, image fitting
- Byte-exact test cases: [`python/tests/test_protocol.py`](python/tests/test_protocol.py)
- The decompiled app source is in `extracted/jadx/sources/com/led/spotled/` if anything is unclear.
- Don't copy `the archived spotled-web repo`: it carries the old python-spotled bugs (packet-count flow control, 1-byte flip, sending the SpeedObject).

## Panel facts (from `spotled info`)

| | |
|---|---|
| Screen | 32×16, colorType `0xFF` (24-bit RGB) |
| Limits | 20 frames, 160 characters |
| Font | `word_stock = 1`: no built-in font, so 12×12 glyphs must be uploaded with each text |
| Transfer window | `receive_bytes = 7280` |
| Writes | FF21 and FF22 are both write-without-response; FF21 notifies |

## Stack

- **Vite + Svelte 5 + TypeScript**, built to static files.
- **vitest** for tests.
- **Caddy** (or nginx) static container for Dokploy.

## Phases

### Phase 0: protocol in TypeScript
- `src/lib/protocol.ts`: a port of `protocol.py` covering IDs, enums, checksum, short, data and object frames, every object encoder, `encodeFrame` for all colour types, the `Reassembler`, and the reply parsers.
- `src/lib/transport.ts`: the transfer logic, written against a small interface `{ writeCmd, writeData, onNotify }` so it can be tested without Bluetooth:
  - Start → data → End
  - write in chunks with a 5 ms gap
  - wait for Continue when the absolute position is a multiple of `receive_bytes`
  - handle Pause
  - 3 s reply timeout, 10 s flow-control timeout
- **Done when:** the 13 hex test cases from `test_protocol.py` pass in vitest, plus a simulated-panel transfer test that crosses at least one Continue window.

### Phase 1: first working page (run locally)
- Web Bluetooth connection:
  - `navigator.bluetooth.requestDevice({ filters: [{ namePrefix: "SpotLED" }], optionalServices: [0xff20] })`
  - `startNotifications()` on FF21, then `writeValueWithoutResponse` on both characteristics
  - The same startup queries the app runs: version, screen info, anim/text capability, capacity
- Packet size: Web Bluetooth doesn't report the negotiated value. Try 182-byte chunks and fall back to 20 if a write throws or the panel sends Pause. Remember the result in `localStorage`.
- UI: Connect button, panel info, brightness slider, and text with colour, effect and speed.
- Font: load a glyph subset (see the font section) and send the WordStock before the CharacterString.
- **Done when:** Chrome on the Mac shows "Breno" on the panel.

### Phase 2: full controls
- 32×16 pixel-art editor: paint and erase, a colour picker, up to 20 frames, frame time, and a preview.
- Image and GIF upload: resize with a canvas (contain, cover or stretch). Decode GIFs with `ImageDecoder` and fall back to `gifuct-js`.
- Orientation (8 modes), screen on/off, playback rate (0.5×–3×), and bars.
- Per-letter colours in the text editor.
- Text drawn by the browser (proportional font, static pages), like `--render` in the CLI.
- **Done when:** everything the APK does for a single panel works from the browser.

### Phase 3: deploy
- Multi-stage Dockerfile: `vite build`, then Caddy serving `dist/`.
- Dokploy app pointed at this folder, with your domain proxied through Cloudflare (SSL mode "Full (strict)").
- Cloudflare Access policy so only you can open the app (this also keeps the vendor font private).
- PWA manifest and service worker, so the app can be installed and works offline after the first load.
- **Done when:** the app works from `https://<your-domain>` on the Mac (Chrome) and on Android Chrome.

### Later (not in the MVP)
- Saved favourites and presets (`localStorage`).
- An "AI draw this" button that generates pixel-art frames.
- Remote messaging through a gateway, if it's ever wanted. It builds on Phase 0 without rework.

## Font

- The panel needs 12×12 glyphs, 24 bytes each, uploaded with every text.
- The vendor font file (`Unicode_12x12_*.DZK`) is 1.5 MB.
- A build script slices it down to the character ranges actually used and writes `glyphs.bin` plus an index. Planned ranges:
  - ASCII
  - Latin-1 and Latin Extended-A (accents)
  - General Punctuation
  - Arrows
  - Box Drawing and Geometric Shapes
  - Miscellaneous Symbols
  - optionally CJK
- The result is about 100 KB.
- Keep the vendor file out of git and out of public hosting; Cloudflare Access covers the deployed subset.

## Limits

- Web Bluetooth only works in Chrome or Edge on desktop, and Chrome on Android. iOS browsers and Firefox can't use it.
- Connecting requires a user click each time the page is opened. Browsers don't allow silently reconnecting on page load.
- The panel accepts only one connection at a time. Close the phone app, and stop `spotled-mcp --http`, while using the webapp.
- Requires HTTPS or `localhost`. Cloudflare provides HTTPS in production.
