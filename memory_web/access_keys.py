"""Recoverable per-space credentials; the Fernet master key lives outside Postgres."""

import hmac
import re
import secrets

from cryptography.fernet import Fernet, InvalidToken

from memory_core.db import connect

TOKEN = re.compile(r'^mk_([0-9a-f]{32})\.([A-Za-z0-9_-]{43})$')


class InvalidAccessKey(ValueError):
    pass


class KeyConfigurationError(RuntimeError):
    pass


class AccessKeys:
    def __init__(self, app_dsn, config_dsn, master_key):
        self.app_dsn, self.config_dsn = app_dsn, config_dsn
        try:
            self.cipher = Fernet(master_key.encode('ascii'))
        except (ValueError, UnicodeError, AttributeError):
            raise ValueError('访问钥匙主密钥格式无效') from None

    def _decode(self, ciphertext):
        try:
            return self.cipher.decrypt(ciphertext.encode()).decode('ascii')
        except (InvalidToken, ValueError, UnicodeError):
            raise KeyConfigurationError('服务器主密钥与已存访问钥匙不匹配，请恢复正确的 MEMORY_ACCESS_KEY_MASTER_KEY 或重新生成空间钥匙') from None

    def get(self, space):
        with connect(self.app_dsn) as connection:
            row = connection.execute('''SELECT key_id,ciphertext,created_at FROM memory_web.space_access_keys
                WHERE bot_id=%s AND revoked_at IS NULL''', (space,)).fetchone()
        if row is None:
            return {'key': None}
        return {'key': self._decode(row['ciphertext']), 'key_id': row['key_id'], 'created_at': row['created_at']}

    def rotate(self, space):
        key_id = secrets.token_hex(16)
        token = 'mk_' + key_id + '.' + secrets.token_urlsafe(32)
        ciphertext = self.cipher.encrypt(token.encode()).decode('ascii')
        with connect(self.config_dsn) as connection:
            # One lock covers revocation and replacement, across all workers.
            row = connection.execute('SELECT bot_id FROM memory_core.space_policies WHERE bot_id=%s FOR UPDATE', (space,)).fetchone()
            if row is None:
                raise ValueError('空间不存在')
            connection.execute('''UPDATE memory_web.space_access_keys SET revoked_at=memory_core.format_timestamp(CURRENT_TIMESTAMP),ciphertext=NULL
                WHERE bot_id=%s AND revoked_at IS NULL''', (space,))
            connection.execute('INSERT INTO memory_web.space_access_keys(key_id,bot_id,ciphertext) VALUES (%s,%s,%s)', (key_id, space, ciphertext))
        return self.get(space)

    def revoke(self, space):
        with connect(self.config_dsn) as connection:
            connection.execute('SELECT bot_id FROM memory_core.space_policies WHERE bot_id=%s FOR UPDATE', (space,))
            connection.execute('''UPDATE memory_web.space_access_keys SET revoked_at=memory_core.format_timestamp(CURRENT_TIMESTAMP),ciphertext=NULL
                WHERE bot_id=%s AND revoked_at IS NULL''', (space,))
        return {'key': None}

    def authenticate(self, supplied):
        match = TOKEN.fullmatch(supplied) if isinstance(supplied, str) else None
        if match is None:
            raise InvalidAccessKey('访问钥匙无效或已作废')
        with connect(self.app_dsn) as connection:
            # The public selector is not a secret. Decrypt exactly one row,
            # then compare the complete secret in constant time; never scan.
            row = connection.execute('''SELECT bot_id,ciphertext FROM memory_web.space_access_keys
                WHERE key_id=%s AND revoked_at IS NULL''', (match.group(1),)).fetchone()
        if row is None or not hmac.compare_digest(self._decode(row['ciphertext']).encode(), supplied.encode()):
            raise InvalidAccessKey('访问钥匙无效或已作废')
        return row['bot_id']

    def authenticate_header(self, authorization):
        scheme, _, token = authorization.partition(' ')
        if scheme.lower() != 'bearer':
            raise InvalidAccessKey('需要有效的空间访问钥匙')
        return self.authenticate(token)
