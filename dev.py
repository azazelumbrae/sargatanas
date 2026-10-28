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
ICON_SRC = ROOT / "assets" / "sargatanas-icon.png"   # 1024px master, metadata-free
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


def icons() -> None:
    """Regenerate every icon size from the PNG master.

    Pillow is a build-time-only dependency (not shipped with the app). It
    re-encodes from raw pixels, so no C2PA/EXIF metadata carries over.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print("[!] pip install pillow to regenerate icons")
        sys.exit(1)
    if not ICON_SRC.exists():
        print(f"[!] Missing {ICON_SRC}")
        sys.exit(1)
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    img = Image.open(ICON_SRC).convert("RGB")
    side = min(img.size)
    img = img.crop(((img.width - side) // 2, (img.height - side) // 2,
                    (img.width - side) // 2 + side, (img.height - side) // 2 + side))

    mask = Image.new("L", (1024, 1024), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, 1023, 1023), radius=225, fill=255)
    master = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
    master.paste(img.resize((1024, 1024), Image.LANCZOS), (0, 0), mask)

    for size in (16, 32, 48, 64, 128, 256, 512, 1024):
        master.resize((size, size), Image.LANCZOS).save(ICON_DIR / f"{size}x{size}.png",
                                                        optimize=True)
    master.save(ICON_DIR / "sargatanas.png", optimize=True)
    master.save(ICON_DIR / "icon.ico",
                sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    master.save(ICON_DIR / "icon.icns")
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
