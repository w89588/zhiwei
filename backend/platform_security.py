"""Authentication and user-scoped authenticated encryption; no plaintext chat indexes."""
import base64
import hashlib
import hmac
import json
import secrets
from Crypto.Cipher import AES


def encode(value):
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


def decode(value):
    return base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def secret(prefix=''):
    return prefix + secrets.token_urlsafe(32)


def password_hash(password):
    if not 12 <= len(password) <= 256:
        raise ValueError('密码必须为 12–256 个字符')
    salt = secrets.token_bytes(16)
    return 'pbkdf2$600000$' + encode(salt) + '$' + encode(hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 600000))


def password_valid(password, stored):
    try:
        _, rounds, salt, expected = stored.split('$')
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), decode(salt), int(rounds))
        return hmac.compare_digest(actual, decode(expected))
    except (ValueError, TypeError):
        return False


def seal(key, data, aad):
    nonce = secrets.token_bytes(12)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    cipher.update(aad.encode())
    ciphertext, tag = cipher.encrypt_and_digest(json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode())
    return {'nonce': encode(nonce), 'ciphertext': encode(ciphertext), 'tag': encode(tag)}


def unseal(key, data, aad):
    try:
        nonce, tag = decode(data['nonce']), decode(data['tag'])
        if len(nonce) != 12 or len(tag) != 16:
            raise ValueError()
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        cipher.update(aad.encode())
        return json.loads(cipher.decrypt_and_verify(decode(data['ciphertext']), tag))
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise ValueError('加密数据校验失败') from None


class Vault:
    def __init__(self, master):
        self.master = decode(master)
        if len(self.master) != 32:
            raise ValueError('WECHAT_MASTER_KEY 必须是 32 字节随机密钥的 base64url 编码')

    def key(self, user_id):
        return hmac.new(self.master, ('wechat-v2:user:' + user_id).encode(), hashlib.sha256).digest()

    def encrypt(self, user_id, kind, object_id, data):
        return json.dumps(seal(self.key(user_id), data, f'{user_id}:{kind}:{object_id}'))

    def decrypt(self, user_id, kind, object_id, data):
        return unseal(self.key(user_id), json.loads(data), f'{user_id}:{kind}:{object_id}')
