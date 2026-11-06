# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build for the USBCam Windows viewer.

Bundles adb.exe + its two DLLs from the local Android SDK so end users do
not need Android platform-tools installed. If no SDK is present at build
time the app still works: it downloads platform-tools on first run.

Build:
    pyinstaller --noconfirm --clean USBCam.spec
"""
import os
from pathlib import Path


def _platform_tools_dir():
    candidates = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "Android" / "Sdk" / "platform-tools")
    sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if sdk:
        candidates.append(Path(sdk) / "platform-tools")
    candidates.append(Path("platform-tools"))
    for c in candidates:
        if (c / "adb.exe").is_file():
            return c
    return None


binaries = []
_pt = _platform_tools_dir()
if _pt is not None:
    for _name in ("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll"):
        _f = _pt / _name
        if _f.is_file():
            binaries.append((str(_f), "platform-tools"))
    print(f"Bundling platform-tools from {_pt}")
else:
    print("WARNING: platform-tools not found; the exe will download adb on first run")


a = Analysis(
    ['pc/receiver.py'],
    pathex=[],
    binaries=binaries,
    datas=[],
    hiddenimports=['pyvirtualcam', 'numpy'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='USBCam',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
