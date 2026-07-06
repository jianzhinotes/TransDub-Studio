import dataclasses
import json
from pathlib import Path

from videotrans.task.project import (
    PROJECT_EXT, load_project, project_dir_for, project_paths, save_project,
)


@dataclasses.dataclass
class _Cfg:
    """最小可用的 cfg 替身（save_project 用 dataclasses.asdict，故用真 dataclass）。"""
    target_dir: str = ''
    noextname: str = 'demo'
    name: str = '/src/demo.mp4'
    targetdir_mp4: str = ''
    target_language_code: str = 'zh-cn'
    voice_autorate: bool = True
    subtitle_type: int = 1
    novoice_mp4: str = ''
    source_wav: str = ''


def _make_cfg(target_dir, cache):
    return _Cfg(target_dir=target_dir, targetdir_mp4=f'{target_dir}/demo.mp4',
                novoice_mp4=f'{cache}/novoice.mp4', source_wav=f'{cache}/source.wav')


def _touch(p, content=b'x'):
    Path(p).write_bytes(content)


class TestSaveLoadProject:
    def test_round_trip(self, tmp_path):
        cache = tmp_path / 'cache'
        cache.mkdir()
        out = tmp_path / 'out'
        out.mkdir()
        # 造 cache 内容
        _touch(cache / 'novoice.mp4')
        _touch(cache / 'source.wav')
        _touch(cache / 'dubb-0.wav')
        _touch(cache / 'dubb-1.wav')
        queue = [
            {'line': 1, 'start_time': 0, 'end_time': 1000, 'text': 'hi',
             'filename': str(cache / 'dubb-0.wav')},
            {'line': 2, 'start_time': 1000, 'end_time': 2000, 'text': 'yo',
             'filename': str(cache / 'dubb-1.wav')},
        ]
        cfg = _make_cfg(str(out), str(cache))

        proj = save_project(cfg, queue, str(cache))
        assert proj.endswith('demo' + PROJECT_EXT)
        assert Path(proj, 'project.json').exists()
        assert Path(proj, 'novoice.mp4').exists()
        assert Path(proj, 'source.wav').exists()
        assert Path(proj, 'dubb', 'dubb-0.wav').exists()

        # queue_tts.json 里 filename 已相对化
        saved_q = json.loads(Path(proj, 'queue_tts.json').read_text())
        assert saved_q[0]['filename'] == 'dubb/dubb-0.wav'

        project, loaded_q = load_project(proj)
        assert project['cfg']['target_language_code'] == 'zh-cn'
        assert project['cfg']['voice_autorate'] is True
        # 还原为绝对路径且文件存在
        assert Path(loaded_q[0]['filename']).exists()
        assert loaded_q[1]['text'] == 'yo'

    def test_missing_dub_cleared(self, tmp_path):
        cache = tmp_path / 'cache'; cache.mkdir()
        out = tmp_path / 'out'; out.mkdir()
        queue = [{'line': 1, 'start_time': 0, 'end_time': 1000, 'text': 'x',
                  'filename': str(cache / 'nonexistent.wav')}]
        proj = save_project(_make_cfg(str(out), str(cache)), queue, str(cache))
        _, loaded = load_project(proj)
        assert loaded[0]['filename'] == ''   # 缺文件清空

    def test_project_paths(self, tmp_path):
        p = project_paths(str(tmp_path / 'x.tdproj'))
        assert p['cache_folder'].endswith('x.tdproj')
        assert p['novoice_mp4'].endswith('novoice.mp4')

    def test_dir_for(self):
        assert project_dir_for('/out', 'movie') == '/out/movie.tdproj'
