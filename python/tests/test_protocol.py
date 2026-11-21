"""
Byte-exact checks against the layouts recovered from the APK smali, plus an
end-to-end run of the client against a simulated panel.
"""
import asyncio

import pytest

from spotled_ble import protocol as p
from spotled_ble.font import FONT_DIR, STYLES, load_font

needs_font = pytest.mark.skipif(
    not (FONT_DIR / STYLES["regular"]).exists(),
    reason="vendor font not extracted (scripts/extract_fonts.py)",
)


def h(s: str) -> bytes:
    return bytes.fromhex(s.replace(" ", "").replace("|", ""))


def test_checksum():
    assert p.checksum(h("00 00 00 08 00 0e 32")) == 0x48
    assert p.checksum(bytes([200, 100])) == (-300) & 0xFF


def test_brightness_object_and_frame():
    o = p.brightness_obj(50)
    assert o == h("00 00 00 08 00 0e 32 48")
    frame = p.data_frame(p.Cmd.SET_OBJECT, p.u32(len(o)), o, sn=0)
    assert frame == h("00 00 00 0F 80 04 00 00 00 00 00 00 00 08 9B") + o


def test_flip_is_three_bytes():
    assert p.flip_obj(1) == h("00 00 00 0a 00 0f 01 00 00 1a")
    assert p.flip_obj(0x10) == h("00 00 00 0a 00 0f 10 00 00 29")


def test_playback_rate():
    assert p.playback_rate_obj(100) == h("00 00 00 08 00 20 64 8c")


def test_short_commands():
    assert p.short_command(p.Cmd.SCREEN_INFO, serial=0) == h("04 12 00 00")
    assert p.start_sending(1, 0x8004, 23) == h("0a 01 0001 8004 00000017")
    assert p.end_sending(h("0001"), 0x8004, 23) == h("0a 03 0001 8004 00000017")


def test_character_string():
    o = p.character_string_obj("Hi", [(255, 255, 255)] * 2, 200, p.Effect.SCROLL_LEFT)
    assert o == h(
        "00 00 00 0a 00 04 00 02 01 11 | 00 00 00 0a 00 02 ff ff ff f7 | 00 00 00 09 00 03 00 48 54"
        "| 00 00 00 0a 00 02 ff ff ff f7 | 00 00 00 09 00 03 00 69 75 | 00 00 00 0a 00 07 00 00 c8 d9"
        "| 00 00 00 08 00 08 03 13"
    )


@needs_font
def test_glyph_header():
    g = p.glyph_obj("H", 12, 12, load_font().glyph("H"))
    assert g[:12] == h("00 00 00 27 00 0d 01 00 0c 00 0c 00")
    assert g[12:14] == h("48 18") and len(g) == 0x27


def test_mono_frame_packing():
    w, hgt = 32, 16
    px = [(0, 0, 0)] * (w * hgt)
    px[0] = px[-1] = (255, 255, 255)
    f = p.encode_frame(w, hgt, px, p.ColorType.MONO)
    assert f == h("00 00 00 4c 00 60 00 20 00 10 01 80") + bytes(62) + h("01 a2")
    anim = p.frame_animation_obj([f], 200, p.Effect.STATIC)
    assert anim.startswith(h("00 00 00 09 00 0b 00 01 15"))
    assert anim.endswith(h("00 00 00 0a 00 07 00 00 c8 d9 00 00 00 08 00 08 00 10"))


def test_color_frames():
    px = [(255, 0, 0), (0, 0, 255)]
    assert p.encode_frame(2, 1, px, p.ColorType.RGB888) == h("00 00 00 12 00 60 00 02 00 01 18 ff 00 00 00 00 ff 75")
    assert p.encode_frame(2, 1, px, p.ColorType.RGB_LATTICE) == h("00 00 00 11 00 06 00 02 00 01 ff 00 00 00 00 ff e8")
    rgb332 = p.encode_frame(2, 1, px, p.ColorType.RGB332)
    assert rgb332[11:13] == bytes([0b11100000, 0b00000111])


def test_histogram():
    vals = list(range(13)) + [12, 6, 3]
    assert p.histogram_obj(vals) == h(
        "00 00 00 29 00 0a 00 10 0000 0001 0002 0003 0004 0005 0006 0007 0008 0009 000a 000b 000c 000c 0006 0003 a6"
    )


def test_reassembler_split_and_long_frames():
    r = p.Reassembler()
    assert r.feed(h("09 15 00 00")) == []
    assert r.feed(h("00 00 00 04 44 0a 02")) == [h("09 15 00 00 00 00 00 04 44")]
    long = h("00 00 00 0d 80 ff 00 00 00 00 00 00 01")
    assert r.feed(h("00 01 00 00 00 00 00 00")) == [h("0a 02 00 01 00 00 00 00 00 00")]
    assert r.feed(long[:5]) == []
    assert r.feed(long[5:]) == [long]
    assert p.response_id(long) == 0x80FF


@needs_font
def test_font_glyph_A():
    g = load_font().glyph("A")
    assert len(g) == 24 and g[4] == 0x20  # row 2: ..#.........


# ---------------------------------------------------------------------------
# End-to-end against a simulated panel
# ---------------------------------------------------------------------------
class FakeChar:
    def __init__(self, props):
        self.properties = props
        self.max_write_without_response_size = 182


class FakeService:
    def get_characteristic(self, uuid):
        return FakeChar(["write-without-response", "notify"] if uuid == p.CMD_CHAR_UUID else ["write-without-response"])


class FakeServices:
    def get_service(self, uuid):
        return FakeService() if uuid == p.SERVICE_UUID else None


class FakePanel:
    """Implements just enough firmware behaviour, incl. CONTINUE flow control every RECV bytes."""
    RECV = 1092

    def __init__(self, *_a, **_k):
        self.is_connected = False
        self.services = FakeServices()
        self.mtu_size = 247
        self.cb = None
        self.data = bytearray()
        self.expect_len = 0
        self.received: list[bytes] = []
        self.cmd_log: list[bytes] = []
        self.since_continue = 0

    async def connect(self):
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False

    async def start_notify(self, uuid, cb):
        assert uuid == p.CMD_CHAR_UUID
        self.cb = cb

    def notify(self, data: bytes):
        # split notifications to exercise reassembly
        asyncio.get_running_loop().call_soon(self.cb, None, bytearray(data[:3]))
        asyncio.get_running_loop().call_soon(self.cb, None, bytearray(data[3:]))

    async def write_gatt_char(self, uuid, data, response=False):
        data = bytes(data)
        if uuid == p.DATA_CHAR_UUID:
            assert len(data) <= 182
            self.data += data
            self.since_continue += len(data)
            if len(self.data) == self.expect_len:
                self.received.append(bytes(self.data))
            if self.since_continue == self.RECV:
                self.since_continue = 0
                self.notify(h("0a ff 0000 8004") + p.u32(len(self.data)))
            return
        self.cmd_log.append(data)
        cid = data[1]
        if cid == p.Cmd.VERSION:
            self.notify(h("0f 11 00 00 00 0001 00000002 00000003"))
        elif cid == p.Cmd.SCREEN_INFO:
            self.notify(h("0e 13 00 00 00 0030 000c 10 14 32 01 01"))  # 48x12 mono
        elif cid == p.Cmd.ANIM_TEXT_CAPABILITY:
            self.notify(h("0d 1b 00 00 00 0014 0050 00010000"))
        elif cid == p.Cmd.CAPACITY:
            self.notify(h("09 15 00 00 00 00 00 04 44"))
        elif cid == p.Cmd.START_SENDING:
            self.data = bytearray()
            self.since_continue = 0
            self.expect_len = int.from_bytes(data[6:10], "big")
            self.notify(h("07 02") + data[2:4] + h("00 80 04"))
        elif cid == p.Cmd.END_SENDING:
            ok = len(self.data) == self.expect_len
            self.notify(h("07 04") + data[2:4] + (h("00") if ok else h("01")) + h("80 04"))


@pytest.fixture
def fake(monkeypatch):
    import spotled_ble.client as c

    holder = {}

    def factory(*a, **k):
        holder["panel"] = FakePanel()
        return holder["panel"]

    monkeypatch.setattr(c, "BleakClient", factory)
    return holder


@needs_font
def test_client_end_to_end(fake):
    from spotled_ble.client import SpotLED

    async def run():
        led = SpotLED("fake")
        st = await led.connect()
        assert (st.width, st.height, st.color_type, st.receive_bytes, st.chunk_size) == (48, 12, 0x10, 1092, 182)
        await led.set_brightness(30)
        await led.show_text("Hello AI " * 3)  # word stock ~1.2 kB -> exercises CONTINUE
        frames = [[(255, 255, 255) if (x + y + i) % 3 == 0 else (0, 0, 0) for y in range(12) for x in range(48)]
                  for i in range(20)]
        await led.show_frames(frames, interval_ms=100)
        return fake["panel"]

    panel = asyncio.run(run())
    assert len(panel.received) == 4  # brightness, word stock, char string, animation
    assert panel.received[0].endswith(p.brightness_obj(30))
    assert len(panel.received[3]) > 1092  # animation crossed a CONTINUE window and finished
    assert panel.cmd_log[-1][1] == p.Cmd.END_SENDING
