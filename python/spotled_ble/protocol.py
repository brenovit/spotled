"""
Pure (no Bluetooth) encoders/decoders for the SPOTLED BLE protocol.

Everything here was recovered from the official Android app
(com.led.spotled 1.9.4). See docs/PROTOCOL.md for the full write-up and the
smali locations each fact was read from.

All integers are big-endian.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Iterable, Sequence

# --------------------------------------------------------------------------
# GATT layout
# --------------------------------------------------------------------------
SERVICE_UUID = "0000ff20-0000-1000-8000-00805f9b34fb"
CMD_CHAR_UUID = "0000ff21-0000-1000-8000-00805f9b34fb"   # short commands + ALL notifications
DATA_CHAR_UUID = "0000ff22-0000-1000-8000-00805f9b34fb"  # data frames (objects)
OTA_SERVICE_UUID = "0000ff10-0000-1000-8000-00805f9b34fb"  # firmware update only - not used here

APP_REQUESTED_MTU = 202
LARGE_CHUNK = 182   # chunk size the app uses when the MTU request succeeds
SMALL_CHUNK = 20    # fallback chunk size


# --------------------------------------------------------------------------
# Command and object ids
# --------------------------------------------------------------------------
class Cmd(IntEnum):
    # short commands on FF21 (response id = request id + 1)
    START_SENDING = 0x01
    START_SENDING_RESP = 0x02
    END_SENDING = 0x03
    END_SENDING_RESP = 0x04
    RECIPIENT_STATUS = 0x05
    RECIPIENT_STATUS_RESP = 0x06
    VERSION = 0x10
    VERSION_RESP = 0x11
    SCREEN_INFO = 0x12
    SCREEN_INFO_RESP = 0x13
    CAPACITY = 0x14
    CAPACITY_RESP = 0x15
    ANIM_TEXT_CAPABILITY = 0x1A
    ANIM_TEXT_CAPABILITY_RESP = 0x1B
    DISCONNECT = 0xED
    PAUSE = 0xFE       # device -> app
    CONTINUE = 0xFF    # device -> app

    # data commands on FF22 (wrapped by START_SENDING / END_SENDING)
    QUERY_OBJECT = 0x8000
    QUERY_OBJECT_RESP = 0x8001
    SET_OBJECT = 0x8004
    PLAYLIST_MATERIAL = 0x8008
    PLAYLIST_MATERIAL_RESP = 0x8009
    PLAYLIST_OP = 0x800A
    PLAYLIST_OP_RESP = 0x800B
    QUERY_PLAYLIST = 0x800C
    QUERY_PLAYLIST_RESP = 0x800D
    QUERY_MATERIAL = 0x800E
    QUERY_MATERIAL_RESP = 0x800F
    DEVICE_CONTROL = 0x80FE
    DEVICE_CONTROL_RESP = 0x80FF


# Responses that use a 4-byte length prefix (id at bytes 4..5)
LONG_RESPONSE_IDS = {
    Cmd.QUERY_OBJECT_RESP, Cmd.PLAYLIST_MATERIAL_RESP, Cmd.PLAYLIST_OP_RESP,
    Cmd.QUERY_PLAYLIST_RESP, Cmd.QUERY_MATERIAL_RESP, Cmd.DEVICE_CONTROL_RESP,
}


class Obj(IntEnum):
    COLOR = 0x02
    TEXT_CHAR = 0x03
    CHARACTER_STRING = 0x04
    WORD_STOCK = 0x05
    LATTICE_FRAME = 0x06       # RGB frame without bpp byte (colorType 0x31 devices)
    TIME = 0x07
    EFFECT = 0x08
    SPEED = 0x09               # defined but never sent by the app
    HISTOGRAM = 0x0A
    FRAME_ANIMATION = 0x0B
    FONT_GLYPH = 0x0D
    BRIGHTNESS = 0x0E
    SCREEN_FLIP = 0x0F
    SCREEN_VISIBLE_POINT = 0x10
    PLAYLIST = 0x17
    PLAYBACK_RATE = 0x20
    COLOR_FRAME = 0x60         # frame with bpp byte (default)


class Effect(IntEnum):
    """Values of the 0x08 effect object. Names follow the app's UI strings."""
    STATIC = 0          # app label "Slide" (slideshow: frames shown one after another)
    SCROLL_UP = 1
    SCROLL_DOWN = 2
    SCROLL_LEFT = 3
    SCROLL_RIGHT = 4
    SNOW = 5
    REEL = 6            # app label "Scroll" (unroll / reel)
    LASER = 7


class Flip(IntEnum):
    """Values of the 0x0F screen flip object (from the app's selection view)."""
    LANDSCAPE = 0x00
    LANDSCAPE_FLIP_H = 0x01
    LANDSCAPE_FLIP_V = 0x02
    LANDSCAPE_FLIP_HV = 0x03
    PORTRAIT = 0x10
    PORTRAIT_FLIP_H = 0x11
    PORTRAIT_FLIP_V = 0x12
    PORTRAIT_FLIP_HV = 0x13


class PlaybackRate(IntEnum):
    X0_5 = 95
    X1_0 = 100
    X1_5 = 105
    X2_0 = 120
    X3_0 = 130


class ColorType(IntEnum):
    MONO = 0x10       # 1 bit per pixel, packed MSB-first across the whole frame
    RGB332 = 0x80     # 1 byte per pixel RRRGGBBB
    RGB565 = 0xEE     # 2 bytes per pixel (app's encoder is broken for this one)
    RGB888 = 0xFF     # 3 bytes per pixel
    RGB_LATTICE = 0x31  # 3 bytes per pixel, sent as object 0x06


# --------------------------------------------------------------------------
# Primitive helpers
# --------------------------------------------------------------------------
def u8(v: int) -> bytes:
    return struct.pack(">B", v & 0xFF)


def u16(v: int) -> bytes:
    return struct.pack(">H", v & 0xFFFF)


def u32(v: int) -> bytes:
    return struct.pack(">I", v & 0xFFFFFFFF)


def u64(v: int) -> bytes:
    return struct.pack(">Q", v & 0xFFFFFFFFFFFFFFFF)


def checksum(data: bytes) -> int:
    """Utils.checkSum: byte sum, two's-complemented if it overflows one byte."""
    s = sum(data)
    return s if s <= 0xFF else (-s) & 0xFF


# --------------------------------------------------------------------------
# Frames
# --------------------------------------------------------------------------
def short_command(cmd_id: int, content: bytes = b"", serial: int = 0) -> bytes:
    """Short command written to FF21: [len][id][content]. Empty content -> [04][id][serial16]."""
    if not content:
        return bytes([4, cmd_id & 0xFF]) + u16(serial)
    return bytes([len(content) + 2, cmd_id & 0xFF]) + content


def data_frame(cmd_id: int, content: bytes, follow: bytes = b"", sn: int = 0) -> bytes:
    """Data frame written to FF22: [u32 len][u16 id][u32 SN][content][cs] + follow."""
    head = u32(len(content) + 11) + u16(cmd_id) + u32(sn) + content
    return head + u8(checksum(head)) + follow


def start_sending(serial: int, cmd_id: int, data_len: int) -> bytes:
    return short_command(Cmd.START_SENDING, u16(serial) + u16(cmd_id) + u32(data_len))


def end_sending(serial_echo: bytes, cmd_id: int, data_len: int) -> bytes:
    return short_command(Cmd.END_SENDING, serial_echo[:2] + u16(cmd_id) + u32(data_len))


def obj(obj_id: int, content: bytes, follow: bytes = b"") -> bytes:
    """Object: [u32 len=content+7][u16 id][content][cs] + follow (children, not counted)."""
    head = u32(len(content) + 7) + u16(obj_id) + content
    return head + u8(checksum(head)) + follow


# --------------------------------------------------------------------------
# Objects
# --------------------------------------------------------------------------
def brightness_obj(level: int) -> bytes:
    if not 0 <= level <= 100:
        raise ValueError("brightness must be 0..100")
    return obj(Obj.BRIGHTNESS, u8(level))


def flip_obj(mode: int) -> bytes:
    return obj(Obj.SCREEN_FLIP, bytes([mode & 0xFF, 0, 0]))


def playback_rate_obj(rate: int) -> bytes:
    return obj(Obj.PLAYBACK_RATE, u8(rate))


def time_obj(ms: int) -> bytes:
    return obj(Obj.TIME, u8(0) + u16(ms))


def effect_obj(effect: int) -> bytes:
    return obj(Obj.EFFECT, u8(effect))


def color_obj(rgb: tuple[int, int, int]) -> bytes:
    r, g, b = rgb
    return obj(Obj.COLOR, bytes([r & 0xFF, g & 0xFF, b & 0xFF]))


def text_char_obj(ch: str) -> bytes:
    return obj(Obj.TEXT_CHAR, u16(ord(ch)))


def glyph_obj(ch: str, width: int, height: int, bitmap: bytes) -> bytes:
    content = u8(1) + u16(width) + u16(height) + u16(ord(ch)) + u8(len(bitmap)) + bitmap
    return obj(Obj.FONT_GLYPH, content)


def word_stock_obj(glyphs: Sequence[bytes]) -> bytes:
    return obj(Obj.WORD_STOCK, u16(len(glyphs)), b"".join(glyphs))


def character_string_obj(
    text: str,
    colors: Sequence[tuple[int, int, int]],
    interval_ms: int,
    effect: int,
) -> bytes:
    if len(colors) != len(text):
        raise ValueError("need one colour per character")
    children = b"".join(color_obj(c) + text_char_obj(ch) for ch, c in zip(text, colors))
    children += time_obj(interval_ms) + effect_obj(effect)
    return obj(Obj.CHARACTER_STRING, u16(len(text)) + u8(1), children)


def histogram_obj(values: Sequence[int]) -> bytes:
    return obj(Obj.HISTOGRAM, u16(len(values)) + b"".join(u16(v) for v in values))


def color_frame_obj(width: int, height: int, bpp: int, data: bytes) -> bytes:
    return obj(Obj.COLOR_FRAME, u16(width) + u16(height) + u8(bpp) + data)


def lattice_frame_obj(width: int, height: int, rgb_data: bytes) -> bytes:
    return obj(Obj.LATTICE_FRAME, u16(width) + u16(height) + rgb_data)


def frame_animation_obj(frames: Sequence[bytes], interval_ms: int, effect: int | None) -> bytes:
    children = b"".join(frames) + time_obj(interval_ms)
    if effect is not None:
        children += effect_obj(effect)
    return obj(Obj.FRAME_ANIMATION, u16(len(frames)), children)


# --------------------------------------------------------------------------
# Pixel encoding (ObjectFactory.getLatticeFrameObject)
# --------------------------------------------------------------------------
Pixel = tuple[int, int, int]


def encode_frame(width: int, height: int, pixels: Sequence[Pixel], color_type: int) -> bytes:
    """Encode row-major RGB pixels into the frame object the device expects."""
    if len(pixels) != width * height:
        raise ValueError(f"expected {width * height} pixels, got {len(pixels)}")

    if color_type == ColorType.MONO:
        out = bytearray((len(pixels) + 7) // 8)
        for i, (r, g, b) in enumerate(pixels):
            if r or g or b:
                out[i // 8] |= 0x80 >> (i % 8)
        return color_frame_obj(width, height, 1, bytes(out))

    if color_type == ColorType.RGB332:
        out = bytes(
            ((r * 7 // 255) << 5) | ((g * 3 // 255) << 3) | (b * 7 // 255)
            for r, g, b in pixels
        )
        return color_frame_obj(width, height, 8, out)

    if color_type == ColorType.RGB565:
        # Channel scaling matches ObjectFactory.getColorData(px, 2, 31, 63, 31). The app's
        # byte packing for this type always throws (overlapping 3-byte writes), so the
        # big-endian u16 layout here is the evident intent, unverified on hardware.
        out = b"".join(
            u16(((r * 31 // 255) << 11) | ((g * 63 // 255) << 5) | (b * 31 // 255)) for r, g, b in pixels
        )
        return color_frame_obj(width, height, 16, out)

    if color_type == ColorType.RGB_LATTICE:
        return lattice_frame_obj(width, height, bytes(c for p in pixels for c in p))

    # 0xFF and anything unknown: 24-bit RGB
    return color_frame_obj(width, height, 24, bytes(c for p in pixels for c in p))


# --------------------------------------------------------------------------
# Responses
# --------------------------------------------------------------------------
class Reassembler:
    """
    Rebuilds device frames from FF21 notifications (LengthFieldBasedBytesDecoder).
    Short frames: first byte is the total length. Long frames (0x80xx responses):
    first 4 bytes are the total length and bytes 4..5 hold the response id.
    """

    def __init__(self) -> None:
        self.buf = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self.buf += data
        frames: list[bytes] = []
        while self.buf:
            if len(self.buf) >= 6 and int.from_bytes(self.buf[4:6], "big") in LONG_RESPONSE_IDS:
                need = int.from_bytes(self.buf[0:4], "big")
            elif self.buf[0] == 0:
                if len(self.buf) < 6:
                    break  # probably the start of a long frame
                del self.buf[0]  # garbage
                continue
            else:
                need = self.buf[0]
            if need < 2 or len(self.buf) < need:
                break
            frames.append(bytes(self.buf[:need]))
            del self.buf[:need]
        return frames


def response_id(frame: bytes) -> int:
    if len(frame) >= 6 and int.from_bytes(frame[4:6], "big") in LONG_RESPONSE_IDS:
        return int.from_bytes(frame[4:6], "big")
    return frame[1]


@dataclass
class ScreenInfo:
    width: int
    height: int
    color_type: int
    max_frames: int
    brightness: int
    word_stock: int          # 2 => device has a built-in font, glyphs need not be sent
    light_up: int | None     # screen on/off state, only on newer firmware

    @classmethod
    def parse(cls, r: bytes) -> "ScreenInfo":
        return cls(
            width=int.from_bytes(r[5:7], "big"),
            height=int.from_bytes(r[7:9], "big"),
            color_type=r[9],
            max_frames=r[10],
            brightness=r[11],
            word_stock=r[12],
            light_up=r[13] if len(r) > 13 else None,
        )


@dataclass
class VersionInfo:
    hardware_type: int
    hardware_version: int
    firmware_version: int

    @classmethod
    def parse(cls, r: bytes) -> "VersionInfo":
        return cls(
            hardware_type=int.from_bytes(r[5:7], "big"),
            hardware_version=int.from_bytes(r[7:11], "big"),
            firmware_version=int.from_bytes(r[11:15], "big"),
        )


@dataclass
class AnimTextCapability:
    max_frames: int
    max_chars: int
    storage: int | None

    @classmethod
    def parse(cls, r: bytes) -> "AnimTextCapability":
        return cls(
            max_frames=int.from_bytes(r[5:7], "big"),
            max_chars=int.from_bytes(r[7:9], "big"),
            storage=int.from_bytes(r[9:13], "big") if len(r) > 12 else None,
        )


def parse_capacity(r: bytes) -> int:
    return int.from_bytes(r[5:9], "big")


def parse_continue_offset(r: bytes) -> int:
    """0xFF frame: [len][FF][serial 2][cmd 2][offset 4]."""
    return int.from_bytes(r[6:10], "big")


def chunks(data: bytes, size: int) -> Iterable[bytes]:
    for i in range(0, len(data), size):
        yield data[i:i + size]
