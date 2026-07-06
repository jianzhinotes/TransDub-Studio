"""params.json 里 API 密钥的本地加密：磁盘存密文，内存/界面用明文。

机器绑定：密钥由本机特征派生，换机器/被误传时密文无法解开（自动清空，
需重填），避免密钥以明文形式裸露在配置文件里。无 Qt 依赖，可单测。
"""
import base64
import getpass
import hashlib
import uuid

_PREFIX = 'ENCv1:'
# 字段名含这些子串且值为非空字符串时视为敏感（覆盖 *_key/authkey/apikey/*token/*secret）
_SENSITIVE = ('key', 'token', 'secret')


def _is_sensitive(name: str) -> bool:
    n = name.lower()
    return any(s in n for s in _SENSITIVE)


def _fernet():
    from cryptography.fernet import Fernet
    raw = f'{uuid.getnode()}|{getpass.getuser()}|transdub-studio-secret-v1'.encode()
    key = base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
    return Fernet(key)


def encrypt_params(data: dict) -> dict:
    """返回副本：敏感字段加密并加前缀；已加密/非敏感/空值原样保留。"""
    try:
        f = _fernet()
    except Exception:
        return dict(data)   # 加密不可用时退回明文，不阻断保存
    out = dict(data)
    for k, v in data.items():
        if _is_sensitive(k) and isinstance(v, str) and v and not v.startswith(_PREFIX):
            try:
                out[k] = _PREFIX + f.encrypt(v.encode()).decode()
            except Exception:
                pass
    return out


def decrypt_params(data: dict) -> dict:
    """返回副本：带前缀的密文解密；无前缀的当明文原样保留（兼容旧文件）；
    解密失败（换机器/损坏）则清空该字段，避免把密文误当明文使用。"""
    f = None
    out = dict(data)
    for k, v in data.items():
        if isinstance(v, str) and v.startswith(_PREFIX):
            if f is None:
                try:
                    f = _fernet()
                except Exception:
                    out[k] = ''
                    continue
            try:
                out[k] = f.decrypt(v[len(_PREFIX):].encode()).decode()
            except Exception:
                out[k] = ''
    return out
