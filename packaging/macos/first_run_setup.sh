#!/bin/bash
# Setup for the macOS .app: unpacks the bundled source into the Application
# Support runtime, ensures uv + ffmpeg, runs `uv sync`, stamps the version,
# then launches.
#
# Handles both a first install and an upgrade over an existing runtime.
# Unpacking never touches user data: the payload is `git archive` output, and
# cfg.json / params.json / .secret_salt / recent_tasks.json are untracked, so
# they are simply not in it.
set -e
APP_RES="$1"
RUNTIME="$2"
MODE="${3:-install}"
APP_VER="${4:-}"

echo "========================================================"
if [ "$MODE" = "upgrade" ]; then
    OLD_VER="$(cat "$RUNTIME/.payload-version" 2>/dev/null || echo 'older version')"
    echo "  TransDub Studio - updating ${OLD_VER} -> ${APP_VER:-new version}"
    echo "  Your settings and history are kept."
else
    echo "  TransDub Studio - first-time setup"
    echo "  This downloads a few GB (PyTorch + models). Please wait."
fi
echo "========================================================"

mkdir -p "$RUNTIME"
echo "==> Unpacking application files..."
# Clear the stamp first: if anything below fails, the next launch retries the
# upgrade instead of running half-updated code.
rm -f "$RUNTIME/.payload-version"
tar -xzf "$APP_RES/payload.tar.gz" -C "$RUNTIME"

# uv
if ! command -v uv >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/uv" ]; then
    echo "==> Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
UV="$HOME/.local/bin/uv"
[ -x "$UV" ] || UV="$(command -v uv || echo uv)"

# ffmpeg (best effort via Homebrew; otherwise relies on PATH)
if ! command -v ffmpeg >/dev/null 2>&1; then
    if command -v brew >/dev/null 2>&1; then
        echo "==> Installing ffmpeg via Homebrew..."
        brew install ffmpeg || echo "   (ffmpeg install failed; install it manually if dubbing errors out)"
    else
        echo "   NOTE: ffmpeg not found and Homebrew is unavailable."
        echo "         Install ffmpeg (e.g. 'brew install ffmpeg') if processing errors out."
    fi
fi

cd "$RUNTIME"
if [ "$MODE" = "upgrade" ]; then
    echo "==> Checking dependencies..."
else
    echo "==> Installing dependencies (this is the long part)..."
fi
"$UV" sync

# Only stamp once the runtime is fully usable.
[ -n "$APP_VER" ] && printf '%s' "$APP_VER" > "$RUNTIME/.payload-version"

echo "==> Launching TransDub Studio..."
exec "$RUNTIME/.venv/bin/python" sp.py
