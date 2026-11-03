# Third-party notices

The downloadable packages bundle two programs that g64-converter runs as
separate processes. Neither is linked into the converter: it invokes them
through `subprocess`, so they stay independent programs that happen to be
distributed alongside it.

## FFmpeg

Used to mux the reassembled elementary stream into MP4 with `-c copy`.

| | |
|---|---|
| Project | <https://ffmpeg.org> |
| Source code | <https://github.com/FFmpeg/FFmpeg> |
| License | GNU Lesser General Public License v2.1 or later |
| Windows / Linux builds | <https://github.com/BtbN/FFmpeg-Builds> (`n8.1-latest-*-lgpl`) |
| macOS build | compiled from the upstream tag in CI |

The converter never encodes video, only remuxes it, so it has no use for the
GPL-licensed parts of FFmpeg (x264, x265, libpostproc and the GPL filters).
The packages therefore bundle an **LGPL** build, configured without
`--enable-gpl`. Every release build asserts this by checking the configure
line printed by `ffmpeg -version`; the build fails if a GPL build is picked up.

No trustworthy prebuilt static macOS ffmpeg is configured that way — the
widely used ones all enable the GPL parts — so the macOS package's ffmpeg is
compiled from the upstream release tarball during the build instead, with
`--disable-autodetect` so that no external codec library is linked in.

Run `ffmpeg -version` on the bundled binary to see the exact version and
configure flags of the build you received. The corresponding source for any
FFmpeg version is available from the upstream repository above.

## 7-Zip (7za / 7zz)

Used only to read zip exports whose download was cut off part-way; the
standard library handles complete zips.

| | |
|---|---|
| Project | <https://www.7-zip.org> |
| Source code | <https://github.com/ip7z/7zip> |
| License | GNU Lesser General Public License v2.1 or later |

## Python

The packages embed the CPython runtime, bundled by PyInstaller.

| | |
|---|---|
| Project | <https://www.python.org> |
| License | Python Software Foundation License v2 |
