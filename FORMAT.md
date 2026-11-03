# The G64 file format and how it was converted

This document explains what is inside a Genetec `.g64` export and how this tool
turns it into MP4 without the Genetec Video Player. Everything below was found by
inspecting real exports (header `Genetec Omnicast Archive v5.31`) with a hex viewer
and small Python scripts; nothing comes from Genetec documentation.

## 1. Finding out what the file is

* The file starts with the ASCII string `Genetec Omnicast Archive v5.31`, followed
  by a Windows FILETIME and the camera name in UTF-16LE (twice).
* `ffprobe` rejects the file, so it is not a normal media container.
* Searching for H.264 Annex B start codes (`00 00 00 01`) finds nothing, but the
  8-byte sequence `00 00 03 00 00 03 00 00` appears thousands of times. That is the
  H.264/H.265 *emulation prevention* pattern, so compressed video is in there, just
  not framed as a plain elementary stream.
* Most of the file is made of records exactly 1406 bytes apart, each beginning with
  `00 1f 05 78 00 00 80 60 ...`:
  * `05 78` is 1400, the length of what follows;
  * `80 60` is an **RTP header** (version 2, payload type 96), followed by a
    sequence number that increases by one per record and a timestamp that stays
    constant within a frame;
  * the RTP payload starts with `7c 85` = H.264 **FU-A** fragment, start bit set,
    NAL type 5 (IDR), then `7c 05 ...` continuation fragments.

So a `.g64` is simply the RTP packet stream the camera sent, stored as received,
with a small Genetec envelope around every frame. Nothing is encrypted.

## 2. Layout

```
file   := header | frame-block ( trailer frame-block )* [footer]

header := "Genetec Omnicast Archive v5.31" FILETIME camera-name ... (variable, ~40 KB,
          may contain a per-second index table of FILETIME + file offset)

frame-block :=
    FILETIME        8 bytes, little-endian, UTC (100 ns ticks since 1601-01-01)
    flags           1 byte   0x04 = keyframe
    payload_size    4 bytes, little-endian
    payload         payload_size bytes:
        prefix      12 bytes  80 66 00 00 00 00 00 00 00 00 00 30
        record*
record :=
    00 xx           2 bytes  xx = stream id (seen: 1f, 1d, 41; constant per file)
    length          2 bytes, big-endian
    00 00           2 bytes
    rtp-packet      length bytes (RFC 3550 header + payload)

trailer :=
    hash            64 bytes  (looks like a SHA-512 / signature of the block; not needed)
    block_size      4 bytes, little-endian = 13 + payload_size of the block before it
```

One frame block holds all RTP packets of one video frame. A keyframe block starts
with SPS and PPS (H.264) or VPS/SPS/PPS (H.265) as single-NAL packets, then the
fragmented I-frame. P-frames are usually one single-NAL packet.

Audio or metadata streams, when present, appear as RTP packets with another payload
type (an intercom export contained one packet of type 126). They are ignored.

## 3. Codec

* H.264 (RFC 6184): single NAL unit packets (types 1-23), STAP-A (24), FU-A (28).
* H.265 (RFC 7798): single NAL unit packets (types 0-47), AP (48), FU (49).

The codec is detected per file by counting parameter-set packets: H.264 SPS/PPS
start with a byte whose low 5 bits are 7 or 8; H.265 VPS/SPS/PPS/FU start with
`40 01`, `42 01`, `44 01` or `62 01`. Cameras in the same system used both codecs
(older ones H.264 at 3648x2052, newer ones H.265 at 3840x2160).

## 4. Conversion pipeline

1. Walk the frame blocks (resynchronising on the payload prefix if a block is bad).
2. Keep RTP packets with payload type 96, in file order.
3. Depacketize them into NAL units and write each one with an Annex B start code
   (`00 00 00 01`), giving a raw H.264 or H.265 elementary stream.
4. Let ffmpeg mux that stream into MP4 with `-c copy` (no re-encoding, original
   quality). The frame rate is computed from the FILETIME of the first and last
   frame (`frames / duration`); the first frame's UTC time goes into the MP4
   `creation_time` metadata. H.265 gets the `hvc1` tag so QuickTime plays it.

## 5. Things that need care

* **Orphan P-frames.** Each 20-minute export starts at an exact clock time, usually
  in the middle of a GOP. The first 1-4 seconds are P-frames whose keyframe is at
  the end of the previous file. When the previous file of the same camera ends
  less than 2 s before, its last GOP is prepended, so nothing is lost. For the
  first file of a sequence those frames are dropped (they cannot be decoded).
* **Cut-off files.** OneDrive stops folder downloads at 20 GB and simply truncates
  the zip, leaving no central directory. `unzip` and Finder refuse such a file;
  7-Zip reads it from the local headers. The last `.g64` entry is cut mid-way; the
  parser keeps every complete frame and stops at the truncated block.
* **Lost packets.** The camera's RTP stream sometimes misses a packet (about 80 per
  20 minutes in the exports seen). A missing fragment corrupts one frame, which the
  decoder conceals; the file is not affected otherwise.
* **Time zone.** FILETIMEs are UTC; the time in the file name is the camera's
  local time (UTC-7 in the exports seen).
* **Evidence.** Converting drops Genetec's signature (the 64-byte trailer). Keep
  the original `.g64` files if chain of custody matters.
