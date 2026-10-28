#!/usr/bin/env python3
"""Sargatanas development helper (cross-platform).

    python dev.py test     run the test suite (default)
    python dev.py dist     build sdist + wheel into dist/
    python dev.py icons    render PNG/ICO/ICNS from the SVG
    python dev.py clean    remove build artefacts
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ICON_SRC = ROOT / "assets" / "sargatanas-icon.svg"
ICON_DIR = ROOT / "assets" / "icons"


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), check=check)


def test() -> None:
    print("[*] Running tests...")
    run(sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-v")


def dist() -> None:
    test()
    print("[*] Building package...")
    run(sys.executable, "-m", "pip", "install", "--upgrade", "build")
    run(sys.executable, "-m", "build")
    print("[+] Artefacts:")
    for p in sorted((ROOT / "dist").glob("*")):
        print(f"  - {p.name}")


def _renderer() -> list[str] | None:
    """Return an argv-prefix that turns (svg, size, out) into a PNG."""
    for tool in ("rsvg-convert",):
        if shutil.which(tool):
            return [tool, "-w", "{size}", "-h", "{size}", "{svg}", "-o", "{out}"]
    for tool in ("magick", "convert"):
        if shutil.which(tool):
            return [tool, "-background", "none", "{svg}", "-resize", "{size}x{size}", "{out}"]
    return None


def icons() -> None:
    render = _renderer()
    if not render:
        print("[!] Install rsvg-convert or ImageMagick to render icons")
        sys.exit(1)
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    for size in (16, 32, 64, 128, 256, 512, 1024):
        argv = [p.format(size=str(size), svg=str(ICON_SRC),
                          out=str(ICON_DIR / f"{size}x{size}.png")) for p in render]
        run(*argv)

    ico_tool = shutil.which("magick") or shutil.which("convert")
    if ico_tool:
        run(ico_tool, "-background", "none", str(ICON_SRC),
            "-define", "icon:auto-resize=256,128,64,48,32,16",
            str(ICON_DIR / "icon.ico"))

    if shutil.which("iconutil"):
        icon_set = ICON_DIR / "Sargatanas.iconset"
        if icon_set.exists():
            shutil.rmtree(icon_set)
        icon_set.mkdir(parents=True)
        for size in (16, 32, 128, 256, 512):
            for suffix, px in (("", size), ("@2x", size * 2)):
                argv = [p.format(size=str(px), svg=str(ICON_SRC),
                                  out=str(icon_set / f"icon_{size}x{size}{suffix}.png"))
                        for p in render]
                run(*argv)
        run("iconutil", "-c", "icns", str(icon_set), "-o", str(ICON_DIR / "icon.icns"))
        shutil.rmtree(icon_set)
    print(f"[+] Icons in {ICON_DIR}")


def clean() -> None:
    for name in ("dist", "build", ".pytest_cache"):
        p = ROOT / name
        if p.exists():
            shutil.rmtree(p)
    for egg in ROOT.glob("*.egg-info"):
        shutil.rmtree(egg)
    for pycache in ROOT.rglob("__pycache__"):
        shutil.rmtree(pycache, ignore_errors=True)


def usage() -> None:
    print(__doc__)


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "test"
    {"test": test, "dist": dist, "icons": icons, "clean": clean,
     "help": usage, "-h": usage, "--help": usage}.get(cmd, usage)()
    return 0


if __name__ == "__main__":
    sys.exit(main())
