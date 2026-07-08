#!/usr/bin/env bash
# Assemble TransDub Studio.app (self-bootstrapping) and package it into a .dmg.
# Usage: build_dmg.sh <version>   e.g. build_dmg.sh 1.0.0
set -euo pipefail

VER="${1:-0.0.0}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
BUILD="$ROOT/dist/mac"
APP="$BUILD/TransDub Studio.app"

echo "==> Building TransDub Studio.app v$VER"
rm -rf "$BUILD"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# 1) payload = tracked source (excludes .venv / models / secrets automatically)
echo "==> Archiving source payload..."
git -C "$ROOT" archive --format=tar HEAD | gzip > "$APP/Contents/Resources/payload.tar.gz"

# 2) icon + scripts
cp "$ROOT/TransDub Studio.app.noindex/Contents/Resources/TransDubStudio.icns" \
   "$APP/Contents/Resources/app.icns"
cp "$HERE/first_run_setup.sh" "$APP/Contents/Resources/first_run_setup.sh"
cp "$HERE/launcher.sh"        "$APP/Contents/MacOS/TransDubStudio"
chmod +x "$APP/Contents/MacOS/TransDubStudio" "$APP/Contents/Resources/first_run_setup.sh"

# 3) Info.plist
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>TransDub Studio</string>
    <key>CFBundleDisplayName</key><string>TransDub Studio</string>
    <key>CFBundleIdentifier</key><string>com.jianzhinotes.transdubstudio</string>
    <key>CFBundleExecutable</key><string>TransDubStudio</string>
    <key>CFBundleIconFile</key><string>app</string>
    <key>CFBundleShortVersionString</key><string>$VER</string>
    <key>CFBundleVersion</key><string>$VER</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>LSMinimumSystemVersion</key><string>11.0</string>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

# 4) dmg with an Applications symlink for drag-install
ln -s /Applications "$BUILD/Applications"
mkdir -p "$ROOT/dist"
DMG="$ROOT/dist/TransDub-Studio-$VER.dmg"
echo "==> Creating $DMG"
hdiutil create -volname "TransDub Studio" -srcfolder "$BUILD" -ov -format UDZO "$DMG"
echo "==> Done: $DMG"
