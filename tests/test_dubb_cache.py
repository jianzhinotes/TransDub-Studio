from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

import videotrans.tts._base as tts_base
from videotrans.tts._base import BaseTTS
from videotrans.dub.performance_report import TTS_RUN_STATS_FILE


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
    def test_item_callback_runs_before_a_later_batch_failure(self, tmp_path, cache_dir):
        completed = []

        class PartialFailureTTS(FakeTTS):
            def _exec(self):
                self._item_task(self.queue_tts[0], 0)
                raise ValueError("backend stopped")

        queue = _queue(tmp_path, "callback", ["已经完成", "尚未开始"])
        with pytest.raises(ValueError, match="backend stopped"):
            PartialFailureTTS(
                queue_tts=queue,
                language="zh-cn",
                tts_type=0,
                on_item_done=lambda item, index: completed.append(
                    (index, Path(item["filename"]).read_bytes())
                ),
            ).run()

        assert completed == [(0, b"RIFFfake-audio-bytes")]

    def test_project_checkpoint_survives_process_specific_output_path(self, tmp_path):
        from videotrans.task.trans_create import TransCreate

        task = TransCreate.__new__(TransCreate)
        task.cfg = SimpleNamespace(
            target_dir=str(tmp_path / "output"),
            noextname="demo",
            target_language_code="zh-cn",
            tts_type=8,
            clear_cache=False,
        )
        task.signal = lambda **kwargs: None
        first = tmp_path / "pid-1" / "smart-0.wav"
        first.parent.mkdir()
        first.write_bytes(b"RIFF-persisted-candidate")
        common = {
            "dub_unit_id": "unit-0", "text": "持久恢复候选",
            "role": "clone", "rate": "+0%", "volume": "+0%",
            "pitch": "+0Hz", "ref_text": "source sentence",
            "start_time_source": 100, "end_time_source": 900,
        }
        task.queue_tts = [{**common, "filename": str(first)}]

        assert task._save_dubbing_checkpoint() == 1

        second = tmp_path / "pid-2" / "smart-0.wav"
        task.queue_tts = [{**common, "filename": str(second)}]
        assert task._restore_dubbing_checkpoint() == 1
        assert second.read_bytes() == b"RIFF-persisted-candidate"

    def test_project_checkpoint_is_written_one_item_at_a_time(self, tmp_path):
        from videotrans.task.trans_create import TransCreate

        task = TransCreate.__new__(TransCreate)
        task.cfg = SimpleNamespace(
            target_dir=str(tmp_path / "output"),
            noextname="demo",
            target_language_code="zh-cn",
            tts_type=8,
            clear_cache=False,
        )
        source = tmp_path / "process-cache" / "smart-0.wav"
        source.parent.mkdir()
        source.write_bytes(b"RIFF-immediate-candidate")
        (source.parent / "synthesis_supervisor.json").write_text(
            '{"completed": 1, "timeouts": 0}', encoding="utf-8")
        item = {
            "dub_unit_id": "unit-0", "text": "立即保存候选",
            "role": "clone", "rate": "+0%", "volume": "+0%",
            "pitch": "+0Hz", "ref_text": "source sentence",
            "start_time_source": 100, "end_time_source": 900,
            "filename": str(source),
        }

        task._save_dubbing_checkpoint_item(item, 0)

        root = task._dubbing_checkpoint_dir()
        assert len(list((root / "audio").glob("*.wav"))) == 1
        assert (root / "manifest.json").is_file()
        assert (root / "supervisor.json").is_file()

    def test_successful_prefix_is_cached_even_when_run_raises(self, tmp_path, cache_dir):
        class PartialFailureTTS(FakeTTS):
            def _exec(self):
                self._item_task(self.queue_tts[0], 0)
                raise ValueError("backend stopped")

        queue = _queue(tmp_path, 1, ['已经生成', '尚未生成'])
        with pytest.raises(ValueError, match="backend stopped"):
            PartialFailureTTS(
                queue_tts=queue, language='zh-cn', tts_type=0).run()

        assert len(list(cache_dir.iterdir())) == 1
        resumed = FakeTTS(
            queue_tts=_queue(tmp_path, 2, ['已经生成', '尚未生成']),
            language='zh-cn', tts_type=0)
        before = FakeTTS.calls
        resumed.run()
        assert FakeTTS.calls == before + 1

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
        stats = Path(t2.queue_tts[0]['filename']).parent / TTS_RUN_STATS_FILE
        assert stats.is_file()

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

    def test_chinese_anchor_changes_cache_key(self, tmp_path, cache_dir):
        ref_a = tmp_path / 'anchor-a.wav'
        ref_b = tmp_path / 'anchor-b.wav'
        ref_a.write_bytes(b'RIFF-anchor-a')
        ref_b.write_bytes(b'RIFF-anchor-b')
        t = FakeTTS(queue_tts=_queue(tmp_path, 1, ['中文锚点']), language='zh-cn', tts_type=0)
        item = t.queue_tts[0]
        item['chinese_anchor_ref'] = str(ref_a)
        key_a = t._dubb_cache_key(item)
        item['chinese_anchor_ref'] = str(ref_b)
        assert t._dubb_cache_key(item) != key_a
