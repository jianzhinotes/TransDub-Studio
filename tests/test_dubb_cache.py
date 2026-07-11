from dataclasses import dataclass
from pathlib import Path

import pytest

import videotrans.tts._base as tts_base
from videotrans.tts._base import BaseTTS


@dataclass
class FakeTTS(BaseTTS):
    """最小可跑的 TTS 渠道：_run 写一个非空文件并计数。"""
    calls: int = 0

    def _run(self, data_item, idx=-1):
        type(self).calls += 1
        Path(data_item['filename']).write_bytes(b'RIFFfake-audio-bytes')
        return None


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    d = tmp_path / 'dubb_cache'
    monkeypatch.setattr(tts_base, 'DUBB_CACHE_DIR', str(d))
    monkeypatch.setattr(tts_base, '_dubb_cache_pruned', True)
    return d


def _queue(tmp_path, run_tag, texts):
    out = tmp_path / f'run-{run_tag}'
    out.mkdir(parents=True, exist_ok=True)
    return [
        {'text': t, 'role': 'edge-A', 'filename': str(out / f'{i}.wav'),
         'rate': '+0%', 'volume': '+0%', 'pitch': '+0Hz'}
        for i, t in enumerate(texts)
    ]


class TestDubbCache:
    def test_second_run_hits_cache(self, tmp_path, cache_dir):
        FakeTTS.calls = 0
        texts = ['你好世界', '第二句话']
        t1 = FakeTTS(queue_tts=_queue(tmp_path, 1, texts), language='zh-cn', tts_type=0)
        t1.run()
        assert FakeTTS.calls == 2
        assert len(list(cache_dir.iterdir())) == 2

        # 新的一次运行：新 filename（模拟新 uuid 目录），同文本同参数 → 全部命中，不再合成
        t2 = FakeTTS(queue_tts=_queue(tmp_path, 2, texts), language='zh-cn', tts_type=0)
        t2.run()
        assert FakeTTS.calls == 2  # 没有新调用
        for it in t2.queue_tts:
            assert Path(it['filename']).stat().st_size > 0

    def test_use_cache_false_regenerates(self, tmp_path, cache_dir):
        FakeTTS.calls = 0
        texts = ['重新处理的句子']
        FakeTTS(queue_tts=_queue(tmp_path, 1, texts), language='zh-cn', tts_type=0).run()
        assert FakeTTS.calls == 1
        t2 = FakeTTS(queue_tts=_queue(tmp_path, 2, texts), language='zh-cn',
                     tts_type=0, use_cache=False)
        t2.run()
        assert FakeTTS.calls == 2  # 不恢复，重新合成

    def test_text_change_misses(self, tmp_path, cache_dir):
        FakeTTS.calls = 0
        FakeTTS(queue_tts=_queue(tmp_path, 1, ['原句']), language='zh-cn', tts_type=0).run()
        FakeTTS(queue_tts=_queue(tmp_path, 2, ['改过的句子']), language='zh-cn', tts_type=0).run()
        assert FakeTTS.calls == 2

    def test_leak_marked_not_stored(self, tmp_path, cache_dir):
        FakeTTS.calls = 0

        class LeakTTS(FakeTTS):
            def _exec(self):
                super()._exec()
                for it in self.queue_tts:
                    it['lang_leak'] = 'Je vous laisse'

        LeakTTS(queue_tts=_queue(tmp_path, 1, ['可疑句子']), language='zh-cn', tts_type=0).run()
        assert len(list(cache_dir.iterdir())) == 0  # 疑似泄漏的不入缓存
