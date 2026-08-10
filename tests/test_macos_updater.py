"""macOS .app 升级路径的回归测试。

背景：v1.1.2 之前 launcher.sh 只判断 `.venv` 是否存在，于是把新版 dmg 装到
已有安装之上时，运行时代码永远不会被替换 —— 用户装了新版，跑的还是旧代码，
而且没有任何报错。这类 bug 在纯 Python 测试里完全不可见，所以在这里直接驱动
真实的 shell 脚本。

launcher 最后一步是用 osascript 弹 Terminal。测试里跑一份把那段替换成 echo
的副本（`_make_probe`），除此之外逻辑与线上脚本逐行相同；分支判断本身没有被
改写。first_run_setup.sh 则是原样端到端执行，只用桩替换 uv / python。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != 'darwin', reason='macOS packaging scripts'
)

PKG = Path(__file__).resolve().parent.parent / 'packaging' / 'macos'
LAUNCHER = PKG / 'launcher.sh'
SETUP = PKG / 'first_run_setup.sh'

PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleShortVersionString</key><string>{ver}</string>
</dict>
</plist>
"""

# 会被真实解包覆盖的代码，以及必须原样幸存的用户数据。
USER_FILES = {
    'params.json': '{"deepseek_key":"SECRET-MUST-SURVIVE"}',
    'cfg.json': '{"lang":"zh"}',
    '.secret_salt': 'saltysalt',
    'recent_tasks.json': '[{"name":"old task"}]',
}


def _write(path: Path, text: str, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if executable:
        path.chmod(0o755)


@pytest.fixture()
def bundle(tmp_path: Path):
    """造一个 build_dmg.sh 产物形状的 .app，payload 里放一个老 runtime 没有的新模块。"""
    app = tmp_path / 'App' / 'TransDub Studio.app'
    res = app / 'Contents' / 'Resources'
    _write(app / 'Contents' / 'MacOS' / 'TransDubStudio', LAUNCHER.read_text(), executable=True)
    _write(res / 'first_run_setup.sh', SETUP.read_text(), executable=True)
    _write(app / 'Contents' / 'Info.plist', PLIST.format(ver='1.2.0'))

    payload_src = tmp_path / 'payload'
    _write(payload_src / 'sp.py', 'print("app running")\n')
    _write(payload_src / 'videotrans' / '__init__.py', 'VERSION_NUM = 10200\n')
    _write(payload_src / 'videotrans' / 'flowui' / 'recent_card.py', 'class RecentCard: pass\n')
    with tarfile.open(res / 'payload.tar.gz', 'w:gz') as tar:
        tar.add(payload_src, arcname='.')
    return app


@pytest.fixture()
def home(tmp_path: Path):
    """一台「装了旧版本、带用户数据」的机器。"""
    h = tmp_path / 'home'
    runtime = h / 'Library' / 'Application Support' / 'TransDub Studio' / 'runtime'
    _write(runtime / 'videotrans' / '__init__.py', 'VERSION_NUM = 10101\n')
    for name, body in USER_FILES.items():
        _write(runtime / 'videotrans' / name, body)
    # uv 与 python 的桩：真跑会下载几个 G。
    _write(runtime / '.venv' / 'bin' / 'python', '#!/bin/sh\necho "[python] $*"\n', executable=True)
    _write(h / '.local' / 'bin' / 'uv', '#!/bin/sh\necho "[uv] $*"\n', executable=True)
    return h


def _runtime(home: Path) -> Path:
    return home / 'Library' / 'Application Support' / 'TransDub Studio' / 'runtime'


def _make_probe(app: Path) -> Path:
    """launcher.sh 的副本，末尾的 osascript 弹窗换成一行可观测输出。"""
    src = (app / 'Contents' / 'MacOS' / 'TransDubStudio').read_text()
    head, sep, _ = src.partition('/usr/bin/osascript')
    assert sep, 'launcher.sh 不再通过 osascript 启动 setup，请更新此测试'
    probe = app / 'Contents' / 'MacOS' / 'probe'
    _write(probe, head + 'echo "SETUP mode=$MODE ver=$APP_VER"\n', executable=True)
    return probe


def _launch(app: Path, home: Path) -> str:
    env = dict(os.environ, HOME=str(home))
    # 让 ffmpeg 探测走空，避免测试机上触发 brew install。
    stub = app.parent / 'stubbin'
    _write(stub / 'ffmpeg', '#!/bin/sh\nexit 0\n', executable=True)
    env['PATH'] = f'{stub}:{env["PATH"]}'
    out = subprocess.run(
        ['/bin/zsh', str(_make_probe(app))],
        capture_output=True, text=True, env=env, timeout=60,
    )
    return (out.stdout + out.stderr).strip().splitlines()[-1]


def _stamp(home: Path, value: str | None) -> None:
    f = _runtime(home) / '.payload-version'
    if value is None:
        f.unlink(missing_ok=True)
    else:
        f.write_text(value)


# --- 分支判断 -------------------------------------------------------------

def test_matching_stamp_launches_directly(bundle, home):
    """版本一致时不得弹 Terminal —— 日常启动必须是 1 秒直达。"""
    _stamp(home, '1.2.0')
    assert '[python] sp.py' in _launch(bundle, home)


def test_stale_stamp_triggers_upgrade(bundle, home):
    """装了新版 dmg 就必须刷新运行时代码，这正是原来缺失的分支。"""
    _stamp(home, '1.1.2')
    assert 'mode=upgrade' in _launch(bundle, home)


def test_missing_stamp_triggers_upgrade(bundle, home):
    """1.1.2 及更早版本装出来的运行时没有版本戳，也要能自愈。"""
    _stamp(home, None)
    assert 'mode=upgrade' in _launch(bundle, home)


def test_no_venv_triggers_first_install(bundle, home):
    import shutil
    shutil.rmtree(_runtime(home) / '.venv')
    assert 'mode=install' in _launch(bundle, home)


def test_stamp_with_trailing_newline_still_matches(bundle, home):
    """手写或被编辑器加过换行的版本戳不该触发无谓升级。"""
    _stamp(home, '1.2.0\n')
    assert '[python] sp.py' in _launch(bundle, home)


@pytest.mark.parametrize('plist', [None, 'not a plist at all'])
def test_unreadable_plist_never_blocks_launch(bundle, home, plist):
    """plist 缺失或损坏时 PlistBuddy 会把提示文字打到 stdout 并返回 0。

    若把那串文字当成版本号，它永远匹配不上版本戳 —— 用户每次启动都会被弹一次
    Terminal 重装，无限循环。这里必须回落到直接启动。
    """
    _stamp(home, '1.2.0')
    p = bundle / 'Contents' / 'Info.plist'
    if plist is None:
        p.unlink()
    else:
        p.write_text(plist)
    assert '[python] sp.py' in _launch(bundle, home)


# --- 真实升级 -------------------------------------------------------------

def test_upgrade_replaces_code_and_keeps_user_data(bundle, home):
    """端到端跑 first_run_setup.sh：代码换新、新模块到位、用户数据一个不少。"""
    runtime = _runtime(home)
    env = dict(os.environ, HOME=str(home))
    subprocess.run(
        ['/bin/bash', str(bundle / 'Contents' / 'Resources' / 'first_run_setup.sh'),
         str(bundle / 'Contents' / 'Resources'), str(runtime), 'upgrade', '1.2.0'],
        check=True, capture_output=True, text=True, env=env, timeout=120,
    )

    assert 'VERSION_NUM = 10200' in (runtime / 'videotrans' / '__init__.py').read_text()
    # 老运行时没有的模块 —— 正是 ModuleNotFoundError 崩溃的那一类。
    assert (runtime / 'videotrans' / 'flowui' / 'recent_card.py').exists()
    assert (runtime / '.payload-version').read_text() == '1.2.0'
    for name, body in USER_FILES.items():
        assert (runtime / 'videotrans' / name).read_text() == body, f'{name} 被覆盖了'


def test_stamp_written_only_after_uv_sync_succeeds(bundle, home):
    """uv sync 失败时不得留下版本戳，否则下次启动会直接跑半更新的代码。"""
    runtime = _runtime(home)
    _stamp(home, '1.1.2')
    _write(home / '.local' / 'bin' / 'uv', '#!/bin/sh\nexit 1\n', executable=True)

    out = subprocess.run(
        ['/bin/bash', str(bundle / 'Contents' / 'Resources' / 'first_run_setup.sh'),
         str(bundle / 'Contents' / 'Resources'), str(runtime), 'upgrade', '1.2.0'],
        capture_output=True, text=True, env=dict(os.environ, HOME=str(home)), timeout=120,
    )
    assert out.returncode != 0
    assert not (runtime / '.payload-version').exists()


# --- 脚本自身 -------------------------------------------------------------

@pytest.mark.parametrize('shell,script', [('/bin/zsh', LAUNCHER), ('/bin/bash', SETUP)])
def test_scripts_parse(shell, script):
    assert subprocess.run([shell, '-n', str(script)], capture_output=True).returncode == 0


def test_build_dmg_stamps_a_version_the_launcher_can_read():
    """launcher 用 CFBundleShortVersionString 做判断，build_dmg.sh 必须写它，
    且写的值要能通过 launcher 里的版本号格式校验。"""
    build = (PKG / 'build_dmg.sh').read_text()
    assert 'CFBundleShortVersionString' in build
    assert re.search(r'VER="\$\{1:-([0-9.]+)\}"', build), 'build_dmg.sh 的版本参数形式变了'
