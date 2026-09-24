#!/usr/bin/env bash
# Bundle CPython + server into a built Tigerose.app (arm64).
# Usage: ./macos/Packaging/bundle_runtime.sh [path/to/Tigerose.app]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
APP="${1:-"$ROOT/dist/Tigerose.app"}"
APP="$(cd "$APP" && pwd)"
RUNTIME="$APP/Contents/Resources/runtime"
PY_VERSION="${PY_VERSION:-3.12.8}"

echo "==> Bundling runtime into $APP"
mkdir -p "$RUNTIME"

if [[ ! -d "$APP/Contents/MacOS" ]]; then
  echo "App bundle not found: $APP" >&2
  echo "Build first: ./script/build_and_run.sh (or pass path to .app)" >&2
  exit 1
fi

# Prefer already-downloaded standalone CPython if present.
STANDALONE="${TIGEROSE_PYTHON_STANDALONE:-${AVENT_PYTHON_STANDALONE:-}}"
if [[ -z "$STANDALONE" ]]; then
  CAND="$ROOT/macos/Packaging/.cache/python"
  if [[ -x "$CAND/bin/python3" ]]; then
    STANDALONE="$CAND"
  fi
fi

if [[ -z "$STANDALONE" || ! -x "$STANDALONE/bin/python3" ]]; then
  echo "No standalone Python at macos/Packaging/.cache/python" >&2
  echo "Download python-build-standalone (arm64) into that path, or set TIGEROSE_PYTHON_STANDALONE (or AVENT_PYTHON_STANDALONE)." >&2
  echo "Dev tip: App can run without bundle using system python3 + repo PYTHONPATH." >&2
  exit 1
fi

rm -rf "$RUNTIME/python" "$RUNTIME/server" "$RUNTIME/site-packages" "$RUNTIME/avent_config.py" "$RUNTIME/avent_paths.py" "$RUNTIME/session_store.py" "$RUNTIME/agents_md.py" "$RUNTIME/AGENTS.md"
mkdir -p "$RUNTIME/python" "$RUNTIME/site-packages"
cp -R "$STANDALONE"/. "$RUNTIME/python/"

# Copy server package + top-level modules needed at import time.
cp -R "$ROOT/server" "$RUNTIME/server"
cp "$ROOT/avent_config.py" "$ROOT/avent_paths.py" "$ROOT/session_store.py" "$ROOT/agents_md.py" "$ROOT/memory_core.py" "$RUNTIME/"
# Project behavior rules — injected into every App assistant turn.
cp "$ROOT/AGENTS.md" "$RUNTIME/AGENTS.md"
# Optional assets used by catalog
[[ -d "$ROOT/skills" ]] && cp -R "$ROOT/skills" "$RUNTIME/skills" || true
[[ -d "$ROOT/plugins" ]] && cp -R "$ROOT/plugins" "$RUNTIME/plugins" || true
[[ -f "$ROOT/config.example.yaml" ]] && cp "$ROOT/config.example.yaml" "$RUNTIME/config.example.yaml"

PY="$RUNTIME/python/bin/python3"
"$PY" -m pip install --upgrade pip --target "$RUNTIME/site-packages"
(cd "$ROOT" && "$PY" -m pip install --target "$RUNTIME/site-packages" -r "$ROOT/requirements.txt")

# Source caches and local dependency trees are not distributable app assets.
find "$RUNTIME" -type d \( -name __pycache__ -o -name .git -o -name node_modules \) -prune -exec rm -rf {} +
find "$RUNTIME" -type f \( -name .DS_Store -o -name '*.pyc' -o -name .env \) -delete

# The optional WeCom channel needs its production SDK; Node.js remains a host prerequisite.
if [[ -f "$RUNTIME/server/im_channels/package-lock.json" ]]; then
  npm ci --omit=dev --ignore-scripts --prefix "$RUNTIME/server/im_channels"
fi

# Re-sign after mutating Contents/Resources (ad-hoc; required for LaunchServices).
codesign --force --deep --sign - "$APP" >/dev/null

echo "==> Done. Runtime at $RUNTIME"
