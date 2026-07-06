import pytest

pytest.importorskip("cryptography")

from videotrans.configure.secret_store import (
    _PREFIX, decrypt_params, encrypt_params, _is_sensitive,
)


class TestSensitiveDetection:
    def test_matches(self):
        assert _is_sensitive('deepseek_key')
        assert _is_sensitive('deepl_authkey')
        assert _is_sensitive('deepgram_apikey')
        assert _is_sensitive('zijierecognmodel_token')

    def test_non_sensitive(self):
        assert not _is_sensitive('source_language')
        assert not _is_sensitive('model_name')
        assert not _is_sensitive('voice_role')


class TestEncryptDecrypt:
    def test_round_trip(self):
        data = {'deepseek_key': 'sk-abc123', 'source_language': 'en'}
        enc = encrypt_params(data)
        assert enc['deepseek_key'].startswith(_PREFIX)
        assert enc['deepseek_key'] != 'sk-abc123'
        assert enc['source_language'] == 'en'      # 非敏感字段不动
        dec = decrypt_params(enc)
        assert dec['deepseek_key'] == 'sk-abc123'
        assert dec['source_language'] == 'en'

    def test_empty_value_untouched(self):
        enc = encrypt_params({'chatgpt_key': ''})
        assert enc['chatgpt_key'] == ''

    def test_already_encrypted_not_double(self):
        once = encrypt_params({'gemini_key': 'k'})
        twice = encrypt_params(once)
        assert once['gemini_key'] == twice['gemini_key']

    def test_plaintext_load_preserved(self):
        # 旧的明文文件（无前缀）加载时原样保留
        dec = decrypt_params({'openai_key': 'plainkey', 'x': 1})
        assert dec['openai_key'] == 'plainkey'
        assert dec['x'] == 1

    def test_corrupt_ciphertext_cleared(self):
        # 换机器/损坏 → 解密失败 → 清空，不把密文当明文
        dec = decrypt_params({'azure_speech_key': _PREFIX + 'not-a-valid-token'})
        assert dec['azure_speech_key'] == ''
