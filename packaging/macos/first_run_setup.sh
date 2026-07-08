#!/bin/bash
# First-run setup for the macOS .app. Extracts the bundled source into the
# Application Support runtime, installs uv + ffmpeg, runs `uv sync`, then launches.
set -e
APP_RES="$1"
RUNTIME="$2"

echo "========================================================"
echo "  TransDub Studio - first-time setup"
echo "  This downloads a few GB (PyTorch + models). Please wait."
echo "========================================================"

mkdir -p "$RUNTIME"
echo "==> Unpacking application files..."
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
echo "==> Installing dependencies (this is the long part)..."
"$UV" sync

echo "==> Launching TransDub Studio..."
exec "$UV" run python sp.py
