#!/bin/zsh

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
F5_STOP="/Users/jinxing/Documents/codex/f5-tts-service/停止F5-TTS.command"
LOCK_DIR="/tmp/com.transdub.studio.local.lock"
PID_FILE="$LOCK_DIR/python.pid"
OWNS_LOCK=0
cd "$PROJECT_DIR"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export LANG="${LANG:-zh_CN.UTF-8}"

activate_existing() {
    local pid="$1"
    /usr/bin/osascript - "$pid" <<'APPLESCRIPT' >/dev/null 2>&1
on run argv
    set targetPID to (item 1 of argv) as integer
    tell application "System Events"
        set frontmost of first process whose unix id is targetPID to true
    end tell
end run
APPLESCRIPT
}

release_lock() {
    [[ "$OWNS_LOCK" -eq 1 ]] || return 0
    if [[ -s "$PID_FILE" ]]; then
        local recorded_pid
        recorded_pid="$(cat "$PID_FILE" 2>/dev/null)"
        [[ "$recorded_pid" == "$$" ]] || return 0
    fi
    rm -f "$PID_FILE"
    rmdir "$LOCK_DIR" 2>/dev/null || true
    OWNS_LOCK=0
}

cleanup_lock() {
    [[ "$OWNS_LOCK" -eq 1 ]] || return 0
    if [[ -x "$F5_STOP" ]]; then
        "$F5_STOP" >/dev/null 2>&1 || true
    fi
    release_lock
}

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    if [[ -s "$PID_FILE" ]]; then
        EXISTING_PID="$(cat "$PID_FILE")"
        if kill -0 "$EXISTING_PID" 2>/dev/null; then
            echo "TransDub Studio 已经在运行，正在切换到现有窗口。"
            activate_existing "$EXISTING_PID"
            exit 0
        fi
    fi
    # Recover a stale lock left by an unclean shutdown.  cleanup_lock only
    # removes locks owned by this process, so stale cleanup is explicit here.
    rm -f "$PID_FILE"
    rmdir "$LOCK_DIR" 2>/dev/null || true
    if ! mkdir "$LOCK_DIR" 2>/dev/null; then
        echo "无法创建单实例锁：$LOCK_DIR"
        exit 1
    fi
fi
OWNS_LOCK=1
echo "$$" >"$PID_FILE"

trap cleanup_lock EXIT

export PYVIDEOTRANS_LANG="zh"
export TRANSDUB_PID_FILE="$PID_FILE"
export TRANSDUB_F5_STOP="$F5_STOP"
exec "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/sp.py" --lang zh
