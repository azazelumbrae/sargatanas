#!/usr/bin/env bash
# Sargatanas build helper: run the tests, build the wheel, render icons.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

ICON_SRC="assets/sargatanas-icon.svg"
ICON_DIR="assets/icons"

usage() {
    cat <<'EOF'
Usage: ./build.sh [test|dist|icons|clean]

  test   Run the unit test suite (default)
  dist   Build sdist + wheel into dist/
  icons  Render PNG/ICO/ICNS from the SVG (needs ImageMagick or rsvg-convert)
  clean  Remove build artefacts
EOF
}

run_tests() {
    echo "[*] Running tests..."
    python3 -m unittest discover -s tests -t . -v
}

build_dist() {
    run_tests
    echo "[*] Building package..."
    python3 -m pip install --upgrade build >/dev/null
    python3 -m build
    echo "[+] Artefacts:"
    ls -1 dist/
}

render_icons() {
    mkdir -p "$ICON_DIR"
    if command -v rsvg-convert >/dev/null; then
        render() { rsvg-convert -w "$1" -h "$1" "$ICON_SRC" -o "$2"; }
    elif command -v magick >/dev/null; then
        render() { magick -background none "$ICON_SRC" -resize "$1x$1" "$2"; }
    elif command -v convert >/dev/null; then
        render() { convert -background none "$ICON_SRC" -resize "$1x$1" "$2"; }
    else
        echo "[!] Install rsvg-convert or ImageMagick to render icons"
        return 1
    fi

    for size in 16 32 64 128 256 512 1024; do
        render "$size" "$ICON_DIR/${size}x${size}.png"
    done

    if command -v magick >/dev/null || command -v convert >/dev/null; then
        tool=$(command -v magick || command -v convert)
        "$tool" -background none "$ICON_SRC" -define icon:auto-resize=256,128,64,48,32,16 \
            "$ICON_DIR/icon.ico"
    fi

    if command -v iconutil >/dev/null; then
        set_dir="$ICON_DIR/Sargatanas.iconset"
        rm -rf "$set_dir" && mkdir -p "$set_dir"
        for size in 16 32 128 256 512; do
            render "$size" "$set_dir/icon_${size}x${size}.png"
            render "$((size * 2))" "$set_dir/icon_${size}x${size}@2x.png"
        done
        iconutil -c icns "$set_dir" -o "$ICON_DIR/icon.icns"
        rm -rf "$set_dir"
    fi
    echo "[+] Icons in $ICON_DIR"
}

case "${1:-test}" in
    test) run_tests ;;
    dist) build_dist ;;
    icons) render_icons ;;
    clean) rm -rf dist build *.egg-info .pytest_cache && find . -name __pycache__ -type d -exec rm -rf {} + ;;
    -h|--help|help) usage ;;
    *) usage; exit 1 ;;
esac
