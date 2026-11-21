# SPOTLED BLE protocol

Recovered from the official Android app **SPOTLED 1.9.4** (`com.led.spotled`, `SPOTLED_1.9.4_APKPure.xapk`).
The APK was decoded with apktool to smali (`extracted/apktool/`). The sources are:

- transport: `smali/com/led/spotled/LED.smali`, `BluetoothGattWrapper.smali`, `cmd/Command.smali`, `cmd/CommandExecutor.smali`, `bytesdecoder/*`
- payloads: `cmd/object/*.smali`, `cmd/Constants.smali`
- composition: `smali_classes2/com/led/spotled/util/PlayUtil.smali`, `Utils.smali`

Tags: **[R]** = read directly in the smali. **[I]** = inferred.

The app has no native protocol code; its only `.so` is a GIF decoder.

## 1. GATT

| UUID | Role |
|---|---|
| `0000ff20-…` service | main service |
| `0000ff21-…` | short commands (write) **and every notification from the device** |
| `0000ff22-…` | data frames (write); never subscribed |
| `0000ff10-…` service, `ff11`/`ff12` | firmware OTA only |
| `0000ae30-…` | present on the device, but the app never uses it |

**Notifications** [R] (`BluetoothGattWrapper.smali:717-818`):
- The app calls `setCharacteristicNotification(FF21)`.
- It then writes `01 00` (`ENABLE_NOTIFICATION_VALUE`) to every descriptor of FF21. It writes `02 00` as well if FF21 can indicate.
- The descriptors are found by discovery, never by a fixed handle.

> This is why `python-spotled` failed with *"Attribute can't be written"*. It wrote 4 bytes `00 00 00 01` to a hard-coded handle `0x0f`, and on SPOTLED_3E11 that handle is some other attribute.

**MTU and chunking** [R]:
- The app requests MTU 202.
- If that succeeds and the result is ≥ 182, data is written in **182-byte** chunks. Otherwise it uses **20-byte** chunks.
- There is a 5 ms sleep between chunks.

**Write type** [R+I]: the app never calls `setWriteType`, so Android's default applies. That default is without-response if the characteristic supports it.

**Connect sequence** [R]: connect → MTU → discover → notify FF21 → then these queries:
- Version (0x10)
- ScreenInfo (0x12)
- AnimTextCapability (0x1A)
- QueryPlaylist
- QueryObject(0x10)
- Capacity (0x14)

## 2. Encoding

- **Integers** are big-endian. **Characters** are UTF-16BE code units.
- **Checksum** (`Utils.checkSum`): `s = Σ bytes`. The result is `s` if `s ≤ 0xFF`, otherwise `(-s) & 0xFF`.

**Short command** (written to FF21):
- Layout: `[len][id][content…]`, where `len = content + 2`.
- With no content: `04 id serialHi serialLo`.

**Data frame** (written to FF22):
- Layout: `[u32 len = content+11][u16 id][u32 SN][content][checksum]` + *follow*.
- *follow* is not counted in `len` and is not covered by the checksum.

**Object**:
- Layout: `[u32 len = content+7][u16 objId][content][checksum]` + *follow*, where *follow* holds the children.

## 3. Sending a data command

```
FF21  0A 01 [serial 2][cmdId 2][u32 total data-frame length]   START_SENDING
FF21  <-  .. 02 [serial 2][status]...                           status 0 = go, echo serial
FF22  data frame in 182/20-byte chunks                          (flow control below)
FF21  0A 03 [serial echo 2][cmdId 2][u32 length]               END_SENDING
FF21  <-  .. 04 [serial 2][status]                              status 0 = accepted
```

**Flow control** [R] (`CommandExecutor.write/checkWait`):
- After each chunk, if `receiveBytes ≠ 0` and `bytesSent % receiveBytes == 0`, the app waits up to 10 s.
- `receiveBytes` comes from the Capacity query (typically 1092).
- It is waiting for `.. FF [serial][cmd][u32 offset]` and then resumes from `offset`.
- If the device sends `.. FE …` (Pause): byte 7 = 0 means wait for a Continue; any other value aborts.

**Notification reassembly**:
- Frames can span several notifications.
- Short frames: the first byte is the total length.
- Long frames: the first 4 bytes are the length and bytes 4..5 hold the id. These are used by responses `8001 8009 800B 800D 800F 80FF`.

## 4. Commands

| id | Purpose | Request content | Response |
|---|---|---|---|
| 0x10 | version | – | 0x11: `[4]` status, `[5..6]` hwType, `[7..10]` hw, `[11..14]` fw |
| 0x12 | screen info | – | 0x13: `[5..6]` width, `[7..8]` height, `[9]` colorType, `[10]` maxFrames, `[11]` brightness, `[12]` wordStock (2 = has built-in font), `[13]` lightUp (newer firmware) |
| 0x14 | receive capacity | – | 0x15: `[5..8]` receiveBytes |
| 0x1A | anim/text limits | – | 0x1B: `[5..6]` frames, `[7..8]` chars, `[9..12]` storage |
| 0xED | disconnect | – | – |
| 0x8004 | **set object** | `u32 objectLen`; the object goes in *follow* | via END_SENDING |
| 0x8000 | query object | `u16 objId` | 0x8001 |
| 0x80FE | screen on/off | `u16`: 1 = off, 2 = on [R] (`WorksFragment.playScreenSwitch`: sends 1 when `lightUp == 1`, then marks the screen off) | 0x80FF |
| 0x8008/0x800A/0x800C/0x800E | playlist and material management | see the playlist section of the RE notes | 0x8009/0x800B/0x800D/0x800F |
| 0x50-0x53 | OTA (FF10 service) | – | – |

## 5. Objects (sent with 0x8004)

| id | Name | Content |
|---|---|---|
| 0x02 | Color | `R G B` |
| 0x03 | Text char | `u16` UTF-16 unit |
| 0x04 | CharacterString | `u16 count, u8 1`. Follow: (Color, Text) × N, then Time, then Effect |
| 0x05 | WordStock (glyph upload) | `u16 count`. Follow: Glyph × N |
| 0x06 | Lattice frame | `u16 w, u16 h`, RGB × w·h (colorType 0x31 only) |
| 0x07 | Time | `u8 0, u16 ms` (step or frame time) |
| 0x08 | Effect | `u8`: 0 static/slide, 1 scroll up, 2 scroll down, 3 scroll left, 4 scroll right, 5 snow, 6 reel, 7 laser |
| 0x0A | Histogram | `u16 n`, then `u16` × n (16 bars, each 0..height) |
| 0x0B | FrameAnimation | `u16 frames`. Follow: frames, then Time (≤ 1000 ms), then Effect |
| 0x0D | Glyph | `u8 1, u16 w(12), u16 h(12), u16 char, u8 n(24)`, bitmap (12 rows × 2 bytes, MSB = left) |
| 0x0E | Brightness | `u8` 0..100 |
| 0x0F | Screen flip | `u8 mode, 00, 00`. Modes: 00/01/02/03 = landscape with no flip, horizontal flip, vertical flip, both. 10/11/12/13 = the same for portrait |
| 0x17 | Playlist | `u64 id, u16 nameLen, name, u32 ts, 01 01 00, u16 n, n × 8-byte ids` |
| 0x20 | Playback rate | `u8`: 95 = 0.5×, 100 = 1×, 105 = 1.5×, 120 = 2×, 130 = 3× |
| 0x60 | Color frame | `u16 w, u16 h, u8 bpp`, then pixels |

**Pixel formats**: the frame layout is chosen by the device's `colorType`.

| colorType | Frame bytes |
|---|---|
| `0x10` mono | `bpp=1`, packed MSB-first over the whole row-major frame (padding only at the end) |
| `0x80` | `bpp=8`, `RRRGGBBB` |
| `0xFF` | `bpp=24`, RGB |
| `0xEE` | `bpp=16`, RGB565 with channels scaled as `r·31/255`, `g·63/255`, `b·31/255`. The app's byte packing writes 3 bytes per 2-byte slot and always throws on the last pixel, so the app cannot drive these panels. |
| `0x31` | object 0x06 |

**Text, as the app sends it**:
1. A WordStock with one 12×12 glyph per character, from `assets/Unicode_12x12_*.DZK`. It is skipped when `wordStock == 2`.
2. A CharacterString with per-character colours, Time = speed slider (100..3400 ms, default 200), and the Effect.

**Image / GIF**: a FrameAnimation of 1..maxFrames frames, then Time, then Effect.

## 6. Open questions

- What the CharacterString flag byte (always 1) means.
- What the Time type byte (always 0) means.
- What the playlist bytes `01 01 00` mean.
- The RGB565 byte layout is unverified (the app never successfully sends one).

Readable Java for everything above is in `extracted/jadx/sources/com/led/spotled/` (decompiled with jadx 1.5.7).
