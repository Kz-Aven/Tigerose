#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-run}"
APP_NAME="TigeroseAgent"
BUNDLE_NAME="Tigerose"
BUNDLE_ID="com.tigerose.agent"
MIN_SYSTEM_VERSION="14.0"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MACOS_DIR="$ROOT_DIR/macos"
DIST_DIR="$ROOT_DIR/dist"
APP_BUNDLE="$DIST_DIR/$BUNDLE_NAME.app"
APP_CONTENTS="$APP_BUNDLE/Contents"
APP_MACOS="$APP_CONTENTS/MacOS"
APP_BINARY="$APP_MACOS/$APP_NAME"
INFO_PLIST="$APP_CONTENTS/Info.plist"

pkill -x "$APP_NAME" >/dev/null 2>&1 || true
pkill -x "AventAgent" >/dev/null 2>&1 || true
# A forced GUI relaunch can bypass BackendProcess.stop() and orphan uvicorn.
# Multiple backends sharing TIGEROSE_HOME race on the same session snapshots.
pkill -f 'uvicorn server\.app:app --host 127\.0\.0\.1 --port' >/dev/null 2>&1 || true

cd "$MACOS_DIR"
swift build --disable-sandbox -c debug --product TigeroseAgent
BUILD_BINARY="$(swift build --disable-sandbox -c debug --show-bin-path)/TigeroseAgent"

rm -rf "$APP_BUNDLE"
mkdir -p "$APP_MACOS"
mkdir -p "$APP_CONTENTS/Resources"
cp "$BUILD_BINARY" "$APP_BINARY"
chmod +x "$APP_BINARY"

# Copy SwiftPM resource bundle if present
RES_BUNDLE="$(swift build --disable-sandbox -c debug --show-bin-path)/TigeroseAgent_TigeroseAgent.bundle"
if [[ -d "$RES_BUNDLE" ]]; then
  cp -R "$RES_BUNDLE" "$APP_CONTENTS/Resources/"
fi

# Flatten logo for Bundle.main fallback (AppResources); Bundle.module is unsafe in .app
LOGO_PNG="$MACOS_DIR/TigeroseAgent/Resources/AppIcon/logo.png"
if [[ -f "$LOGO_PNG" ]]; then
  cp "$LOGO_PNG" "$APP_CONTENTS/Resources/logo.png"
  xattr -c "$APP_CONTENTS/Resources/logo.png" 2>/dev/null || true
fi
LOGO_SVG="$MACOS_DIR/TigeroseAgent/Resources/AppIcon/logo.svg"
if [[ -f "$LOGO_SVG" ]]; then
  cp "$LOGO_SVG" "$APP_CONTENTS/Resources/logo.svg"
  xattr -c "$APP_CONTENTS/Resources/logo.svg" 2>/dev/null || true
fi

# App icon (square canvas, logo fitted without stretch)
ICON_SRC="$MACOS_DIR/TigeroseAgent/Resources/AppIcon/AppIcon.icns"
if [[ -f "$ICON_SRC" ]]; then
  cp "$ICON_SRC" "$APP_CONTENTS/Resources/AppIcon.icns"
fi

cat >"$INFO_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key>
  <string>zh_CN</string>
  <key>CFBundleExecutable</key>
  <string>$APP_NAME</string>
  <key>CFBundleIconFile</key>
  <string>AppIcon</string>
  <key>CFBundleIdentifier</key>
  <string>$BUNDLE_ID</string>
  <key>CFBundleInfoDictionaryVersion</key>
  <string>6.0</string>
  <key>CFBundleName</key>
  <string>$BUNDLE_NAME</string>
  <key>CFBundleDisplayName</key>
  <string>Tigerose 钛钢柔</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>0.1.7</string>
  <key>CFBundleVersion</key>
  <string>1</string>
  <key>LSMinimumSystemVersion</key>
  <string>$MIN_SYSTEM_VERSION</string>
  <key>NSPrincipalClass</key>
  <string>NSApplication</string>
  <key>NSHighResolutionCapable</key>
  <true/>
</dict>
</plist>
PLIST

# Bind Info.plist into an ad-hoc signature so LaunchServices can open the bundle.
# (A bare SwiftPM binary copy often shows "Info.plist=not bound".)
codesign --force --deep --sign - "$APP_BUNDLE" >/dev/null

launch_app() {
  if /usr/bin/open -n "$APP_BUNDLE"; then
    return 0
  fi
  echo "open failed (often Cursor agent sandbox blocking LaunchServices)." >&2
  echo "Run in Terminal.app instead:" >&2
  echo "  open -n \"$APP_BUNDLE\"" >&2
  return 1
}

case "$MODE" in
  run)
    launch_app
    ;;
  --debug|debug)
    lldb -- "$APP_BINARY"
    ;;
  --logs|logs)
    launch_app
    /usr/bin/log stream --info --style compact --predicate "process == \"$APP_NAME\""
    ;;
  --verify|verify)
    launch_app
    sleep 2
    pgrep -x "$APP_NAME" >/dev/null
    ;;
  --build|build)
    echo "Built $APP_BUNDLE"
    ;;
  *)
    echo "usage: $0 [run|--debug|--logs|--verify|--build]" >&2
    exit 2
    ;;
esac
