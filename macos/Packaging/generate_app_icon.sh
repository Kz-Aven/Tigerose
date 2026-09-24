#!/usr/bin/env bash
# Generate AppIcon.icns from logo.png/svg — fit inside square, never stretch.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ICON_DIR="$ROOT/macos/TigeroseAgent/Resources/AppIcon"
SRC="${1:-$ICON_DIR/logo.png}"
if [[ ! -f "$SRC" && -f "$ICON_DIR/logo.svg" ]]; then
  SRC="$ICON_DIR/logo.svg"
fi
if [[ ! -f "$SRC" ]]; then
  echo "Missing logo: $SRC" >&2
  exit 1
fi

python3 - "$SRC" "$ICON_DIR/AppIcon.icns" <<'PY'
import subprocess, tempfile, shutil, os, sys
from pathlib import Path

src = Path(sys.argv[1])
out_icns = Path(sys.argv[2])
work = Path(tempfile.mkdtemp())
dmg = work / "cs.dmg"
mnt = work / "mnt"
mnt.mkdir()
sx = "@" + "2x"
names = [
    "icon_16x16.png",
    f"icon_16x16{sx}.png",
    "icon_32x32.png",
    f"icon_32x32{sx}.png",
    "icon_128x128.png",
    f"icon_128x128{sx}.png",
    "icon_256x256.png",
    f"icon_256x256{sx}.png",
    "icon_512x512.png",
    f"icon_512x512{sx}.png",
]
sizes = [16, 32, 32, 64, 128, 256, 256, 512, 512, 1024]

def run(cmd):
    subprocess.check_call(cmd)

try:
    # Case-sensitive volume: some hosts still benefit; CI APFS may collide otherwise.
    run(["hdiutil", "create", "-size", "20m", "-fs", "Case-sensitive APFS",
         "-volname", "TigeroseIcons", str(dmg)])
    run(["hdiutil", "attach", str(dmg), "-mountpoint", str(mnt), "-nobrowse"])
    master = work / "master.png"
    run(["magick", "-background", "none", str(src), "-resize", "1024x1024",
         "(", "-size", "1024x1024", "xc:none", ")",
         "+swap", "-gravity", "center", "-compose", "over", "-composite", f"PNG32:{master}"])
    px = {}
    for size in (16, 32, 64, 128, 256, 512, 1024):
        out = work / f"s{size}.png"
        run(["magick", str(master), "-resize", f"{size}x{size}", f"PNG32:{out}"])
        px[size] = out
    iconset = mnt / "AppIcon.iconset"
    iconset.mkdir()
    for name, size in zip(names, sizes):
        shutil.copyfile(px[size], iconset / name)
    assert len(os.listdir(iconset)) == 10
    run(["iconutil", "-c", "icns", str(iconset), "-o", str(out_icns)])
    subprocess.call(["xattr", "-c", str(out_icns)])
    print(f"Wrote {out_icns}")
finally:
    subprocess.call(["hdiutil", "detach", str(mnt)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    shutil.rmtree(work, ignore_errors=True)
PY
