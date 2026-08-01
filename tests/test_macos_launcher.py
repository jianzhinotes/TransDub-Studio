import os
from pathlib import Path


def test_source_launcher_execs_python_without_supervisor_shell():
    root = Path(__file__).resolve().parents[1]
    launcher = (root / "启动TransDub Studio.command").read_text(encoding="utf-8")

    assert 'exec "$PROJECT_DIR/.venv/bin/python"' in launcher
    assert '"$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/sp.py" --lang zh &' not in launcher
    assert 'export TRANSDUB_PID_FILE="$PID_FILE"' in launcher
    assert 'rm -f "$PID_FILE"' in launcher


def test_app_launcher_uses_one_runtime_pointer_and_matching_f5_service():
    root = Path(__file__).resolve().parents[1]
    launcher = (
        root / "TransDub Studio.app.noindex/Contents/MacOS/TransDubStudio"
    ).read_text(encoding="utf-8")

    assert 'DEV_ROOT_FILE="$HOME/Library/Application Support/TransDub Studio/dev-root"' in launcher
    assert 'IFS= read -r DEV_APP_DIR < "$DEV_ROOT_FILE"' in launcher
    assert 'F5_ROOT="$(cd "$APP_DIR/.." && pwd)/f5-tts-service"' in launcher
    assert 'export TRANSDUB_RUNTIME_ROOT="$APP_DIR"' in launcher
    assert 'exec "$PYTHON" "$MAIN" --lang zh' in launcher
    assert 'rm -f "$PID_FILE"' in launcher


def test_python_cleanup_releases_only_its_launcher_lock(tmp_path, monkeypatch):
    import sp

    lock_dir = tmp_path / "transdub.lock"
    lock_dir.mkdir()
    pid_file = lock_dir / "python.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    monkeypatch.setenv("TRANSDUB_PID_FILE", str(pid_file))
    monkeypatch.delenv("TRANSDUB_F5_STOP", raising=False)
    sp._launcher_cleanup_done = False

    try:
        sp._cleanup_launcher_runtime()
        assert not pid_file.exists()
        assert not lock_dir.exists()
    finally:
        sp._launcher_cleanup_done = False


def test_python_cleanup_preserves_a_newer_launcher_lock(tmp_path, monkeypatch):
    import sp

    lock_dir = tmp_path / "transdub.lock"
    lock_dir.mkdir()
    pid_file = lock_dir / "python.pid"
    pid_file.write_text(str(os.getpid() + 1), encoding="utf-8")
    monkeypatch.setenv("TRANSDUB_PID_FILE", str(pid_file))
    monkeypatch.delenv("TRANSDUB_F5_STOP", raising=False)
    sp._launcher_cleanup_done = False

    try:
        sp._cleanup_launcher_runtime()
        assert pid_file.exists()
    finally:
        sp._launcher_cleanup_done = False
