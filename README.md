# g64-converter

Converts Genetec Security Center / Omnicast **`.g64`** video exports to standard **MP4**
files, without the Genetec Video Player and without re-encoding (original quality,
fast). Runs on Windows 10/11 as a stand-alone program; the same script also runs on
macOS and Linux with Python 3.

## What it handles

| Supported | Not yet |
|---|---|
| `Genetec Omnicast Archive v5.31` exports | Other archive versions (please send a sample) |
| H.264 and H.265 video, one stream per file | MJPEG / MPEG-4 video |
| Zip exports, including cut-off downloads (OneDrive stops at 20 GB) | `.g64x` containers |
| Files cut off mid-way (keeps every complete frame) | Audio tracks (ignored) |
| | Encrypted / password-protected exports |

Run `g64-converter --diag FILE.g64` on anything that fails: it prints the container
version, stream ids, RTP payload types and codec, which is what is needed to add support.

## Download and run

Nothing to install: each package is self-contained and carries its own `ffmpeg`
and `7z`. Pick your platform on the
[Releases](https://github.com/ielton/g64-converter/releases) page.

| Platform | Package |
|---|---|
| Windows 10/11 (x64) | `g64-converter-windows-x64.zip` |
| macOS (Apple Silicon) | `g64-converter-macos-arm64.zip` |
| Linux (x64, glibc 2.35+) | `g64-converter-linux-x64.tar.gz` |

Intel Macs and other architectures are not packaged; run from source there.

Either point the window at a `.zip` export, a folder of `.g64` files or a single
`.g64` file, or pass it on the command line. MP4 files appear in a folder named
`MP4` next to the input, one sub-folder per camera, with the same file names as
the originals.

### Windows

Unzip anywhere and keep the folder's contents together. It holds two
executables, because one program cannot both open a window with no console
attached and still print to the command line:

- **`g64-converter-gui.exe`** — double-click this one; no terminal window appears.
- `g64-converter.exe` — the command line build. Files can also be dragged onto it.

SmartScreen shows "unknown publisher" the first time because the build is not
code-signed. Click *More info* → *Run anyway*.

### macOS

Unzip and move `g64-converter.app` wherever you like. The app is not signed or
notarized, so Gatekeeper blocks a plain double-click the first time: **right-click
the app → Open**, then confirm. macOS remembers the choice afterwards.

The command line build sits inside the bundle:

```
g64-converter.app/Contents/MacOS/g64-converter CAM01.zip
```

### Linux

```
tar -xzf g64-converter-linux-x64.tar.gz
./g64-converter/g64-converter CAM01.zip   # no arguments opens the window
```

### Command line

The same arguments work on every platform:

```
g64-converter CAM01.zip                 # MP4 next to the zip
g64-converter D:\export\CAM01 -o E:\MP4 # folder in, custom output
g64-converter --diag some_file.g64      # describe the file
```

## Running from source

Requires Python 3.9+ with tkinter, plus `ffmpeg` either on PATH
(`brew install ffmpeg`, `apt install ffmpeg`) or placed next to
`g64_to_mp4.py`. `7z` (p7zip / 7-Zip) is only needed for cut-off zip downloads.
`G64_FFMPEG` and `G64_7Z` override the lookup with an explicit path.

```
python3 g64_to_mp4.py CAM01.zip
python3 g64_to_mp4.py /path/to/folder -o /path/to/MP4
python3 g64_to_mp4.py                   # no arguments opens the window
python3 -m unittest discover -s tests
```

## Building the packages

`.github/workflows/build.yml` builds all three platforms, on every tag `v*` or
manually from the Actions tab: it runs the tests, downloads a static **LGPL**
ffmpeg and the 7-Zip console tool for the target platform, runs PyInstaller
against `g64-converter.spec` and packages the result. The converter only ever
remuxes with `-c copy`, so it never needs the GPL-only parts of ffmpeg; the
build asserts the bundled binary was configured without `--enable-gpl`. See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

To publish a version, create the release first so it is authored by you, then
the workflow attaches each platform's package to it:

```
gh release create v1.2.0 --title "v1.2.0" --notes "What changed"
```

To build locally for the platform you are on:

```
pip install -r requirements-dev.txt
pyinstaller --noconfirm g64-converter.spec
```

The spec builds one console executable and, on Windows and macOS, a second
windowed one with no console attached. `ffmpeg` and `7z` then have to be copied
next to the executables — inside `Contents/MacOS` for the macOS bundle.

## How it works

The full story, byte layout and pipeline are in [FORMAT.md](FORMAT.md). In short:

A `.g64` file is a sequence of frame blocks, each holding the raw RTP packets the
camera sent, with a FILETIME timestamp and a keyframe flag. The converter walks the
blocks, reassembles the H.264/H.265 NAL units from the RTP packets (RFC 6184 /
RFC 7798), writes an Annex B elementary stream and lets ffmpeg mux it into MP4 with
`-c copy`. Each file starts with a few P-frames whose keyframe sits at the end of the
previous file, so when files of one camera are contiguous the previous file's last
GOP is prepended and no frame is lost. The format details are documented at the top
of `g64_to_mp4.py`.

## Notes

- The time in each file name is the camera's local time; the MP4 `creation_time`
  metadata carries the UTC start time of the first frame.
- Converting removes Genetec's digital signature. Keep the original `.g64` files if
  the footage may be needed as evidence.
