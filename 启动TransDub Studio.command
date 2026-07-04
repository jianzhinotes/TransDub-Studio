#!/bin/zsh

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
F5_SERVICE="/Users/jinxing/Documents/codex/f5-tts-service/start_service.sh"
LOCK_DIR="/tmp/com.transdub.studio.local.lock"
PID_FILE="$LOCK_DIR/python.pid"
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

cleanup_lock() {
    rm -f "$PID_FILE"
    rmdir "$LOCK_DIR" 2>/dev/null || true
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

    cleanup_lock
    if ! mkdir "$LOCK_DIR" 2>/dev/null; then
        echo "无法创建单实例锁：$LOCK_DIR"
        exit 1
    fi
fi

trap cleanup_lock EXIT INT TERM HUP

if [[ -x "$F5_SERVICE" ]]; then
    echo "正在确认 F5-TTS 原声克隆服务..."
    "$F5_SERVICE"
    if [[ $? -ne 0 ]]; then
        echo "F5-TTS 未能启动，TransDub Studio 仍将打开，但原声克隆暂不可用。"
    fi
fi

export PYVIDEOTRANS_LANG="zh"
"$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/sp.py" --lang zh &
APP_PID=$!
echo "$APP_PID" >"$PID_FILE"
wait "$APP_PID"
STATUS=$?

if [[ $STATUS -ne 0 ]]; then
    echo
    echo "TransDub Studio 启动失败，错误代码：$STATUS"
    echo "请保留此窗口并查看上方错误信息。"
    echo
    read -k 1 "?按任意键关闭窗口..."
fi

exit $STATUS
