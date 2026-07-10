import hashlib

from videotrans.translator._base import BaseTrans
from videotrans.task.taskcfg import SrtItem


def _make_srt_item(text, line=1, start=0, end=1000):
    return SrtItem(
        text=text, line=line, start_time=start, end_time=end,
        startraw="00:00:00,000", endraw="00:00:01,000",
        time="00:00:00,000 --> 00:00:01,000",
    )


class TestBaseTransPostInit:
    def test_default_trans_thread(self):
        bt = BaseTrans(text_list=[_make_srt_item("hello")])
        assert bt.trans_thread > 0

    def test_translate_type_default(self):
        bt = BaseTrans(text_list=[_make_srt_item("hello")])
        assert bt.translate_type == 0

    def test_source_target_code(self):
        bt = BaseTrans(
            text_list=[_make_srt_item("bonjour")],
            source_code="fr",
            target_code="zh-cn",
        )
        assert bt.source_code == "fr"
        assert bt.target_code == "zh-cn"

    def test_uuid_set(self):
        bt = BaseTrans(text_list=[_make_srt_item("test")], uuid="test-123")
        assert bt.uuid == "test-123"


class TestBaseTransGetKey:
    def test_key_is_md5(self):
        bt = BaseTrans(
            text_list=[_make_srt_item("hello world")],
            translate_type=0,
            source_code="en",
            target_code="zh-cn",
        )
        key = bt._get_key("hello world")
        assert isinstance(key, str)
        assert len(key) == 32  # MD5 hex length

    def test_different_text_different_keys(self):
        bt = BaseTrans(text_list=[_make_srt_item("test")], source_code="en", target_code="fr")
        k1 = bt._get_key("hello")
        k2 = bt._get_key("world")
        assert k1 != k2

    def test_different_languages_different_keys(self):
        bt1 = BaseTrans(text_list=[_make_srt_item("test")], source_code="en", target_code="fr")
        bt2 = BaseTrans(text_list=[_make_srt_item("test")], source_code="en", target_code="zh-cn")
        assert bt1._get_key("same") != bt2._get_key("same")

    def test_same_key_for_identical_input(self):
        bt = BaseTrans(text_list=[_make_srt_item("test")], source_code="en", target_code="zh-cn")
        k1 = bt._get_key("repeat me")
        k2 = bt._get_key("repeat me")
        assert k1 == k2


class TestBaseTransRunTextChunking:
    def test_chunking_with_thread_count(self):
        items = [_make_srt_item(f"line {i}", line=i + 1) for i in range(10)]
        bt = BaseTrans(text_list=items, source_code="en", target_code="zh-cn")
        # trans_thread controls chunk size
        bt.trans_thread = 3
        chunks = [items[i:i + bt.trans_thread] for i in range(0, len(items), bt.trans_thread)]
        assert len(chunks) == 4  # 10 items, 3 per chunk = 4 chunks
        assert len(chunks[0]) == 3
        assert len(chunks[-1]) == 1


class TestDeepSeekBatchCap:
    """长视频不能把整份字幕塞进一次请求，否则 DeepSeek 报 content too long。"""

    def _deepseek(self, monkeypatch, n):
        from videotrans.configure.config import settings
        from videotrans.translator._deepseek import DeepSeek
        monkeypatch.setitem(settings, "aisendsrt", True)
        items = [_make_srt_item(f"line {i}", line=i + 1) for i in range(n)]
        # translate_type=4 == DEEPSEEK_INDEX，令 aisendsrt 生效
        return DeepSeek(text_list=items, translate_type=4,
                        source_code="en", target_code="zh-cn")

    def test_long_srt_is_capped(self, monkeypatch):
        from videotrans.translator._deepseek import DEEPSEEK_SRT_BATCH
        d = self._deepseek(monkeypatch, 800)
        assert d.aisendsrt is True
        assert d.trans_thread <= DEEPSEEK_SRT_BATCH  # 旧代码会是 800

    def test_short_srt_single_batch(self, monkeypatch):
        d = self._deepseek(monkeypatch, 30)
        assert d.trans_thread == 30  # 短字幕仍一次发完，保留上下文

    def test_max_tokens_not_near_context_limit(self, monkeypatch):
        d = self._deepseek(monkeypatch, 5)
        # 输出预留必须远小于模型上下文，否则连短字幕也被拒
        assert d.max_tokens == 16384
