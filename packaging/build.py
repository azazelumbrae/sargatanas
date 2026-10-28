#!/usr/bin/env python3
"""Build standalone Sargatanas binaries with PyInstaller.

    python packaging/build.py

Produces:
    dist/sargatanas                    console binary (CLI + `sargatanas gui`)
    dist/Sargatanas.app                macOS app bundle (macOS only)

QEMU is intentionally NOT bundled: it is large and must match the host CPU,
so Sargatanas detects it at runtime (`sargatanas status` reports what it found).

Requirements: a CPython from python.org (it ships Tk as separate modules, so
PyInstaller can bundle it) plus `pip install pyinstaller psutil`.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
ICONS = ROOT / "assets" / "icons"


def icon_for_host() -> str | None:
    if platform.system() == "Windows":
        candidates = [ICONS / "icon.ico"]
    elif platform.system() == "Darwin":
        candidates = [ICONS / "icon.icns", ICONS / "sargatanas.png"]
    else:
        candidates = [ICONS / "sargatanas.png"]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def check_tk() -> None:
    try:
        import tkinter
    except ImportError:
        sys.exit("[-] This Python has no Tk. Use the python.org build so the GUI "
                 "can be bundled (brew's Tk is not self-contained).")
    spec = getattr(getattr(tkinter, "_tkinter", None), "__spec__", None)
    origin = getattr(spec, "origin", "built-in") if spec else "built-in"
    if origin == "built-in":
        sys.exit("[-] _tkinter is built into this interpreter, so PyInstaller cannot "
                 "bundle it. Use the python.org build.")
    print(f"[+] Tk {tkinter.TkVersion} from {origin}")


def project_version() -> str:
    """Read the version from pyproject so it is defined in exactly one place."""
    pyproject = (ROOT / "pyproject.toml").read_text()
    match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
    return match.group(1) if match else "0.0.0"


def build() -> None:
    check_tk()
    if not (ROOT / "assets" / "icons" / "sargatanas.png").exists():
        subprocess.run([sys.executable, str(ROOT / "dev.py"), "icons"], check=True)

    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile",
        "--name", "sargatanas",
        "--add-data", f"{ICONS / 'sargatanas.png'}{os.pathsep}assets/icons",
        "--add-data", f"{ROOT / 'assets' / 'sargatanas-icon.png'}{os.pathsep}assets",
        "--hidden-import", "sargatanas.gui_tk",
        "--hidden-import", "sargatanas.cloud",
        "--hidden-import", "sargatanas.torctl",
        "--hidden-import", "sargatanas.vpn",
        "--hidden-import", "sargatanas.torcfg",
        "--hidden-import", "tkinter",
        "--hidden-import", "tkinter.ttk",
        "--hidden-import", "tkinter.filedialog",
        "--hidden-import", "tkinter.messagebox",
    ]
    icon = icon_for_host()
    if icon:
        args += ["--icon", icon]
    if platform.system() == "Windows":
        args += ["--collect-all", "tkinter"]
    args.append(str(ROOT / "sargatanas.py"))

    print("[*] Running PyInstaller…")
    subprocess.run(args, check=True, cwd=ROOT)

    binary = DIST / ("sargatanas.exe" if platform.system() == "Windows" else "sargatanas")
    if not binary.exists():
        sys.exit("[-] Build finished but the binary is missing")
    size = binary.stat().st_size / (1024 * 1024)
    print(f"[+] {binary} ({size:.1f} MB)")

    if platform.system() == "Darwin":
        _make_macos_app()


def _make_macos_app() -> None:
    """Wrap the console binary in a .app so it can be double-clicked."""
    version = project_version()
    app = DIST / "Sargatanas.app"
    if app.exists():
        shutil.rmtree(app)
    macos = app / "Contents" / "MacOS"
    res = app / "Contents" / "Resources"
    macos.mkdir(parents=True)
    res.mkdir(parents=True)
    shutil.copy2(DIST / "sargatanas", macos / "Sargatanas")
    shutil.copy2(ICONS / "sargatanas.png", res / "sargatanas.png")
    (app / "Contents" / "Info.plist").write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Sargatanas</string>
  <key>CFBundleDisplayName</key><string>Sargatanas</string>
  <key>CFBundleIdentifier</key><string>com.sargatanas.app</string>
  <key>CFBundleVersion</key><string>{version}</string>
  <key>CFBundleShortVersionString</key><string>{version}</string>
  <key>CFBundleExecutable</key><string>Sargatanas</string>
  <key>CFBundleIconFile</key><string>sargatanas.png</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
</dict>
</plist>
""")
    print(f"[+] {app}")


if __name__ == "__main__":
    build()
