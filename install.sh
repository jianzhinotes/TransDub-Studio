#!/usr/bin/env bash
#
# TransDub Studio one-command installer (macOS).
#   curl -fsSL https://raw.githubusercontent.com/jianzhinotes/TransDub-Studio/main/install.sh | bash
#
# Installs uv, clones the repo, and syncs dependencies. Optional first arg = install dir.
set -euo pipefail

REPO="https://github.com/jianzhinotes/TransDub-Studio.git"
DEST="${1:-$HOME/TransDub-Studio}"

echo "==> TransDub Studio installer"

# 1) uv (Python toolchain manager)
if ! command -v uv >/dev/null 2>&1; then
  echo "==> Installing uv…"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi
command -v uv >/dev/null 2>&1 || { echo "!! uv not found on PATH. Open a new terminal and re-run."; exit 1; }

# 2) clone or update
if [ -d "$DEST/.git" ]; then
  echo "==> Updating existing install at $DEST"
  git -C "$DEST" pull --ff-only || echo "   (skipped pull; local changes present)"
else
  echo "==> Cloning into $DEST"
  git clone "$REPO" "$DEST"
fi

# 3) dependencies (torch etc. — a few GB)
cd "$DEST"
echo "==> Installing dependencies (downloads several GB, please be patient)…"
uv sync

echo ""
echo "✅ Installation complete."
echo ""
echo "   Launch it with:"
echo "       cd \"$DEST\" && uv run python sp.py"
echo ""
echo "   (First launch downloads the recognition model on demand; after that it runs fully local.)"
