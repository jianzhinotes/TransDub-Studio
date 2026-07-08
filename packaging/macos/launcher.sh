#!/bin/zsh
# TransDub Studio .app executable.
# If the runtime is already set up, launch straight away (no terminal window).
# On first run, open Terminal to run first_run_setup.sh (extract + uv sync + launch)
# so the user can watch the multi-GB download.
set -e

APP_RES="$(cd "$(dirname "$0")/../Resources" && pwd)"
ROOT="$HOME/Library/Application Support/TransDub Studio"
RUNTIME="$ROOT/runtime"
UV="$HOME/.local/bin/uv"
mkdir -p "$ROOT"

if [[ -x "$RUNTIME/.venv/bin/python" ]]; then
    [[ -x "$UV" ]] || UV="$(command -v uv || echo uv)"
    cd "$RUNTIME"
    exec "$UV" run python sp.py
fi

# First run: bootstrap in a visible Terminal, then launch from there.
/usr/bin/osascript <<APPLESCRIPT
tell application "Terminal"
    activate
    do script "/bin/bash '$APP_RES/first_run_setup.sh' '$APP_RES' '$RUNTIME'"
end tell
APPLESCRIPT
