# spotled-ble (Python helper)

Python library, CLI and MCP server for SPOTLED Bluetooth LED panels. Uses [bleak](https://github.com/hbldh/bleak), so it runs on macOS, Linux and Windows. It is the reference implementation of [docs/PROTOCOL.md](../docs/PROTOCOL.md) and has been verified on a real 32×16 RGB panel.

## Install

```bash
cd python
uv venv && uv pip install -e '.[mcp,dev]'
```

The 12×12 text font belongs to the app vendor and is not in the repository. Extract it from your own copy of the SPOTLED APK or XAPK:

```bash
.venv/bin/python scripts/extract_fonts.py path/to/SPOTLED_1.9.4.xapk
```

## CLI

```bash
.venv/bin/spotled scan --all
```
```bash
.venv/bin/spotled -d SpotLED_3E11 info
```
```bash
.venv/bin/spotled -d SpotLED_3E11 text "Hello" -c cyan -e scroll_left -i 150
```
```bash
.venv/bin/spotled -d SpotLED_3E11 image cat.gif
```

Other commands: `pixels`, `brightness`, `flip`, `rate`, `power`, `bars`, `probe`. Add `-v` to log every BLE frame.

- On macOS, address the panel by name (`-d SpotLED_3E11`) or by the UUID from `scan --all`. macOS hides MAC addresses.
- The panel accepts one connection at a time, so close the phone app first.

## Python

```python
import asyncio
from spotled_ble import SpotLED, Effect

async def main():
    async with await SpotLED.discover_and_connect("SpotLED_3E11") as led:
        await led.set_brightness(50)
        await led.show_text("Hello!", color=(255, 80, 0), effect=Effect.SCROLL_LEFT)

asyncio.run(main())
```

## MCP server

See [Use with AI (MCP)](../README.md#use-with-ai-mcp) in the root README.

## Tests

```bash
.venv/bin/python -m pytest -q
```

Tests that need the font are skipped until you extract it.
