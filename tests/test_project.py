import dataclasses
import json
from pathlib import Path

from videotrans.task.project import (
    PROJECT_EXT, load_project, project_dir_for, project_paths, save_project, save_queue,
)
from videotrans.dub.schema import PROJECT_SCHEMA_VERSION, TextCandidate
from videotrans.dub.store import DubProjectStore


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
        assert project['schema_version'] == PROJECT_SCHEMA_VERSION
        assert project['project_id']
        assert project['state_file'] == 'dub_project.json'
        assert DubProjectStore(proj).exists()
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

    def test_load_migrates_v1_project_in_place(self, tmp_path):
        proj = tmp_path / 'legacy.tdproj'
        (proj / 'dubb').mkdir(parents=True)
        _touch(proj / 'dubb' / 'old.wav')
        legacy_manifest = {
            'cfg': {'noextname': 'legacy', 'name': '/src/legacy.mp4',
                    'source_language_code': 'en', 'target_language_code': 'zh-cn'},
            'source_video': '/src/legacy.mp4',
            'target_language_code': 'zh-cn',
            'created': 123,
        }
        legacy_queue = [{
            'line': 1, 'text': '旧工程', 'ref_text': 'Legacy project.',
            'start_time': 0, 'end_time': 1000, 'filename': 'dubb/old.wav',
        }]
        (proj / 'project.json').write_text(json.dumps(legacy_manifest), encoding='utf-8')
        (proj / 'queue_tts.json').write_text(json.dumps(legacy_queue), encoding='utf-8')

        project, queue = load_project(str(proj))

        assert project['schema_version'] == PROJECT_SCHEMA_VERSION
        assert project['created'] == 123
        assert queue[0]['dub_unit_id']
        assert Path(queue[0]['filename']).is_file()
        persisted = json.loads((proj / 'project.json').read_text(encoding='utf-8'))
        assert persisted['schema_version'] == PROJECT_SCHEMA_VERSION
        assert DubProjectStore(proj).load().units[0].source_text == 'Legacy project.'

    def test_save_queue_keeps_v2_candidate_history(self, tmp_path):
        cache = tmp_path / 'cache'; cache.mkdir()
        out = tmp_path / 'out'; out.mkdir()
        _touch(cache / 'novoice.mp4')
        _touch(cache / 'source.wav')
        _touch(cache / 'dubb-0.wav')
        queue = [{
            'line': 1, 'start_time': 0, 'end_time': 1000,
            'text': '初始文本', 'ref_text': 'Initial text.',
            'filename': str(cache / 'dubb-0.wav'),
        }]
        proj = save_project(_make_cfg(str(out), str(cache)), queue, str(cache))
        store = DubProjectStore(proj)
        state = store.load()
        state.units[0].text_candidates.append(
            TextCandidate(id='planner-history', text='规划候选', kind='compact'))
        store.save(state)

        _, editable = load_project(proj)
        editable[0]['text'] = '人工修改'
        save_queue(proj, editable)

        synced = store.load()
        assert 'planner-history' in {c.id for c in synced.units[0].text_candidates}
