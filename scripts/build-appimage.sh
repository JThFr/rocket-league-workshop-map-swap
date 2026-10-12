#!/usr/bin/env bash
# Build a Linux AppImage for rpmap locally. Mirrors the CI job.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # repo root
cd "$here"

python3 -m pip install --user --upgrade pyinstaller
python3 -m PyInstaller --onefile --console --name rpmap scripts/rpmap.py --noconfirm

APPDIR="$here/rpmap.AppDir"
rm -rf "$APPDIR"
install -Dm755 dist/rpmap "$APPDIR/usr/bin/rpmap"
install -Dm644 assets/rpmap.desktop "$APPDIR/rpmap.desktop"
install -Dm644 assets/rpmap.png "$APPDIR/rpmap.png"
cp assets/rpmap.png "$APPDIR/.DirIcon"
cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/bin/rpmap" "$@"
EOF
chmod +x "$APPDIR/AppRun"

TOOL="${APPIMAGETOOL:-/tmp/appimagetool}"
if [ ! -x "$TOOL" ]; then
    TOOL=/tmp/appimagetool
    curl -fL -o "$TOOL" \
        https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
    chmod +x "$TOOL"
fi

ARCH=x86_64 "$TOOL" --appimage-extract-and-run "$APPDIR" "$here/rpmap-x86_64.AppImage"
echo "built: $here/rpmap-x86_64.AppImage"
