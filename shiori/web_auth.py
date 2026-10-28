"""Password and session authority for the WebUI, independent of API transport.

Only a trusted caller may decide whether a request actually arrived on loopback.
Never derive ``is_local`` from a forwarded header, Origin, or a client payload.
The legacy bootstrap token is deliberately not accepted as a browser session.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from .settings import _protect, _unprotect


PASSWORD_FILE = 'security/webui-password.json'
COOKIE_NAME = 'shiori_session'
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 256
SESSION_SECONDS = 12 * 60 * 60
_SCRYPT = {'n': 32768, 'r': 8, 'p': 1, 'dklen': 32}
_MAX_FILE_BYTES = 16384


class AuthError(ValueError):
    def __init__(self, message: str, status_code: int = 400, code: str = 'auth_error'):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class WebAuth:
    """Fail-closed, file-backed password state with memory-only browser sessions.

    Windows encrypts the *salted scrypt verifier*, never the password, using the
    current Windows account's DPAPI key. Other systems store only the salted
    verifier and restrict its file permissions; they do not pretend Base64 is
    encryption. Sessions intentionally expire when this process restarts.
    """

    def __init__(self, data_dir: Path, bootstrap_token: Callable[[], str] | str,
                 *, clock: Callable[[], float] = time.time):
        self.data_dir = Path(data_dir).resolve()
        self.path = self.data_dir / PASSWORD_FILE
        self._bootstrap_token = bootstrap_token
        self._clock = clock
        self._lock = threading.RLock()
        self._fingerprint = None
        self._record: dict = {}
        self._error = ''
        self._sessions: dict[str, dict] = {}
        self._failures: dict[str, list[float]] = {}
        self._global_failures: list[float] = []
        with self._lock:
            self._refresh()

    @property
    def storage_protection(self) -> str:
        return 'windows-dpapi+scrypt' if os.name == 'nt' else 'scrypt+file-permissions'

    def reset_from_host(self):
        """Local OS entry point only. Deliberately has no HTTP route."""
        with self._lock:
            self._sessions.clear()
            self._write({'mode':'setup','generation':secrets.token_hex(16)})
            self._require_healthy()

    @staticmethod
    def _dump(value: dict) -> bytes:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':')).encode('utf-8')

    def _read_bytes(self):
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise ValueError('密码文件不能是符号链接')
        with self.path.open('rb') as stream:
            stat = os.fstat(stream.fileno())
            if stat.st_size > _MAX_FILE_BYTES:
                raise ValueError('密码文件过大')
            raw = stream.read(_MAX_FILE_BYTES + 1)
        if len(raw) > _MAX_FILE_BYTES:
            raise ValueError('密码文件过大')
        fingerprint = (stat.st_dev, stat.st_ino, stat.st_mtime_ns,
                       stat.st_ctime_ns, hashlib.sha256(raw).hexdigest())
        return raw, fingerprint

    def _decode(self, raw: bytes) -> dict:
        envelope = json.loads(raw)
        if not isinstance(envelope, dict) or envelope.get('version') != 1:
            raise ValueError('不支持的密码文件版本')
        protection = envelope.get('protection')
        if protection == 'windows-dpapi':
            payload = envelope.get('payload')
            if os.name != 'nt' or not isinstance(payload, str) or not payload.startswith('dpapi:'):
                raise ValueError('当前系统不能解密密码文件')
            record = json.loads(_unprotect(payload))
        elif protection == 'scrypt-verifier' and os.name != 'nt':
            record = envelope.get('record')
        else:
            raise ValueError('密码文件加密方式无效')
        if not isinstance(record, dict) or record.get('mode') not in ('setup', 'password', 'local_only'):
            raise ValueError('密码状态无效')
        if not isinstance(record.get('generation'), str) or len(record['generation']) != 32:
            raise ValueError('密码版本无效')
        credential = record.get('credential')
        if record['mode'] == 'password':
            if not isinstance(credential, dict) or credential.get('algorithm') != 'scrypt':
                raise ValueError('密码校验信息无效')
            if credential.get('parameters') != _SCRYPT:
                raise ValueError('密码校验参数无效')
            salt = base64.b64decode(credential.get('salt', ''), validate=True)
            digest = base64.b64decode(credential.get('digest', ''), validate=True)
            if len(salt) != 32 or len(digest) != 32:
                raise ValueError('密码校验信息无效')
        elif credential is not None:
            raise ValueError('免密或待设置状态不应含有密码')
        return record

    def _write(self, record: dict):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.parent.is_symlink() or self.path.is_symlink():
            raise AuthError('密码文件路径不安全，拒绝写入', 503, 'password_file_error')
        if os.name != 'nt':
            self.path.parent.chmod(0o700)
        if os.name == 'nt':
            envelope = {'version': 1, 'protection': 'windows-dpapi',
                        'payload': _protect(self._dump(record).decode('utf-8'))}
        else:
            envelope = {'version': 1, 'protection': 'scrypt-verifier', 'record': record}
        raw = self._dump(envelope)
        descriptor, name = tempfile.mkstemp(prefix='.password-', dir=self.path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                if os.name != 'nt':
                    os.fchmod(stream.fileno(), 0o600)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
        self._sessions.clear()
        self._fingerprint = None

    def _refresh(self):
        """Observe external changes before every auth operation, including delete."""
        try:
            try:
                raw, fingerprint = self._read_bytes()
            except FileNotFoundError:
                self._sessions.clear()
                self._write({'mode': 'setup', 'generation': secrets.token_hex(16)})
                raw, fingerprint = self._read_bytes()
            if fingerprint == self._fingerprint:
                return
            self._sessions.clear()
            self._record = self._decode(raw)
            self._fingerprint = fingerprint
            self._error = ''
        except (OSError, ValueError, TypeError, KeyError, UnicodeError) as exc:
            self._sessions.clear()
            self._record = {}
            self._fingerprint = None
            self._error = '密码文件无法读取。请在本机关闭软件后检查或删除该文件，再重新设置密码。'

    def _require_healthy(self):
        self._refresh()
        if self._error:
            raise AuthError(self._error, 503, 'password_file_error')

    def _local_proof(self, is_local: bool, token: str):
        if not is_local:
            raise AuthError('首次设置或启用本机免密须在运行 Shiori 的电脑上完成', 403, 'local_setup_required')
        expected = self._bootstrap_token() if callable(self._bootstrap_token) else self._bootstrap_token
        valid = False
        if isinstance(token, str) and isinstance(expected, str) and len(expected) >= 32 and len(token) <= 256:
            try:
                valid = hmac.compare_digest(token.encode('utf-8'), expected.encode('utf-8'))
            except UnicodeError:
                pass
        if not valid:
            raise AuthError('本机设置凭据无效，请从 Shiori 程序重新打开设置页面', 403, 'bootstrap_required')

    @staticmethod
    def _validate_password(password: str):
        if not isinstance(password, str) or not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
            raise AuthError('密码长度须为 8 至 256 个字符', 400, 'invalid_password')
        try:
            byte_length = len(password.encode('utf-8'))
        except UnicodeError:
            byte_length = 1025
        if not password.strip() or byte_length > 1024:
            raise AuthError('密码格式无效', 400, 'invalid_password')

    @staticmethod
    def _credential(password: str) -> dict:
        salt = secrets.token_bytes(32)
        digest = hashlib.scrypt(password.encode('utf-8'), salt=salt, maxmem=64 * 1024 * 1024, **_SCRYPT)
        return {'algorithm': 'scrypt', 'parameters': dict(_SCRYPT),
                'salt': base64.b64encode(salt).decode('ascii'),
                'digest': base64.b64encode(digest).decode('ascii')}

    def _verify(self, password: str) -> bool:
        if not isinstance(password, str) or len(password) > MAX_PASSWORD_LENGTH or self._record.get('mode') != 'password':
            return False
        credential = self._record['credential']
        try:
            raw_password = password.encode('utf-8')
        except UnicodeError:
            return False
        digest = hashlib.scrypt(raw_password, salt=base64.b64decode(credential['salt']),
                                maxmem=64 * 1024 * 1024, **_SCRYPT)
        return hmac.compare_digest(digest, base64.b64decode(credential['digest']))

    def _session(self) -> dict:
        now = self._clock()
        self._sessions = {key: item for key, item in self._sessions.items() if item['expires_at'] > now}
        while len(self._sessions) >= 32:
            self._sessions.pop(next(iter(self._sessions)))
        token = secrets.token_urlsafe(48)
        result = {'token': token, 'expires_at': now + SESSION_SECONDS}
        self._sessions[hashlib.sha256(token.encode()).hexdigest()] = {'expires_at': result['expires_at']}
        return result

    def _session_current(self, token: str) -> dict | None:
        if not isinstance(token, str) or len(token) > 256:
            return None
        try:
            key = hashlib.sha256(token.encode()).hexdigest()
        except UnicodeError:
            return None
        item = self._sessions.get(key)
        if item and item['expires_at'] > self._clock():
            return item
        self._sessions.pop(key, None)
        return None

    def authenticate(self, token: str = '', *, is_local: bool = False) -> bool:
        with self._lock:
            self._refresh()
            if self._error:
                return False
            mode = self._record.get('mode')
            if mode == 'local_only':
                return bool(is_local)
            return mode == 'password' and self._session_current(token) is not None

    def status(self, session_token: str = '', *, is_local: bool = False) -> dict:
        with self._lock:
            self._refresh()
            mode = 'error' if self._error else self._record.get('mode', 'error')
            session = self._session_current(session_token) if mode == 'password' else None
            result = {'mode': mode, 'setup_required': mode == 'setup',
                      'password_set': mode == 'password', 'password_configured': mode == 'password',
                      'authenticated': bool(session or (mode == 'local_only' and is_local)),
                      'local_only': mode == 'local_only', 'storage_protection': self.storage_protection,
                      'session_expires_at': session['expires_at'] if session else None}
            if self._error:
                result['error'] = self._error
            if is_local:
                result['password_file'] = str(self.path)
            return result

    def setup(self, password: str, *, is_local: bool, bootstrap_token: str) -> dict:
        with self._lock:
            self._require_healthy()
            self._local_proof(is_local, bootstrap_token)
            if self._record['mode'] not in ('setup', 'local_only'):
                raise AuthError('已设置密码，请使用修改密码功能', 409, 'already_configured')
            self._validate_password(password)
            self._write({'mode': 'password', 'generation': secrets.token_hex(16),
                         'credential': self._credential(password)})
            self._require_healthy()
            self._failures.clear()
            self._global_failures.clear()
            return self._session()

    def enable_local_only(self, *, is_local: bool, bootstrap_token: str):
        """An explicit no-password alternative; it never authenticates remote UI."""
        with self._lock:
            self._require_healthy()
            self._local_proof(is_local, bootstrap_token)
            if self._record['mode'] == 'password':
                raise AuthError('已有密码时请先在本机删除密码文件并重新打开，或继续使用密码保护',
                                409, 'password_exists')
            self._write({'mode': 'local_only', 'generation': secrets.token_hex(16)})
            self._require_healthy()

    def _check_rate(self, peer: str):
        now = self._clock()
        self._failures = {key: [stamp for stamp in stamps if stamp > now - 60]
                          for key, stamps in self._failures.items() if any(stamp > now - 60 for stamp in stamps)}
        self._global_failures = [stamp for stamp in self._global_failures if stamp > now - 60]
        key = peer[:128] if isinstance(peer, str) else ''
        # A global budget also bounds attempts from rotating/spoofed peer values.
        if len(self._failures.get(key, [])) >= 5 or len(self._global_failures) >= 100:
            raise AuthError('登录失败次数过多，请在一分钟后重试', 429, 'rate_limited')
        return key

    def login(self, password: str, *, peer: str = '') -> dict:
        with self._lock:
            self._require_healthy()
            if self._record['mode'] != 'password':
                raise AuthError('请先在本机设置密码；本机免密模式不允许远程登录', 403, 'password_required')
            key = self._check_rate(peer)
            if not self._verify(password):
                now = self._clock()
                self._failures.setdefault(key, []).append(now)
                self._global_failures.append(now)
                raise AuthError('密码不正确', 401, 'invalid_credentials')
            self._failures.pop(key, None)
            return self._session()

    def logout(self, token: str):
        with self._lock:
            self._refresh()
            if isinstance(token, str) and len(token) <= 256:
                try:
                    self._sessions.pop(hashlib.sha256(token.encode()).hexdigest(), None)
                except UnicodeError:
                    pass

    def change_password(self, current_password: str, new_password: str, *, token: str,
                        is_local: bool = False) -> dict:
        with self._lock:
            self._require_healthy()
            if self._record['mode'] != 'password' or self._session_current(token) is None:
                raise AuthError('登录已失效，请重新登录', 401, 'authentication_required')
            key = self._check_rate('change:' + hashlib.sha256(token.encode()).hexdigest())
            if not self._verify(current_password):
                now = self._clock()
                self._failures.setdefault(key, []).append(now)
                self._global_failures.append(now)
                raise AuthError('当前密码不正确', 401, 'invalid_credentials')
            self._validate_password(new_password)
            self._write({'mode': 'password', 'generation': secrets.token_hex(16),
                         'credential': self._credential(new_password)})
            self._require_healthy()
            self._failures.clear()
            self._global_failures.clear()
            return self._session()


# A descriptive alias for integration code preferring the generic manager name.
AuthManager = WebAuth
