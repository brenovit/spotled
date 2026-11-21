# spotled

Control **SPOTLED** Bluetooth LED panels (for example `SpotLED_3E11`, 32×16 RGB) without the vendor's Android app.

The BLE protocol was recovered from the official app (SPOTLED 1.9.4) and is documented in [docs/PROTOCOL.md](docs/PROTOCOL.md).

| Path | What |
|---|---|
| `/` (root) | The main project: a browser webapp that talks to the panel over Web Bluetooth. Work in progress; see [PLAN.md](PLAN.md). |
| [`python/`](python/) | Helper Python library, CLI and MCP server (bleak). Working and verified on hardware. |
| [`docs/PROTOCOL.md`](docs/PROTOCOL.md) | Protocol specification. |

This project replaces the archived [python-spotled](https://github.com/brenovit/python-spotled) and [spotled-web](https://github.com/brenovit/spotled-web) repositories. Those had a wrong notification setup and broken flow control.

> Not affiliated with the SPOTLED vendor. The vendor APK and its fonts are not included; extract the font from your own copy (see [python/README.md](python/README.md)).

## Quick start (Python CLI)

```bash
cd python && uv venv && uv pip install -e '.[mcp,dev]'
```
```bash
.venv/bin/python scripts/extract_fonts.py path/to/SPOTLED.xapk
```
```bash
.venv/bin/spotled -d SpotLED_3E11 text "Hello" -c cyan
```

Before connecting:
- Close the SPOTLED phone app. The panel accepts one connection at a time and stops advertising while connected.
- On macOS, your terminal needs Bluetooth permission: System Settings → Privacy & Security → Bluetooth.

## Use with AI (MCP)

The MCP server gives an AI assistant these tools: `find_panels`, `panel_info`, `show_text`, `show_pixel_art`, `show_image`, `set_brightness`, `set_orientation`, `set_speed`, `set_screen`, `show_bars`, `disconnect`.

Below, `<repo>` is the absolute path of your clone.

### Which mode?

| | How it runs | Use when |
|---|---|---|
| **HTTP** (recommended on macOS) | You start the server in your own terminal; the AI client connects to `http://127.0.0.1:8765/mcp` | macOS. Claude Code and Claude Desktop cannot be granted Bluetooth themselves, so macOS kills a Bluetooth server they launch (the client shows "Connection closed"; the crash report says TCC, NSBluetoothAlwaysUsageDescription). |
| **stdio** | The AI client launches the server itself | Linux and Windows, or any client process that has Bluetooth access. |

### 1. Start the server (HTTP mode)

Run this in a terminal app that has Bluetooth permission, and keep it open:

```bash
SPOTLED_DEVICE=SpotLED_3E11 <repo>/python/.venv/bin/spotled-mcp --http
```

- It listens only on `127.0.0.1:8765`. Change the port with `--port`.
- `SPOTLED_DEVICE` is optional. Without it, the server uses the first SPOTLED panel it finds.

### 2a. Claude Code

HTTP mode:

```bash
claude mcp add --transport http spotled http://127.0.0.1:8765/mcp
```

stdio mode (Linux/Windows):

```bash
claude mcp add spotled -e SPOTLED_DEVICE=SpotLED_3E11 -- <repo>/python/.venv/bin/spotled-mcp
```

Then start a new Claude Code session. Configuration is read when a session starts. Check the registration with `claude mcp get spotled`.

### 2b. Claude Desktop (`claude_desktop_config.json`)

Open the config file from Claude Desktop → Settings → Developer → Edit Config, or directly:
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

**HTTP mode** (macOS). Claude Desktop launches stdio commands, so bridge to the local HTTP server with [`mcp-remote`](https://www.npmjs.com/package/mcp-remote) (needs Node.js):

```json
{
  "mcpServers": {
    "spotled": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://127.0.0.1:8765/mcp"]
    }
  }
}
```

**stdio mode** (Linux/Windows):

```json
{
  "mcpServers": {
    "spotled": {
      "command": "<repo>/python/.venv/bin/spotled-mcp",
      "env": { "SPOTLED_DEVICE": "SpotLED_3E11" }
    }
  }
}
```

On Windows, the command path is `<repo>\\python\\.venv\\Scripts\\spotled-mcp.exe`.

Fully quit and reopen Claude Desktop after editing the file. "spotled" then appears in the tools menu.

### 3. Try it

> "Show 'hello' scrolling left in cyan on my LED panel"
> "Draw a pixel-art heart, then set brightness to 30"

To use the phone app again, stop the server (Ctrl-C) or ask the AI to call `disconnect`.

### Troubleshooting

| Symptom | Fix |
|---|---|
| "Connection closed" right after a tool call (macOS) | You are in stdio mode; switch to HTTP mode. |
| "no SPOTLED device found" | Close the phone app, then run `spotled scan --all` to check the panel is advertising. |
| Tools missing after registering | Start a new session, or restart Claude Desktop. |
| Text fails with a font error | Extract the font: `python scripts/extract_fonts.py <apk>`. |

## Webapp (in progress)

A static Vite + Svelte site in this root folder. It talks to the panel from Chrome or Edge via Web Bluetooth and is deployed on Dokploy behind Cloudflare. See [PLAN.md](PLAN.md).
