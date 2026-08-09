"""配音质量预设：把 6 个时间↔质量开关收敛成一个选择。"""
import pytest

from videotrans.dub import presets


class TestNormalize:
    @pytest.mark.parametrize('raw,expected', [
        ('fast', 'fast'), ('QUALITY', 'quality'), (' balanced ', 'balanced'),
        ('custom', 'custom'), ('', 'balanced'), (None, 'balanced'),
        ('nonsense', 'balanced'),
    ])
    def test_normalize(self, raw, expected):
        assert presets.normalize(raw) == expected


class TestValues:
    def test_all_presets_cover_the_same_keys(self):
        keysets = [set(v) for v in presets.PRESETS.values()]
        assert all(k == keysets[0] for k in keysets)
        assert set(presets.MANAGED_KEYS) == keysets[0]

    def test_quality_is_slower_than_fast(self):
        fast = presets.values_for('fast')
        quality = presets.values_for('quality')
        assert quality['f5tts_nfe'] > fast['f5tts_nfe']
        assert quality['f5tts_preflight_samples'] > fast['f5tts_preflight_samples']
        assert quality['f5tts_ref_similarity'] > fast['f5tts_ref_similarity']
        # 校验批次越小 → 每批更细 → 更慢更严
        assert quality['f5tts_validation_batch_size'] < fast['f5tts_validation_batch_size']

    def test_balanced_matches_shipping_defaults(self):
        # 均衡档必须等于此前的内置默认，升级用户行为不变
        balanced = presets.values_for('balanced')
        assert balanced['f5tts_nfe'] == 32
        assert balanced['f5tts_preflight_samples'] == 5
        assert balanced['f5tts_ref_similarity'] == 0.75
        assert balanced['f5tts_multi_speaker'] is True

    def test_custom_covers_nothing(self):
        assert presets.values_for('custom') == {}


class TestApply:
    def test_writes_into_settings(self):
        store = {}
        applied = presets.apply('fast', store)
        assert store['f5tts_nfe'] == 16
        assert store['f5tts_multi_speaker'] is False
        assert applied == presets.values_for('fast')

    def test_custom_leaves_expert_values_alone(self):
        store = {'f5tts_nfe': 24, 'f5tts_ref_similarity': 0.9}
        assert presets.apply('custom', store) == {}
        assert store['f5tts_nfe'] == 24          # 专家配置未被覆盖
        assert store['f5tts_ref_similarity'] == 0.9

    def test_switching_preset_overwrites_previous(self):
        store = {}
        presets.apply('quality', store)
        assert store['f5tts_nfe'] == 40
        presets.apply('fast', store)
        assert store['f5tts_nfe'] == 16          # 不残留上一档的值

    def test_unknown_name_falls_back_to_default(self):
        store = {}
        presets.apply('bogus', store)
        assert store['f5tts_nfe'] == presets.values_for('balanced')['f5tts_nfe']


class TestCurrent:
    def test_reads_from_settings(self):
        assert presets.current({'f5tts_preset': 'quality'}) == 'quality'

    def test_missing_key_defaults(self):
        assert presets.current({}) == presets.DEFAULT_PRESET


class TestDescribe:
    def test_summary_mentions_key_dials(self):
        text = presets.describe('quality')
        assert 'nfe=40' in text and 'preflight=8' in text

    def test_custom_has_no_summary(self):
        assert presets.describe('custom') == ''


class TestRegistration:
    def test_managed_keys_are_registered_defaults(self):
        """预设覆盖的键必须都在 _get_defaults 里，否则 cfg.json 合并会丢弃它们。"""
        from videotrans.configure.config import settings
        defaults = settings._get_defaults()
        for key in presets.MANAGED_KEYS:
            assert key in defaults, key
        assert 'f5tts_preset' in defaults
        # 曾经漏注册，导致 cfg.json 里写了也不生效
        assert 'f5tts_speaker_identity_gate' in defaults
