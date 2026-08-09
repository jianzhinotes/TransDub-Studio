"""配音进度埋点：所有 TTS 后端的公共收敛点逐段落盘，UI 据此算真 ETA。"""
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

import videotrans.tts._base as tts_base
from videotrans.dub.performance_report import TTS_PROGRESS_FILE
from videotrans.tts._base import BaseTTS


@pytest.fixture(autouse=True)
def isolated_dub_cache(tmp_path, monkeypatch):
    """隔离跨运行配音缓存：否则同名文本会被上一个用例的缓存命中，
    prefilled 计数被污染。"""
    monkeypatch.setattr(tts_base, 'DUBB_CACHE_DIR', str(tmp_path / 'dubb_cache'))
    monkeypatch.setattr(tts_base, '_dubb_cache_pruned', True)


@dataclass
class StubTTS(BaseTTS):
    def _run(self, data_item, idx=-1):
        Path(data_item['filename']).write_bytes(b'RIFFfake')
        return None


def _queue(tmp_path, texts, prefilled=0):
    out = tmp_path / 'run'
    out.mkdir(parents=True, exist_ok=True)
    queue = []
    for i, text in enumerate(texts):
        name = out / f'{i}.wav'
        if i < prefilled:                 # 模拟缓存命中：文件已存在
            name.write_bytes(b'RIFFcached')
        queue.append({'text': text, 'role': 'edge-A', 'filename': str(name),
                      'rate': '+0%', 'volume': '+0%', 'pitch': '+0Hz'})
    return queue


def _payload(tmp_path):
    return json.loads((tmp_path / 'run' / TTS_PROGRESS_FILE).read_text(encoding='utf-8'))


class TestSingleThreadProgress:
    def test_publishes_total_and_monotonic_completed(self, tmp_path, monkeypatch):
        written = []
        original = BaseTTS._publish_tts_progress

        def spy(self, completed, prefilled, started, *, status='running'):
            written.append((completed, status))
            original(self, completed, prefilled, started, status=status)

        monkeypatch.setattr(BaseTTS, '_publish_tts_progress', spy)
        t = StubTTS(queue_tts=_queue(tmp_path, ['一', '二', '三']),
                    language='zh-cn', tts_type=0, dub_nums=1)
        t.run()

        data = _payload(tmp_path)
        assert data['total'] == 3
        assert data['completed'] == 3
        assert data['status'] == 'finished'
        counts = [c for c, _ in written]
        assert counts == sorted(counts)          # 单调不减
        assert written[-1][1] == 'finished'

    def test_prefilled_excludes_cached_rows(self, tmp_path):
        # 前两条已有文件（缓存命中），只有第三条真正合成
        t = StubTTS(queue_tts=_queue(tmp_path, ['一', '二', '三'], prefilled=2),
                    language='zh-cn', tts_type=0, dub_nums=1)
        t.run()
        data = _payload(tmp_path)
        assert data['prefilled'] == 2
        assert data['completed'] == 3
        assert data['elapsed_s'] >= 0


class TestThreadPoolProgress:
    def test_pool_branch_also_publishes(self, tmp_path):
        t = StubTTS(queue_tts=_queue(tmp_path, [f'第{i}句' for i in range(6)]),
                    language='zh-cn', tts_type=0, dub_nums=3)
        t.run()
        data = _payload(tmp_path)
        assert data['total'] == 6
        assert data['completed'] == 6
        assert data['status'] == 'finished'


class TestFailureIsolation:
    def test_write_failure_never_breaks_dubbing(self, tmp_path, monkeypatch):
        def boom(*a, **k):
            raise OSError('disk full')

        monkeypatch.setattr('videotrans.dub.store.atomic_write_json', boom)
        t = StubTTS(queue_tts=_queue(tmp_path, ['一', '二']),
                    language='zh-cn', tts_type=0, dub_nums=1)
        t.run()                                   # 不应抛出
        for item in t.queue_tts:
            assert Path(item['filename']).stat().st_size > 0

    def test_no_filename_is_tolerated(self, tmp_path):
        t = StubTTS(queue_tts=[{'text': '无文件名', 'role': 'x', 'filename': ''}],
                    language='zh-cn', tts_type=0, dub_nums=1)
        assert t._tts_progress_path() is None
