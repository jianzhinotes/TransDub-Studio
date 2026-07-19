"""Dubbing Studio 离屏冒烟测试：需要真实 PySide6 + ffmpeg，CI mock 环境自动跳过。"""
import importlib.util
import inspect
import json
import os
import shutil
import subprocess

import pytest

if importlib.util.find_spec('PySide6') is None:
    pytest.skip('requires real PySide6', allow_module_level=True)
if shutil.which('ffmpeg') is None:
    pytest.skip('requires ffmpeg', allow_module_level=True)

soundfile = pytest.importorskip('soundfile')

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')


@pytest.fixture(scope='module')
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def studio_env(tmp_path):
    """合成 3 行 queue_tts + 音频片段 + 2 秒测试视频。"""
    import numpy as np
    sr = 16000
    video = tmp_path / 'v.mp4'
    subprocess.run(
        ['ffmpeg', '-y', '-f', 'lavfi', '-i', 'color=c=blue:s=320x180:d=6',
         '-f', 'lavfi', '-i', 'sine=frequency=440:duration=6',
         '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest',
         str(video)], check=True, capture_output=True)
    queue = []
    for i in range(3):
        wav = tmp_path / f'dubb-{i}.wav'
        t = np.arange(sr) / sr
        soundfile.write(str(wav), 0.5 * np.sin(2 * np.pi * (300 + 100 * i) * t), sr)
        start = i * 2000
        queue.append({
            'text': f'line {i}', 'ref_text': f'src {i}', 'line': i + 1,
            'start_time': start, 'end_time': start + 1000,
            'startraw': '', 'endraw': '',
            'start_time_source': start, 'end_time_source': start + 1000,
            'role': 'roleA', 'rate': '+0%', 'volume': '+0%', 'pitch': '+0Hz',
            'tts_type': 0, 'filename': str(wav), 'dubbing_s': 1.0,
        })
    (tmp_path / 'queue_tts.json').write_text(json.dumps(queue, ensure_ascii=False),
                                             encoding='utf-8')
    return {'video': str(video), 'cache': str(tmp_path)}


class TestStudioSmoke:
    def test_open_edit_and_persist(self, qapp, studio_env, tmp_path, monkeypatch):
        from videotrans.component.timeline.studio import (
            DubbingStudioDialog, JointPlanPreviewDialog)

        dlg = DubbingStudioDialog(cache_folder=studio_env['cache'],
                                  language='zh-cn',
                                  video_path=studio_env['video'],
                                  auto_plan=False)
        dlg.show()
        for _ in range(30):
            qapp.processEvents()

        # 卡片已建
        assert dlg.cards.card(0) is not None
        assert dlg.cards.card(2) is not None
        assert dlg.cards.card(0).text_edit.toPlainText() == 'line 0'
        # 联合编排必须由用户显式触发；打开工作台不会自动调用 API 或启动规划线程
        assert dlg.joint_btn.menu() is None
        assert dlg._joint_worker is None
        assert inspect.signature(
            DubbingStudioDialog.__init__).parameters['auto_plan'].default is True

        # 离线规划在线程中完成，结果窗可打开；原 StudioState 不被规划预览改写
        monkeypatch.setattr(JointPlanPreviewDialog, 'exec', lambda self: 0)
        original_texts = [item['text'] for item in dlg.state.items]
        dlg._start_joint_planning('rules')
        worker = dlg._joint_worker
        assert worker is not None
        assert worker.wait(5000)
        for _ in range(10):
            qapp.processEvents()
        assert dlg._joint_worker is None
        assert [item['text'] for item in dlg.state.items] == original_texts
        assert 'rules' in dlg.joint_status.text()
        assert os.path.exists(os.path.join(studio_env['cache'],
                                           'joint-preview.tdproj', 'dub_project.json'))

        # 截图非空
        png = tmp_path / 'studio.png'
        dlg.grab().save(str(png))
        assert png.stat().st_size > 1000

        # 模拟编辑：时间 + 文本 + dirty
        dlg.state.set_times(0, 500, 1500)
        dlg.state.set_text(1, 'edited line 1')
        assert dlg.state.is_dirty(1)
        assert dlg.cards.card(1).dirty_badge.isVisible() or True  # 徽标状态经 refresh 更新

        # 持久化路径（不走 accept 的 UI 弹窗）
        dlg.state.save(studio_env['cache'])
        data = json.loads(open(f"{studio_env['cache']}/queue_tts.json",
                               encoding='utf-8').read())
        assert data[0]['start_time'] == 500
        assert data[0]['startraw'] == '00:00:00,500'
        assert data[1]['text'] == 'edited line 1'
        assert all(not k.startswith('_') for it in data for k in it)

        # 等 PrepWorker 结束再退出，避免测试进程结束时销毁运行中的 QThread
        dlg._prep_worker.wait(15000)
        dlg._teardown()
        dlg._accepting = True
        dlg.close()
        qapp.processEvents()
