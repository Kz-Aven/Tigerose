#!/usr/bin/env bash
# Build an isolated release without stopping the running development app.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
VERSION="0.1.7"
BUILD_DATE="$(date +%Y%m%d)"
RELEASE="$ROOT/dist/release-$BUILD_DATE"
APP="$RELEASE/Tigerose.app"
DMG="$RELEASE/Tigerose-$VERSION-$BUILD_DATE-arm64.dmg"
export CLANG_MODULE_CACHE_PATH="${CLANG_MODULE_CACHE_PATH:-/private/tmp/tigerose-swift-module-cache}"
cd "$ROOT/macos"
swift build --disable-sandbox -c release --product TigeroseAgent
BIN="$(swift build --disable-sandbox -c release --show-bin-path)"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN/TigeroseAgent" "$APP/Contents/MacOS/TigeroseAgent"
cp -R "$BIN/TigeroseAgent_TigeroseAgent.bundle" "$APP/Contents/Resources/"
for asset in logo.png logo.svg AppIcon.icns; do
  cp "$ROOT/macos/TigeroseAgent/Resources/AppIcon/$asset" "$APP/Contents/Resources/"
done
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleDevelopmentRegion</key><string>zh_CN</string>
<key>CFBundleExecutable</key><string>TigeroseAgent</string>
<key>CFBundleIconFile</key><string>AppIcon</string>
<key>CFBundleIdentifier</key><string>com.tigerose.agent</string>
<key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
<key>CFBundleName</key><string>Tigerose</string>
<key>CFBundleDisplayName</key><string>Tigerose</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>$VERSION</string>
<key>CFBundleVersion</key><string>$BUILD_DATE</string>
<key>LSMinimumSystemVersion</key><string>14.0</string>
<key>NSPrincipalClass</key><string>NSApplication</string>
<key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST
bash "$ROOT/macos/Packaging/bundle_runtime.sh" "$APP"
codesign --verify --deep --strict "$APP"
mkdir -p "$RELEASE/dmg-root"
ditto "$APP" "$RELEASE/dmg-root/Tigerose.app"
ln -sfn /Applications "$RELEASE/dmg-root/Applications"
hdiutil create -volname Tigerose -srcfolder "$RELEASE/dmg-root" -ov -format UDZO "$DMG"
hdiutil verify "$DMG"
(cd "$RELEASE" && shasum -a 256 "$(basename "$DMG")" > "$(basename "$DMG").sha256")
printf 'Built %s\n' "$DMG"
