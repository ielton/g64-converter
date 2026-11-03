"""Convert Genetec Omnicast G64 video archives to MP4 (stream copy, no re-encode).

Runs on Windows, macOS and Linux. Needs ffmpeg. 7-Zip (7za/7z) is only needed to
read zip downloads that were cut off (OneDrive truncates folder downloads at 20 GB).

Usage:
  g64_to_mp4.py INPUT [INPUT ...] [-o OUTPUT_DIR]
      INPUT is a .g64 file, a folder of .g64 files (camera sub-folders are kept),
      or a .zip export. Without -o, MP4s go to a folder named "MP4" next to the input.
  g64_to_mp4.py --diag FILE.g64
      Describe what is inside a file (container version, streams, codec) without converting.
  g64_to_mp4.py
      With no arguments, opens the graphical window (g64_gui.py) when tkinter is available.

Container layout (reverse-engineered from "Genetec Omnicast Archive v5.31" exports):
  frame block = FILETIME(8, LE) | flags(1; 0x04 = keyframe) | payload_size(4, LE)
                | payload: 12-byte prefix (80 66 00 ... 30) + records
  record      = 00 xx | len(2, BE) | 00 00 | RTP packet (xx = stream id, e.g. 1f/1d/41)
  after block = 64-byte hash + 4-byte LE size of the previous block
RTP payload type 96 carries H.264 (RFC 6184) or H.265 (RFC 7798). The codec is detected
per file from parameter-set packets. Audio streams are ignored.

Consecutive files of the same camera are processed in name order. A file usually starts
with P-frames whose keyframe is at the end of the previous file, so the previous file's
last GOP is prepended when the recording is contiguous; no frame is lost.
"""
import argparse
import datetime
import os
import re
import shutil
import statistics
import struct
import subprocess
import sys
import tempfile
import time
import zipfile

__version__ = "1.0.0"

EPOCH_DIFF = 116444736000000000  # FILETIME ticks between 1601-01-01 and 1970-01-01
MAGIC = b"Genetec Omnicast Archive"
# 12-byte payload prefix followed by the first record tag (00 xx)
BLOCK_RE = re.compile(rb"\x80\x66\x00\x00\x00\x00\x00\x00\x00\x00\x00\x30\x00[\x00-\xff]")
SC = b"\x00\x00\x00\x01"  # Annex B start code
VIDEO_PT = 96
HASH_LEN = 64
TRAILER_LEN = HASH_LEN + 4
GOP_GAP_SECONDS = 2.0  # max gap between files to treat the recording as contiguous


def log(message):
    """Default logger: print to stdout. The GUI replaces this attribute."""
    print(message, flush=True)


# --- external tools ------------------------------------------------------------

def _search_dirs():
    dirs = []
    if getattr(sys, "frozen", False):
        dirs.append(os.path.dirname(sys.executable))
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            dirs.append(meipass)
    dirs.append(os.path.dirname(os.path.abspath(__file__)))
    return dirs


def find_tool(names, env_var):
    """Locate an external executable: env override, next to the program, then PATH."""
    override = os.environ.get(env_var)
    if override:
        return override
    exts = [".exe", ""] if os.name == "nt" else [""]
    for d in _search_dirs():
        for n in names:
            for e in exts:
                candidate = os.path.join(d, n + e)
                if os.path.isfile(candidate):
                    return candidate
    for n in names:
        found = shutil.which(n)
        if found:
            return found
    return None


def ffmpeg_path():
    return find_tool(["ffmpeg"], "G64_FFMPEG")


def sevenzip_path():
    return find_tool(["7za", "7z", "7zz"], "G64_7Z")


def _popen_flags():
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


# --- RTP ------------------------------------------------------------------------

def rtp_payload(pkt):
    """Return (payload_type, payload) for an RTP packet, or (None, None) if not RTP."""
    if len(pkt) < 12 or (pkt[0] >> 6) != 2:
        return None, None
    pt = pkt[1] & 0x7F
    cc = pkt[0] & 0x0F
    hdr = 12 + 4 * cc
    if (pkt[0] >> 4) & 1:  # header extension
        if len(pkt) < hdr + 4:
            return None, None
        ext_len = struct.unpack(">H", pkt[hdr + 2:hdr + 4])[0]
        hdr += 4 + 4 * ext_len
    if pkt[0] & 0x20 and pkt[-1] <= len(pkt):  # padding
        pkt = pkt[:len(pkt) - pkt[-1]]
    return pt, pkt[hdr:]


def depacketize_h264(payload, fu_buf, out):
    """RFC 6184: single NAL, STAP-A and FU-A."""
    if not payload:
        return
    t = payload[0] & 0x1F
    if 1 <= t <= 23:
        out.append(SC + payload)
    elif t == 24:  # STAP-A
        p = 1
        while p + 2 <= len(payload):
            n = struct.unpack(">H", payload[p:p + 2])[0]
            out.append(SC + payload[p + 2:p + 2 + n])
            p += 2 + n
    elif t == 28:  # FU-A
        if len(payload) < 2:
            return
        fu = payload[1]
        start, end, nal_type = fu >> 7, (fu >> 6) & 1, fu & 0x1F
        if start:
            fu_buf[:] = [bytes([(payload[0] & 0xE0) | nal_type]) + payload[2:]]
        elif fu_buf:
            fu_buf.append(payload[2:])
        if end and fu_buf:
            out.append(SC + b"".join(fu_buf))
            fu_buf[:] = []


def depacketize_hevc(payload, fu_buf, out):
    """RFC 7798: single NAL, aggregation packet (48) and fragmentation unit (49)."""
    if len(payload) < 2:
        return
    t = (payload[0] >> 1) & 0x3F
    if t < 48:
        out.append(SC + payload)
    elif t == 48:
        p = 2
        while p + 2 <= len(payload):
            n = struct.unpack(">H", payload[p:p + 2])[0]
            out.append(SC + payload[p + 2:p + 2 + n])
            p += 2 + n
    elif t == 49:
        if len(payload) < 3:
            return
        fu = payload[2]
        start, end, nal_type = fu >> 7, (fu >> 6) & 1, fu & 0x3F
        if start:
            hdr = bytes([(payload[0] & 0x81) | (nal_type << 1), payload[1]])
            fu_buf[:] = [hdr + payload[3:]]
        elif fu_buf:
            fu_buf.append(payload[3:])
        if end and fu_buf:
            out.append(SC + b"".join(fu_buf))
            fu_buf[:] = []


DEPACKETIZERS = {"h264": depacketize_h264, "hevc": depacketize_hevc}


def detect_codec(payloads):
    """Return 'h264', 'hevc' or None, judged from parameter-set/fragment packets."""
    h264 = hevc = 0
    for pl in payloads:
        if len(pl) < 2:
            continue
        b = pl[0]
        if (b & 0x80) == 0 and (b & 0x1F) in (7, 8, 24):
            h264 += 1
        if pl[1] == 0x01 and ((b >> 1) & 0x3F) in (32, 33, 34, 49):
            hevc += 1
    if h264 == 0 and hevc == 0:
        return None
    return "hevc" if hevc > h264 else "h264"


def has_parameter_sets(nals, codec):
    if codec == "hevc":
        return any(((n[4] >> 1) & 0x3F) == 33 for n in nals)
    return any((n[4] & 0x1F) == 7 for n in nals)


# --- G64 container ----------------------------------------------------------------

class ParseResult:
    """frames = [(filetime, annexb_bytes, is_keyframe)], plus diagnostics."""

    def __init__(self):
        self.frames = []
        self.codec = None
        self.errors = 0
        self.version = ""
        self.blocks = 0
        self.flags = {}
        self.payload_types = {}
        self.record_tags = {}
        self.unsupported = None  # reason string when the file cannot be converted


def ft_to_dt(ft):
    return datetime.datetime.fromtimestamp((ft - EPOCH_DIFF) / 1e7, datetime.timezone.utc)


def _iter_records(payload):
    pos = 12
    while pos + 6 <= len(payload):
        if payload[pos] != 0:
            raise ValueError("bad record tag")
        tag = payload[pos + 1]
        ln = struct.unpack(">H", payload[pos + 2:pos + 4])[0]
        yield tag, payload[pos + 6:pos + 6 + ln]
        pos += 6 + ln


def parse_g64_bytes(data):
    res = ParseResult()
    m = re.match(MAGIC + rb" v[0-9.]+", data[:64])
    if m:
        res.version = m.group().decode("ascii")
    m = BLOCK_RE.search(data)
    if not m:
        res.unsupported = ("not a recognised Omnicast archive" if not res.version
                           else "no video frame blocks found")
        return res
    pos = m.start() - 13
    blocks = []  # (filetime, flags, [video payloads])
    while pos + 13 <= len(data):
        ft, flags, size = struct.unpack("<QBI", data[pos:pos + 13])
        payload = data[pos + 13:pos + 13 + size]
        truncated = len(payload) < size
        if payload[:2] != b"\x80\x66":
            m = BLOCK_RE.search(data, pos + 14)  # resync past the bad block
            if not m:
                break
            res.errors += 1
            pos = m.start() - 13
            continue
        res.blocks += 1
        res.flags[flags] = res.flags.get(flags, 0) + 1
        video = []
        try:
            for tag, pkt in _iter_records(payload):
                res.record_tags[tag] = res.record_tags.get(tag, 0) + 1
                pt, pl = rtp_payload(pkt)
                if pt is None:
                    continue
                res.payload_types[pt] = res.payload_types.get(pt, 0) + 1
                if pt == VIDEO_PT:
                    video.append(pl)
        except (ValueError, struct.error, IndexError):
            res.errors += 1
        if video:
            blocks.append((ft, flags, video))
        if truncated:  # cut-off file: keep the complete packets of the last block
            break
        pos += 13 + size + TRAILER_LEN
    if not blocks:
        res.unsupported = ("no video RTP stream (payload type %d); payload types seen: %s"
                           % (VIDEO_PT, sorted(res.payload_types) or "none"))
        return res
    sample = [pl for b in blocks[:400] for pl in b[2]]
    res.codec = detect_codec(sample)
    if res.codec is None:
        res.unsupported = "video codec not recognised (only H.264 and H.265 are supported)"
        return res
    depack = DEPACKETIZERS[res.codec]
    fu_buf = []
    for ft, flags, payloads in blocks:
        nals = []
        for pl in payloads:
            depack(pl, fu_buf, nals)
        if nals:
            is_key = bool(flags & 0x04) or has_parameter_sets(nals, res.codec)
            res.frames.append((ft, b"".join(nals), is_key))
    if not res.frames:
        res.unsupported = "no complete video frames could be reassembled"
    return res


def diagnose(path):
    """Human-readable description of a .g64 file."""
    data = open(path, "rb").read()
    res = parse_g64_bytes(data)
    lines = [
        "file: %s" % path,
        "size: %d bytes" % len(data),
        "container: %s" % (res.version or "unknown (no Genetec magic)"),
        "frame blocks: %d  (flags histogram: %s)" % (res.blocks, dict(sorted(res.flags.items()))),
        "record stream ids: %s" % {hex(k): v for k, v in sorted(res.record_tags.items())},
        "rtp payload types: %s" % dict(sorted(res.payload_types.items())),
        "video codec: %s" % (res.codec or "unknown"),
        "video frames: %d  keyframes: %d" % (len(res.frames), sum(1 for f in res.frames if f[2])),
        "parse errors: %d" % res.errors,
    ]
    if res.frames:
        first, last = ft_to_dt(res.frames[0][0]), ft_to_dt(res.frames[-1][0])
        lines.append("time span (UTC): %s -> %s" % (first.isoformat(), last.isoformat()))
    lines.append("status: %s" % (res.unsupported or "convertible"))
    return "\n".join(lines)


# --- MP4 muxing -------------------------------------------------------------------

def write_mp4(frames, dst, tmpdir, codec):
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found (put ffmpeg next to the program or on PATH)")
    raw = os.path.join(tmpdir, "stream." + codec)
    with open(raw, "wb") as f:
        for _, nal_bytes, _ in frames:
            f.write(nal_bytes)
    fts = [fr[0] for fr in frames]
    deltas = [(b - a) / 1e7 for a, b in zip(fts, fts[1:]) if b > a]
    median = statistics.median(deltas) if deltas else 1 / 15
    duration = (fts[-1] - fts[0]) / 1e7 + median
    fps = len(frames) / duration if duration > 0 else 15.0
    created = ft_to_dt(fts[0]).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    cmd = [ffmpeg, "-y", "-v", "error", "-fflags", "+genpts", "-r", "%.6f" % fps,
           "-f", codec, "-i", raw, "-c", "copy", "-movflags", "+faststart",
           "-metadata", "creation_time=" + created]
    if codec == "hevc":
        cmd += ["-tag:v", "hvc1"]  # Apple players need the hvc1 tag
    cmd.append(dst)
    r = subprocess.run(cmd, capture_output=True, text=True, **_popen_flags())
    os.remove(raw)
    err = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else ""
    return fps, r.returncode, err


# --- conversion -------------------------------------------------------------------

class CamState:
    """Carry-over between consecutive files of one camera (its last GOP)."""

    def __init__(self):
        self.prev_tail = []
        self.prev_last_ft = 0


class Summary:
    def __init__(self):
        self.converted = 0
        self.skipped = 0
        self.failed = 0
        self.unsupported = []


def output_name(name):
    base = os.path.basename(name)
    base = base.replace(".g64.INCOMPLETE", ".INCOMPLETE")
    base = re.sub(r"\.g64$", "", base, flags=re.IGNORECASE)
    return base + ".mp4"


def convert_one(cam, name, load_bytes, out_root, state, tmpdir, summary):
    t0 = time.time()
    label = ("[%s] " % cam) if cam else ""
    out_dir = os.path.join(out_root, cam) if cam else out_root
    os.makedirs(out_dir, exist_ok=True)
    dst = os.path.join(out_dir, output_name(name))
    res = parse_g64_bytes(load_bytes())
    if res.unsupported:
        log("%s%s: UNSUPPORTED - %s" % (label, name, res.unsupported))
        summary.unsupported.append((name, res.unsupported))
        summary.failed += 1
        return False
    frames = res.frames
    note = ""
    if not frames[0][2]:
        gap = (frames[0][0] - state.prev_last_ft) / 1e7 if state.prev_last_ft else 1e9
        if state.prev_tail and 0 <= gap < GOP_GAP_SECONDS:
            lead = len(state.prev_tail)
            frames = state.prev_tail + frames
            note = "prepended %d frames from previous file" % lead
        else:
            first_key = next((i for i, fr in enumerate(frames) if fr[2]), None)
            if first_key is None:
                log("%s%s: SKIPPED - no keyframe in file" % (label, name))
                summary.failed += 1
                return False
            note = "dropped %d orphan frames at start" % first_key
            frames = frames[first_key:]
    last_key = max(i for i, fr in enumerate(frames) if fr[2])
    state.prev_tail = frames[last_key:]
    state.prev_last_ft = frames[-1][0]
    if os.path.exists(dst) and os.path.getsize(dst) > 0:
        log("%s%s: exists, skipped" % (label, os.path.basename(dst)))
        summary.skipped += 1
        return False
    fps, rc, err = write_mp4(frames, dst, tmpdir, res.codec)
    if rc != 0:
        log("%s%s: FFMPEG FAILED (%s)" % (label, os.path.basename(dst), err))
        summary.failed += 1
        return False
    size_mb = os.path.getsize(dst) / 1e6
    t_first = ft_to_dt(frames[0][0]).strftime("%H:%M:%S")
    t_last = ft_to_dt(frames[-1][0]).strftime("%H:%M:%S")
    log("%s%s: OK %s %d frames %.1f fps utc %s-%s %.0f MB%s (%.0fs)" % (
        label, os.path.basename(dst), res.codec, len(frames), fps, t_first, t_last,
        size_mb, (" " + note) if note else "", time.time() - t0))
    summary.converted += 1
    return True


# --- input discovery ---------------------------------------------------------------

def _is_g64(name):
    return ".g64" in name.lower()


def sources_from_dir(root):
    """[(cam, name, loader)] for every .g64 under root; sub-folder names become cam."""
    items = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        rel = os.path.relpath(dirpath, root)
        cam = "" if rel == "." else rel.replace(os.sep, "/")
        for fn in sorted(filenames):
            if _is_g64(fn):
                full = os.path.join(dirpath, fn)
                items.append((cam, fn, lambda p=full: open(p, "rb").read()))
    return items


def sources_from_zip(path):
    """Read entries with zipfile; fall back to 7-Zip for truncated archives."""
    try:
        zf = zipfile.ZipFile(path)
        entries = sorted(i.filename for i in zf.infolist() if not i.is_dir() and _is_g64(i.filename))
        return [(_cam_of(e), e.split("/")[-1], lambda e=e: zf.read(e)) for e in entries]
    except zipfile.BadZipFile:
        pass
    seven = sevenzip_path()
    if not seven:
        raise RuntimeError("%s is not a valid zip (cut-off download?) and 7-Zip (7za) was not found"
                           % os.path.basename(path))
    log("zip central directory unreadable (cut-off or non-standard download); reading it with 7-Zip")
    lst = subprocess.run([seven, "l", "-slt", path], capture_output=True, text=True,
                         errors="replace", **_popen_flags()).stdout
    entries = sorted(l[7:].strip() for l in lst.splitlines()
                     if l.startswith("Path = ") and _is_g64(l))

    def loader(entry):
        return subprocess.run([seven, "e", "-so", "-y", path, entry],
                              capture_output=True, **_popen_flags()).stdout
    return [(_cam_of(e), e.split("/")[-1], lambda e=e: loader(e)) for e in entries]


def _cam_of(entry):
    parts = entry.replace("\\", "/").split("/")
    return "/".join(parts[:-1])


def collect_sources(path):
    if os.path.isdir(path):
        return sources_from_dir(path)
    if path.lower().endswith(".zip"):
        return sources_from_zip(path)
    if _is_g64(path):
        return [("", os.path.basename(path), lambda p=path: open(p, "rb").read())]
    raise RuntimeError("not a .g64 file, folder or .zip: %s" % path)


def default_output(path):
    if os.path.isdir(path):
        return os.path.join(path, "MP4")
    return os.path.join(os.path.dirname(os.path.abspath(path)), "MP4")


def run(inputs, out_root=None, limit=None):
    """Convert every input; returns a Summary."""
    summary = Summary()
    if not ffmpeg_path():
        log("ERROR: ffmpeg not found. Put ffmpeg(.exe) next to this program or on PATH.")
        summary.failed += 1
        return summary
    with tempfile.TemporaryDirectory() as tmpdir:
        for inp in inputs:
            out = out_root or default_output(inp)
            log("input: %s" % inp)
            log("output: %s" % out)
            try:
                sources = collect_sources(inp)
            except RuntimeError as e:
                log("ERROR: %s" % e)
                summary.failed += 1
                continue
            if not sources:
                log("no .g64 files found in %s" % inp)
                continue
            states = {}
            for cam, name, loader in sources:
                if limit and summary.converted >= limit:
                    break
                state = states.setdefault(cam, CamState())
                try:
                    convert_one(cam, name, loader, out, state, tmpdir, summary)
                except Exception as e:  # keep going with the next file
                    log("%s: ERROR %s" % (name, e))
                    summary.failed += 1
    log("done: %d converted, %d skipped, %d failed" % (summary.converted, summary.skipped, summary.failed))
    return summary


# --- entry point ------------------------------------------------------------------

def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        try:
            import g64_gui
        except ImportError:
            print(__doc__)
            return 1
        return g64_gui.main()
    parser = argparse.ArgumentParser(description="Convert Genetec G64 archives to MP4.")
    parser.add_argument("inputs", nargs="*", help=".g64 file, folder or .zip export")
    parser.add_argument("-o", "--output", help="output folder (default: 'MP4' next to the input)")
    parser.add_argument("--diag", metavar="FILE", help="describe a .g64 file and exit")
    parser.add_argument("--limit", type=int, help="convert at most N files (for testing)")
    parser.add_argument("--version", action="version", version="g64-converter " + __version__)
    args = parser.parse_args(argv)
    if args.diag:
        print(diagnose(args.diag))
        return 0
    if not args.inputs:
        parser.error("no input given")
    summary = run(args.inputs, args.output, args.limit)
    return 0 if summary.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
