"""
Async BLE client for SPOTLED panels (bleak; works on macOS, Linux and Windows).

Mirrors the transport of the official app (LED / CommandExecutor /
BluetoothGattWrapper in com.led.spotled 1.9.4):

  * notifications are enabled on FF21 only (the CCCD is found by discovery, not
    a hard-coded handle - the cause of python-spotled's "Attribute can't be written")
  * short commands go to FF21, data frames go to FF22 in 182- or 20-byte chunks
    with a 5 ms gap
  * data commands are wrapped as START_SENDING -> data -> END_SENDING
  * flow control: whenever the absolute byte position is a multiple of the
    device's receive capacity, wait for a CONTINUE (0xFF) and resume from the
    offset the device reports; PAUSE (0xFE) also makes us wait
"""
from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass, field
from typing import Sequence

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

from . import protocol as p
from .font import GLYPH_H, GLYPH_W, load_font
from .protocol import Cmd, Effect, Pixel

log = logging.getLogger("spotled")

RESPONSE_TIMEOUT = 3.0     # app: 0xbb8 ms
FLOW_TIMEOUT = 10.0        # app: waitNotification 10000 ms
CHUNK_GAP = 0.005          # app: 5 ms between data chunks


class SpotLEDError(RuntimeError):
    pass


@dataclass
class DeviceState:
    version: p.VersionInfo | None = None
    screen: p.ScreenInfo | None = None
    capability: p.AnimTextCapability | None = None
    receive_bytes: int = 0
    chunk_size: int = p.SMALL_CHUNK
    extra: dict = field(default_factory=dict)

    @property
    def width(self) -> int:
        return self.screen.width if self.screen else 32

    @property
    def height(self) -> int:
        return self.screen.height if self.screen else 16

    @property
    def color_type(self) -> int:
        return self.screen.color_type if self.screen else p.ColorType.MONO

    @property
    def max_frames(self) -> int:
        if self.capability and self.capability.max_frames:
            return self.capability.max_frames
        return (self.screen.max_frames if self.screen else 0) or 20

    @property
    def max_chars(self) -> int:
        return (self.capability.max_chars if self.capability else 0) or 80


# Services a SPOTLED panel may advertise. On macOS the advertised name is often
# missing (only the GAP name, read after connecting, says SPOTLED_xxxx).
SPOTLED_ADV_UUIDS = {
    p.SERVICE_UUID,
    p.OTA_SERVICE_UUID,
    "0000ae30-0000-1000-8000-00805f9b34fb",
}


def _names(d: BLEDevice, ad) -> list[str]:
    return [n.upper() for n in (d.name, getattr(ad, "local_name", None)) if n]


def looks_like_spotled(d: BLEDevice, ad, name_prefix: str = "SPOTLED") -> bool:
    if any(n.startswith(name_prefix.upper()) for n in _names(d, ad)):
        return True
    uuids = {u.lower() for u in (getattr(ad, "service_uuids", None) or [])}
    uuids |= {u.lower() for u in (getattr(ad, "service_data", None) or {})}
    return bool(uuids & SPOTLED_ADV_UUIDS)


async def scan_all(timeout: float = 8.0) -> list[tuple[BLEDevice, object]]:
    seen = await BleakScanner.discover(timeout=timeout, return_adv=True)
    return sorted(seen.values(), key=lambda t: -(t[1].rssi or -999))


async def scan(timeout: float = 5.0, name_prefix: str = "SPOTLED") -> list[BLEDevice]:
    return [d for d, ad in await scan_all(timeout) if looks_like_spotled(d, ad, name_prefix)]


async def find_device(identifier: str | None = None, timeout: float = 10.0) -> BLEDevice:
    """identifier: address / CoreBluetooth UUID, a name (e.g. SPOTLED_3E11), or None for the first SPOTLED."""
    if identifier and sys.platform == "darwin" and identifier.count(":") == 5:
        raise SpotLEDError(
            f"{identifier} is a MAC address; macOS hides those. Use the name instead "
            "(e.g. -d SpotLED_3E11) or the UUID shown by `spotled scan --all`."
        )
    if identifier and (":" in identifier or "-" in identifier) and not identifier.upper().startswith("SPOTLED"):
        dev = await BleakScanner.find_device_by_address(identifier, timeout=timeout)
    elif identifier:
        target = identifier.upper()
        dev = await BleakScanner.find_device_by_filter(
            lambda d, ad: any(n.startswith(target) for n in _names(d, ad)), timeout=timeout
        )
    else:
        dev = await BleakScanner.find_device_by_filter(looks_like_spotled, timeout=timeout)
    if dev is None:
        raise SpotLEDError(
            f"no SPOTLED device found ({identifier or 'any'}). Is it on and not connected to the phone? "
            "Run `spotled scan --all` and pass the panel's address with -d."
        )
    return dev


class SpotLED:
    def __init__(self, device: BLEDevice | str, *, chunk_size: int | None = None):
        self._device = device
        self._client: BleakClient | None = None
        self._forced_chunk = chunk_size
        self._reasm = p.Reassembler()
        self._waiters: dict[int, asyncio.Future] = {}
        self._serial = 0
        self._sn = 0
        self._continue = asyncio.Event()
        self._continue_offset = 0
        self._paused = False
        self._abort = False
        self._lock = asyncio.Lock()
        self._write_cmd_response = False
        self._write_data_response = False
        self.state = DeviceState()

    # ------------------------------------------------------------------ setup
    @classmethod
    async def discover_and_connect(cls, identifier: str | None = None, **kw) -> "SpotLED":
        dev = await find_device(identifier)
        led = cls(dev, **kw)
        await led.connect()
        return led

    async def __aenter__(self) -> "SpotLED":
        if not self.connected:
            await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.disconnect()

    @property
    def connected(self) -> bool:
        return bool(self._client and self._client.is_connected)

    async def connect(self) -> DeviceState:
        self._client = BleakClient(self._device, timeout=15.0)
        await self._client.connect()
        svc = self._client.services.get_service(p.SERVICE_UUID)
        if svc is None:
            raise SpotLEDError("service FF20 not found - not a SPOTLED panel?")
        cmd = svc.get_characteristic(p.CMD_CHAR_UUID)
        data = svc.get_characteristic(p.DATA_CHAR_UUID)
        if cmd is None or data is None:
            raise SpotLEDError("characteristics FF21/FF22 not found")
        # Android's default write type: without-response if the characteristic supports it.
        self._write_cmd_response = "write-without-response" not in cmd.properties
        self._write_data_response = "write-without-response" not in data.properties
        self.state.chunk_size = self._forced_chunk or self._pick_chunk(data)
        log.debug("FF21 props=%s FF22 props=%s chunk=%d", cmd.properties, data.properties, self.state.chunk_size)

        await self._client.start_notify(p.CMD_CHAR_UUID, self._on_notify)

        # Same queries the app runs after connecting (LEDSetter$1 / $2)
        self.state.version = p.VersionInfo.parse(await self._query(Cmd.VERSION, Cmd.VERSION_RESP))
        self.state.screen = p.ScreenInfo.parse(await self._query(Cmd.SCREEN_INFO, Cmd.SCREEN_INFO_RESP))
        try:
            self.state.capability = p.AnimTextCapability.parse(
                await self._query(Cmd.ANIM_TEXT_CAPABILITY, Cmd.ANIM_TEXT_CAPABILITY_RESP)
            )
        except SpotLEDError as e:  # older firmware may not answer
            log.info("anim/text capability query failed: %s", e)
        self.state.receive_bytes = p.parse_capacity(await self._query(Cmd.CAPACITY, Cmd.CAPACITY_RESP))
        return self.state

    def _pick_chunk(self, data_char) -> int:
        mtu = getattr(self._client, "mtu_size", 23) or 23
        limit = mtu - 3
        mwr = getattr(data_char, "max_write_without_response_size", None)
        if mwr:
            limit = max(limit, mwr)
        return p.LARGE_CHUNK if limit >= p.LARGE_CHUNK else p.SMALL_CHUNK

    async def disconnect(self) -> None:
        if self._client and self._client.is_connected:
            try:
                await self._write_cmd(p.short_command(Cmd.DISCONNECT, serial=self._next_serial()))
                await asyncio.sleep(0.1)
            except Exception:  # noqa: BLE001 - best effort
                pass
            await self._client.disconnect()

    # ------------------------------------------------------------ transport
    def _next_serial(self) -> int:
        s = self._serial
        self._serial = (self._serial + 1) & 0x7F
        return s

    def _next_sn(self) -> int:
        s = self._sn
        self._sn = (self._sn + 2) & 0x7F
        return s

    def _on_notify(self, _char, data: bytearray) -> None:
        for frame in self._reasm.feed(bytes(data)):
            rid = p.response_id(frame)
            log.debug("<- %s", frame.hex(" "))
            if rid == Cmd.CONTINUE:
                self._continue_offset = p.parse_continue_offset(frame)
                self._paused = False
                self._continue.set()
            elif rid == Cmd.PAUSE:
                if len(frame) > 7 and frame[7] != 0:
                    self._abort = True
                    self._continue.set()
                else:
                    self._paused = True
            elif rid == Cmd.SCREEN_INFO_RESP and rid not in self._waiters and self.state.screen and len(frame) > 13:
                self.state.screen.light_up = frame[13]  # unsolicited screen-state update
            fut = self._waiters.pop(rid, None)
            if fut and not fut.done():
                fut.set_result(frame)

    async def _write_cmd(self, data: bytes) -> None:
        log.debug("-> FF21 %s", data.hex(" "))
        await self._client.write_gatt_char(p.CMD_CHAR_UUID, data, response=self._write_cmd_response)

    async def _expect(self, rid: int, send, timeout: float = RESPONSE_TIMEOUT) -> bytes:
        fut = asyncio.get_running_loop().create_future()
        self._waiters[rid] = fut
        try:
            await send()
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise SpotLEDError(f"timeout waiting for response 0x{rid:02x}") from None
        finally:
            self._waiters.pop(rid, None)

    async def _query(self, cmd_id: int, resp_id: int) -> bytes:
        async with self._lock:
            frame = p.short_command(cmd_id, serial=self._next_serial())
            r = await self._expect(resp_id, lambda: self._write_cmd(frame))
            if len(r) > 4 and r[4] != 0:
                raise SpotLEDError(f"command 0x{cmd_id:02x} failed, status {r[4]}")
            return r

    async def _write_data(self, data: bytes) -> None:
        cs = self.state.chunk_size
        rb = self.state.receive_bytes
        total = len(data)
        pos = 0
        self._continue.clear()
        self._paused = self._abort = False
        while pos < total:
            chunk = data[pos:pos + cs]
            await self._client.write_gatt_char(p.DATA_CHAR_UUID, chunk, response=self._write_data_response)
            pos += len(chunk)
            await asyncio.sleep(CHUNK_GAP)
            if (rb and pos % rb == 0) or self._paused:
                try:
                    await asyncio.wait_for(self._continue.wait(), FLOW_TIMEOUT)
                except asyncio.TimeoutError:
                    if pos >= total:
                        break  # everything is out; let END_SENDING report the result
                    raise SpotLEDError(f"device stopped acknowledging data at byte {pos}/{total}") from None
                self._continue.clear()
                if self._abort:
                    raise SpotLEDError("device aborted the transfer (PAUSE with error)")
                if self._continue_offset <= 0:
                    raise SpotLEDError("device reported invalid resume offset")
                pos = self._continue_offset
            elif self._abort:
                raise SpotLEDError("device aborted the transfer (PAUSE with error)")

    async def send_data_command(
        self, cmd_id: int, content: bytes, follow: bytes = b"", resp_id: int | None = None
    ) -> bytes | None:
        """START_SENDING -> data frame on FF22 (-> optional response) -> END_SENDING."""
        async with self._lock:
            frame = p.data_frame(cmd_id, content, follow, sn=self._next_sn())
            start = p.start_sending(self._next_serial(), cmd_id, len(frame))
            r = await self._expect(Cmd.START_SENDING_RESP, lambda: self._write_cmd(start))
            if r[4] != 0:
                raise SpotLEDError(f"device refused transfer (status {r[4]})")
            serial_echo = r[2:4]

            # Register the optional response first: it may arrive before or after END.
            resp_fut = None
            if resp_id is not None:
                resp_fut = asyncio.get_running_loop().create_future()
                self._waiters[resp_id] = resp_fut
            try:
                await self._write_data(frame)
                end = p.end_sending(serial_echo, cmd_id, len(frame))
                r = await self._expect(Cmd.END_SENDING_RESP, lambda: self._write_cmd(end))
                if r[4] != 0:
                    raise SpotLEDError(f"device rejected data (status {r[4]})")
                if resp_fut is None:
                    return None
                try:
                    return await asyncio.wait_for(resp_fut, RESPONSE_TIMEOUT)
                except asyncio.TimeoutError:
                    raise SpotLEDError(f"timeout waiting for response 0x{resp_id:04x}") from None
            finally:
                if resp_id is not None:
                    self._waiters.pop(resp_id, None)

    async def send_object(self, obj_bytes: bytes) -> None:
        """SetDataObject (0x8004): content = u32 object length, object follows."""
        await self.send_data_command(Cmd.SET_OBJECT, p.u32(len(obj_bytes)), obj_bytes)

    # --------------------------------------------------------------- features
    async def set_brightness(self, level: int) -> None:
        await self.send_object(p.brightness_obj(level))

    async def set_flip(self, mode: int | p.Flip) -> None:
        await self.send_object(p.flip_obj(int(mode)))

    async def set_playback_rate(self, rate: int | p.PlaybackRate) -> None:
        await self.send_object(p.playback_rate_obj(int(rate)))

    async def set_screen(self, on: bool) -> None:
        """DeviceControl 0x80FE: 1 = turn off, 2 = turn on (WorksFragment.playScreenSwitch)."""
        await self.send_data_command(Cmd.DEVICE_CONTROL, p.u16(2 if on else 1), resp_id=Cmd.DEVICE_CONTROL_RESP)

    async def show_bars(self, values: Sequence[int]) -> None:
        """Music-style histogram: usually 16 bars, each 0..panel height."""
        h = self.state.height
        await self.send_object(p.histogram_obj([max(0, min(h, int(v))) for v in values]))

    async def show_text(
        self,
        text: str,
        *,
        color: Pixel | Sequence[Pixel] = (255, 255, 255),
        effect: int | Effect = Effect.SCROLL_LEFT,
        interval_ms: int = 200,
        font_style: str = "regular",
    ) -> None:
        """
        Device-rendered text, exactly like the app: upload the 12x12 glyphs
        (WordStock) unless the device has a built-in font, then send the
        CharacterString with per-char colour, step interval and effect.
        interval_ms: the app's speed slider maps to 100..3400 ms (default 200).
        """
        text = text.replace("\n", " ")[: self.state.max_chars]
        if not text:
            raise ValueError("empty text")
        colors = list(color) if color and isinstance(color[0], (tuple, list)) else [tuple(color)] * len(text)
        if len(colors) < len(text):
            colors += [colors[-1]] * (len(text) - len(colors))

        if not (self.state.screen and self.state.screen.word_stock == 2):
            font = load_font(font_style)
            glyphs = [p.glyph_obj(ch, GLYPH_W, GLYPH_H, font.glyph(ch)) for ch in text]
            await self.send_object(p.word_stock_obj(glyphs))
        await self.send_object(p.character_string_obj(text, colors[: len(text)], interval_ms, int(effect)))

    async def show_frames(
        self,
        frames: Sequence[Sequence[Pixel]],
        *,
        interval_ms: int = 200,
        effect: int | Effect | None = Effect.STATIC,
    ) -> None:
        """Send one or more full-panel frames (row-major RGB). One frame = still image."""
        w, h, ct = self.state.width, self.state.height, self.state.color_type
        frames = list(frames)[: self.state.max_frames]
        encoded = [p.encode_frame(w, h, f, ct) for f in frames]
        await self.send_object(
            p.frame_animation_obj(encoded, min(1000, interval_ms), None if effect is None else int(effect))
        )

    async def query_object(self, obj_id: int) -> bytes:
        """QueryDeviceObject (0x8000), e.g. Obj.SCREEN_VISIBLE_POINT. Returns the raw response."""
        r = await self.send_data_command(Cmd.QUERY_OBJECT, p.u16(obj_id), resp_id=Cmd.QUERY_OBJECT_RESP)
        return r or b""
