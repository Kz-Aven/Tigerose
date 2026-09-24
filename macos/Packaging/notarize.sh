#!/usr/bin/env bash
# Stub for later Developer ID + notarization. Not used in v0 internal builds.
set -euo pipefail
echo "notarize.sh: not implemented yet (v0 ships unsigned)." >&2
echo "When ready: codesign --deep --force --options runtime --sign \"Developer ID Application: …\" Tigerose.app" >&2
echo "Then: xcrun notarytool submit … && xcrun stapler staple Tigerose.app" >&2
exit 1
