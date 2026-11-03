# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build definition.

One Analysis feeds two executables so both are guaranteed to load the same
bundled runtime:

  g64-converter      console build, keeps stdout/stderr for the command line
  g64-converter-gui  windowed build, opens with no terminal window attached

A windowed build has no stdout, so `print()` and `--help` write nowhere; that is
why the command line keeps its own console executable instead of one binary
doing both. Linux has no console/windowed distinction, so it ships only the
first one.

On macOS the collected folder is additionally wrapped in a .app bundle whose
CFBundleExecutable is the windowed build, so double-clicking opens the window
without a Terminal session.
"""
import sys

WINDOWED = sys.platform in ("win32", "darwin")

a = Analysis(
    ["g64_to_mp4.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=["g64_gui"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

_common = dict(
    exclude_binaries=True,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

cli = EXE(pyz, a.scripts, [], name="g64-converter", console=True, **_common)

# BUNDLE takes its executable from the first EXE handed to COLLECT, so the
# windowed build has to come first on macOS for the .app to open the window.
executables = [cli]
if WINDOWED:
    gui = EXE(pyz, a.scripts, [], name="g64-converter-gui", console=False, **_common)
    executables = [gui, cli] if sys.platform == "darwin" else [cli, gui]

coll = COLLECT(
    *executables,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="g64-converter",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="g64-converter.app",
        icon=None,
        bundle_identifier="com.github.ielton.g64converter",
        info_plist={
            "CFBundleExecutable": "g64-converter-gui",
            "CFBundleDisplayName": "G64 to MP4 converter",
            "NSHighResolutionCapable": True,
            # Nothing in the converter reads the system clock as local time for
            # output, but Finder still asks; keep the app out of the dock menus
            # it does not need.
            "LSApplicationCategoryType": "public.app-category.video",
        },
    )
