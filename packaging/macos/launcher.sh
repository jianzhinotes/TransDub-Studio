#!/bin/zsh
# TransDub Studio .app executable.
#
# The .app is a thin shell: the code that actually runs lives in
# ~/Library/Application Support/TransDub Studio/runtime, unpacked from the
# payload inside this bundle. So a launch has three possible outcomes:
#
#   1. runtime is present and matches this bundle's version -> launch directly
#      (no terminal window, ~1s)
#   2. runtime is present but was unpacked by an OLDER bundle -> upgrade it
#      (re-unpack + uv sync) before launching
#   3. no runtime at all -> first-run bootstrap (multi-GB download)
#
# Case 2 is the one that used to be missing: the old check was just
# "does .venv exist?", so installing a new .dmg over an existing install
# silently kept running the previous version's code forever.
set -e

APP_RES="$(cd "$(dirname "$0")/../Resources" && pwd)"
APP_PLIST="$(cd "$(dirname "$0")/.." && pwd)/Info.plist"
ROOT="$HOME/Library/Application Support/TransDub Studio"
RUNTIME="$ROOT/runtime"
mkdir -p "$ROOT"

# Version of the code inside this bundle vs. the code already unpacked.
# PlistBuddy (unlike `defaults read`) never serves a cached value.
APP_VER="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$APP_PLIST" 2>/dev/null || true)"
# On a missing or malformed plist PlistBuddy prints chatter ("File Doesn't
# Exist, Will Create: ...") to stdout and still exits 0. Taking that as the
# version would mismatch the stamp on every launch and re-run setup forever,
# so only accept something that actually looks like a version number.
[[ "$APP_VER" =~ ^v?[0-9]+(\.[0-9]+)*$ ]] || APP_VER=""
RUNTIME_VER="$(cat "$RUNTIME/.payload-version" 2>/dev/null || true)"

if [[ -x "$RUNTIME/.venv/bin/python" ]]; then
    # If we cannot read our own version, never block the user on an upgrade we
    # can't reason about -- just launch what is already installed.
    if [[ -z "$APP_VER" || "$APP_VER" == "$RUNTIME_VER" ]]; then
        cd "$RUNTIME"
        exec "$RUNTIME/.venv/bin/python" sp.py
    fi
    MODE="upgrade"
else
    MODE="install"
fi

# Both bootstrap and upgrade run in a visible Terminal: the first downloads a
# few GB, and the second may still need `uv sync` if dependencies changed.
/usr/bin/osascript <<APPLESCRIPT
tell application "Terminal"
    activate
    do script "/bin/bash '$APP_RES/first_run_setup.sh' '$APP_RES' '$RUNTIME' '$MODE' '$APP_VER'"
end tell
APPLESCRIPT
