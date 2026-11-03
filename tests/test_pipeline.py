"""Conversion pipeline tests: the reader thread, the ffmpeg pipe, the carry-over.

ffmpeg is replaced by a stub that records what arrives on its stdin. That is
not a shortcut around a real mux: the synthetic SPS these tests build is not a
parsable one, so a real ffmpeg cannot write an avcC box from it, and the LGPL
build the packages ship has no H.264 encoder to produce valid test video with
either. What these tests do check is this module's own behaviour: that every
frame reaches the muxer over the pipe, in order, including the frames carried
over from the previous file.
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import g64_to_mp4 as core  # noqa: E402

from test_parser import FT0, block, fu_a, g64, rtp  # noqa: E402

TICK = 10_000_000  # FILETIME ticks per second

STUB = '''\
import os, sys
data = sys.stdin.buffer.read()
dst = sys.argv[-1]
with open(os.path.join(os.environ["G64_STUB_DIR"],
                       os.path.basename(dst) + ".stdin"), "wb") as f:
    f.write(data)
with open(dst, "wb") as f:
    f.write(b"\\0" * 32)   # non-empty, so the caller's size check passes
'''


def h264_file(first_ft, with_keyframe=True, p_frames=2):
    """A one-camera .g64 payload, optionally with no keyframe of its own."""
    blocks = []
    ft = first_ft
    if with_keyframe:
        sps = b"\x67\x4d\x00\x1f" + bytes(range(20))
        pps = b"\x68\xee\x3c\x80"
        idr = b"\x65" + bytes(range(256)) * 3
        pkts = [rtp(1, 1000, sps), rtp(2, 1000, pps)]
        frags = fu_a(idr, 300)
        pkts += [rtp(3 + i, 1000, f, marker=int(i == len(frags) - 1))
                 for i, f in enumerate(frags)]
        blocks.append(block(ft, 4, pkts))
        ft += TICK // 15
    for i in range(p_frames):
        payload = b"\x41" + bytes([i]) + bytes(range(100))
        blocks.append(block(ft, 0, [rtp(20 + i, 1000 + 6000 * (i + 1), payload, marker=1)]))
        ft += TICK // 15
    return g64(blocks), ft


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.out = os.path.join(self.root, "MP4")
        self.stub_dir = tempfile.mkdtemp()
        script = os.path.join(self.stub_dir, "stub.py")
        with open(script, "w") as f:
            f.write(STUB)
        if os.name == "nt":
            self.stub = os.path.join(self.stub_dir, "stub.bat")
            body = '@echo off\r\n"%s" "%s" %%*\r\n' % (sys.executable, script)
        else:
            self.stub = os.path.join(self.stub_dir, "stub.sh")
            body = '#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, script)
        with open(self.stub, "w") as f:
            f.write(body)
        os.chmod(self.stub, 0o755)

        self.env = {k: os.environ.get(k) for k in ("G64_FFMPEG", "G64_STUB_DIR")}
        os.environ["G64_FFMPEG"] = self.stub
        os.environ["G64_STUB_DIR"] = self.stub_dir

        self.lines = []
        self._log = core.log
        core.log = self.lines.append

    def tearDown(self):
        core.log = self._log
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.stub_dir, ignore_errors=True)

    # --- helpers ---
    def write(self, name, payload):
        with open(os.path.join(self.root, name), "wb") as f:
            f.write(payload)

    def captured(self, mp4_name):
        path = os.path.join(self.stub_dir, mp4_name + ".stdin")
        with open(path, "rb") as f:
            return f.read()

    def report(self):
        return "\n".join(self.lines)

    # --- tests ---
    def test_pipe_delivers_every_frame_in_order(self):
        payload, _ = h264_file(FT0)
        self.write("cam_01.g64", payload)
        summary = core.run([self.root], self.out)
        self.assertEqual((summary.converted, summary.failed), (1, 0), self.report())
        expected = b"".join(f[1] for f in core.parse_g64_bytes(payload).frames)
        self.assertEqual(self.captured("cam_01.mp4"), expected)

    def test_timings_are_reported(self):
        payload, _ = h264_file(FT0)
        self.write("cam_01.g64", payload)
        core.run([self.root], self.out)
        ok = [l for l in self.lines if ": OK " in l]
        self.assertEqual(len(ok), 1, self.report())
        for field in ("read ", "parse ", "mux "):
            self.assertIn(field, ok[0])

    def test_contiguous_file_without_keyframe_is_rescued(self):
        first, next_ft = h264_file(FT0)
        # Opens on a P-frame a fraction of a second later, so the keyframe it
        # needs is the one ending the previous file.
        second, _ = h264_file(next_ft, with_keyframe=False)
        self.write("cam_01.g64", first)
        self.write("cam_02.g64", second)
        summary = core.run([self.root], self.out)
        self.assertEqual((summary.converted, summary.failed), (2, 0), self.report())
        self.assertTrue(any("prepended" in l for l in self.lines), self.report())
        own = b"".join(f[1] for f in core.parse_g64_bytes(second).frames)
        sent = self.captured("cam_02.mp4")
        self.assertTrue(sent.endswith(own), "carried-over frames must come first")
        self.assertGreater(len(sent), len(own), "nothing was carried over")

    def test_keyframeless_file_after_a_gap_is_not_rescued(self):
        first, next_ft = h264_file(FT0)
        # Past GOP_GAP_SECONDS the previous GOP belongs to another recording,
        # so there is nothing these P-frames can be decoded against.
        gap = next_ft + int((core.GOP_GAP_SECONDS + 5) * TICK)
        second, _ = h264_file(gap, with_keyframe=False)
        self.write("cam_01.g64", first)
        self.write("cam_02.g64", second)
        summary = core.run([self.root], self.out)
        self.assertEqual((summary.converted, summary.failed), (1, 1), self.report())
        self.assertTrue(any("no keyframe" in l for l in self.lines), self.report())
        self.assertFalse(os.path.isfile(os.path.join(self.out, "cam_02.mp4")))

    def test_one_bad_file_does_not_stop_the_rest(self):
        payload, _ = h264_file(FT0)
        self.write("cam_01.g64", b"not a Genetec archive at all")
        self.write("cam_02.g64", payload)
        summary = core.run([self.root], self.out)
        self.assertEqual((summary.converted, summary.failed), (1, 1), self.report())
        self.assertTrue(os.path.isfile(os.path.join(self.out, "cam_02.mp4")))

    def test_reader_delivers_all_files_when_the_consumer_is_slower(self):
        """The reader runs ahead; the end-of-stream hand-off must still arrive."""
        names = []
        sources = [("", "f%d" % i, (lambda: b"payload")) for i in range(4)]
        for _cam, name, payload, read_s in core._prefetch(sources):
            names.append(name)
            self.assertEqual(payload, b"payload")
            self.assertGreaterEqual(read_s, 0.0)
        self.assertEqual(names, ["f0", "f1", "f2", "f3"])


if __name__ == "__main__":
    unittest.main()
