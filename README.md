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

## Using the Windows build

1. Download `g64-converter-windows.zip` from the Releases page and unzip it anywhere.
   The folder holds `g64-converter.exe`, `ffmpeg.exe` and `7za.exe`; keep them together.
2. Either **drag** a `.zip` export, a folder of `.g64` files, or a single `.g64` file
   onto `g64-converter.exe`, or **double-click** it to get a small window with
   Source / Output / Convert.
3. MP4 files appear in a folder named `MP4` next to the input, one sub-folder per
   camera, same file names as the originals.

Windows SmartScreen shows "unknown publisher" the first time because the build is
not code-signed. Click *More info* → *Run anyway*.

### Command line

```
g64-converter.exe CAM01.zip                 # MP4 next to the zip
g64-converter.exe D:\export\CAM01 -o E:\MP4 # folder in, custom output
g64-converter.exe --diag some_file.g64      # describe the file
```

## Running from source (macOS / Linux / Windows with Python)

Requires Python 3.9+ and `ffmpeg` on PATH (`brew install ffmpeg`, `apt install ffmpeg`).
`7z` (p7zip / 7-Zip) is only needed for cut-off zip downloads.

```
python3 g64_to_mp4.py CAM01.zip
python3 g64_to_mp4.py /path/to/folder -o /path/to/MP4
python3 -m unittest discover -s tests
```

## Building the Windows package

The GitHub Actions workflow in `.github/workflows/build-windows.yml` builds it on a
Windows runner: run tests, download static `ffmpeg.exe` (BtbN builds) and `7za.exe`
(7-Zip extra package), run PyInstaller, zip the folder. It runs on every tag `v*`
(and attaches the zip to the release) or manually from the Actions tab.

To build locally on Windows:

```
pip install pyinstaller
pyinstaller --noconfirm --onedir --name g64-converter --hidden-import g64_gui g64_to_mp4.py
copy ffmpeg.exe dist\g64-converter\
copy 7za.exe dist\g64-converter\
```

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
