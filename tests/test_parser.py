"""Container and RTP tests using synthetic G64 data (no ffmpeg needed)."""
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import g64_to_mp4 as core  # noqa: E402

PREFIX = bytes.fromhex("806600000000000000000030")
FT0 = 0x01DD50A95130CA30  # 2026-09-30 07:00:00.193 UTC


def rtp(seq, ts, payload, marker=0, pt=96):
    return bytes([0x80, (marker << 7) | pt]) + struct.pack(">HI", seq, ts) + b"\x12\x34\x56\x78" + payload


def record(pkt, tag=0x1F):
    return bytes([0, tag]) + struct.pack(">H", len(pkt)) + b"\x00\x00" + pkt


def block(ft, flags, pkts):
    payload = PREFIX + b"".join(record(p) for p in pkts)
    return (struct.pack("<QBI", ft, flags, len(payload)) + payload
            + b"\x00" * core.HASH_LEN + struct.pack("<I", 13 + len(payload)))


def g64(blocks):
    return b"Genetec Omnicast Archive v5.31" + b"\x00" * 200 + b"".join(blocks)


def fu_a(nal, chunk):
    """Split an H.264 NAL unit into FU-A packets."""
    first, body = nal[0], nal[1:]
    ind = (first & 0xE0) | 28
    parts = [body[i:i + chunk] for i in range(0, len(body), chunk)]
    pkts = []
    for i, part in enumerate(parts):
        fu = (first & 0x1F) | (0x80 if i == 0 else 0) | (0x40 if i == len(parts) - 1 else 0)
        pkts.append(bytes([ind, fu]) + part)
    return pkts


def fu_hevc(nal, chunk):
    """Split an H.265 NAL unit into RFC 7798 fragmentation units."""
    nal_type = (nal[0] >> 1) & 0x3F
    body = nal[2:]
    hdr = bytes([(nal[0] & 0x81) | (49 << 1), nal[1]])
    parts = [body[i:i + chunk] for i in range(0, len(body), chunk)]
    pkts = []
    for i, part in enumerate(parts):
        fu = nal_type | (0x80 if i == 0 else 0) | (0x40 if i == len(parts) - 1 else 0)
        pkts.append(hdr + bytes([fu]) + part)
    return pkts


class H264Tests(unittest.TestCase):
    def setUp(self):
        self.sps = b"\x67\x4d\x00\x1f" + bytes(range(20))
        self.pps = b"\x68\xee\x3c\x80"
        self.idr = b"\x65" + bytes(range(256)) * 3
        self.p = b"\x41" + bytes(range(100))
        pkts_key = [rtp(1, 1000, self.sps), rtp(2, 1000, self.pps)]
        frags = fu_a(self.idr, 300)
        pkts_key += [rtp(3 + i, 1000, f, marker=int(i == len(frags) - 1)) for i, f in enumerate(frags)]
        self.key_block = block(FT0, 4, pkts_key)
        self.p_block = block(FT0 + 660000, 0, [rtp(10, 1000 + 6000, self.p, marker=1)])
        self.data = g64([self.key_block, self.p_block])

    def test_parse_and_reassemble(self):
        res = core.parse_g64_bytes(self.data)
        self.assertIsNone(res.unsupported)
        self.assertEqual(res.codec, "h264")
        self.assertEqual(len(res.frames), 2)
        self.assertTrue(res.frames[0][2])
        self.assertFalse(res.frames[1][2])
        expected = core.SC + self.sps + core.SC + self.pps + core.SC + self.idr
        self.assertEqual(res.frames[0][1], expected)
        self.assertEqual(res.frames[1][1], core.SC + self.p)
        self.assertEqual(res.version, "Genetec Omnicast Archive v5.31")
        self.assertEqual(res.payload_types, {96: 6})  # SPS, PPS, 3 FU-A, 1 P

    def test_truncated_file_keeps_complete_frames(self):
        cut = self.data[:-(len(self.p_block) // 2)]
        res = core.parse_g64_bytes(cut)
        self.assertIsNone(res.unsupported)
        self.assertGreaterEqual(len(res.frames), 1)
        self.assertEqual(res.frames[0][1][:8], core.SC + self.sps[:4])

    def test_diag_mentions_codec(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".g64", delete=False) as f:
            f.write(self.data)
        try:
            text = core.diagnose(f.name)
        finally:
            os.remove(f.name)
        self.assertIn("video codec: h264", text)
        self.assertIn("status: convertible", text)


class HevcTests(unittest.TestCase):
    def test_fragmented_idr(self):
        vps = b"\x40\x01\x0c" + bytes(range(10))
        sps = b"\x42\x01\x01" + bytes(range(30))
        pps = b"\x44\x01\xc0" + bytes(range(5))
        idr = b"\x26\x01" + bytes(range(256)) * 2  # type 19 (IDR_W_RADL)
        frags = fu_hevc(idr, 200)
        pkts = [rtp(1, 0, vps), rtp(2, 0, sps), rtp(3, 0, pps)]
        pkts += [rtp(4 + i, 0, f, marker=int(i == len(frags) - 1)) for i, f in enumerate(frags)]
        trail = b"\x02\x01" + bytes(range(50))
        data = g64([block(FT0, 4, pkts), block(FT0 + 660000, 0, [rtp(20, 6000, trail, marker=1)])])
        res = core.parse_g64_bytes(data)
        self.assertEqual(res.codec, "hevc")
        self.assertEqual(len(res.frames), 2)
        self.assertEqual(res.frames[0][1], core.SC + vps + core.SC + sps + core.SC + pps + core.SC + idr)
        self.assertTrue(res.frames[0][2])
        self.assertEqual(res.frames[1][1], core.SC + trail)


class UnsupportedTests(unittest.TestCase):
    def test_not_genetec(self):
        res = core.parse_g64_bytes(b"RIFF" + b"\x00" * 500)
        self.assertEqual(res.unsupported, "not a recognised Omnicast archive")

    def test_no_video_payload_type(self):
        pkts = [rtp(1, 0, b"\xff" * 40, pt=0)]  # G.711 audio only
        res = core.parse_g64_bytes(g64([block(FT0, 0, pkts)]))
        self.assertIn("no video RTP stream", res.unsupported)
        self.assertEqual(res.payload_types, {0: 1})

    def test_unknown_codec(self):
        pkts = [rtp(1, 0, b"\xff\xd8\xff" + bytes(50))]  # looks like nothing we know
        res = core.parse_g64_bytes(g64([block(FT0, 4, pkts)]))
        self.assertIn("codec not recognised", res.unsupported)


class NamingTests(unittest.TestCase):
    def test_output_name(self):
        self.assertEqual(core.output_name("CAM01/a_2026-09-30_00h00.g64"), "a_2026-09-30_00h00.mp4")
        self.assertEqual(core.output_name("b.g64.INCOMPLETE"), "b.INCOMPLETE.mp4")


if __name__ == "__main__":
    unittest.main()
