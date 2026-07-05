"""curated 渠道清单金丝雀：上游若重排渠道 id，这里立即报警。"""
from videotrans.flowui import curated


class TestCuratedCanary:
    def test_all_curated_ids_exist(self):
        for kind, ids in curated.CURATED.items():
            table = curated.id_name_dict(kind)
            for cid in ids:
                assert cid in table, f'{kind} channel id {cid} missing'
                p = table[cid]
                assert p.name

    def test_expected_names(self):
        # 名称锚点：id 重排时这些断言会失败
        assert 'Google' in curated.provider_for('trans', 0).name
        assert 'DeepSeek' in curated.provider_for('trans', 4).name
        assert 'DeepL' in curated.provider_for('trans', 16).name
        assert 'Edge' in curated.provider_for('tts', 0).name
        assert 'Eleven' in curated.provider_for('tts', 20).name.replace(' ', '')
        assert 'Azure' in curated.provider_for('tts', 28).name
        assert 'F5' in curated.provider_for('tts', 8).name
        assert 'whisper' in curated.provider_for('recogn', 0).name.lower()

    def test_free_channels(self):
        assert curated.is_free(curated.provider_for('trans', 0))   # Google
        assert curated.is_free(curated.provider_for('tts', 0))     # Edge-TTS
        assert not curated.is_free(curated.provider_for('trans', 4))  # DeepSeek

    def test_is_configured(self):
        free = curated.provider_for('tts', 0)
        keyed = curated.provider_for('trans', 4)
        assert curated.is_configured(free, lambda k, d=None: '')
        assert not curated.is_configured(keyed, lambda k, d=None: '')
        assert curated.is_configured(keyed, lambda k, d=None: 'sk-xxx')
