"""Administrator identities and local-confirmed QQ pairing.

Identity proofs are created only at authenticated ingress. A nickname, an
is_admin flag or a caller-provided platform dictionary is never a credential.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import re
import secrets
import threading
import time


def normalize_administrators(items):
    if not isinstance(items, list) or len(items) > 100:
        raise ValueError('管理员账号最多100个，须为列表')
    result, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('管理员账号格式不正确')
        platform = item.get('platform')
        account = str(item.get('account_id', '')).strip()
        instance = str(item.get('instance_id', '')).strip()
        if platform not in ('onebot', 'qqofficial'):
            raise ValueError('管理员平台须为onebot或qqofficial；本地Shiori管理员内置')
        if platform == 'onebot':
            valid = bool(re.fullmatch(r'\d{1,20}', account)) and (not instance or bool(re.fullmatch(r'\d{1,20}', instance)))
        else:
            valid = bool(re.fullmatch(r'[A-Za-z0-9_.-]{1,180}', account)) and (not instance or bool(re.fullmatch(r'[A-Za-z0-9_.-]{1,180}', instance)))
        if not valid:
            raise ValueError('管理员真实账号或机器人实例ID格式错误')
        key = platform, account, instance
        if key in seen:
            continue
        seen.add(key)
        result.append({'platform':platform, 'account_id':account, 'instance_id':instance,
                       'label':str(item.get('label', '')).strip()[:80]})
    return result


def chat_allowed(config, platform, kind, account_id):
    """One admission policy for ingress, recall, scheduling and delivery.

    Missing mode means the historical allowlist policy. It must never silently
    widen access for an older configuration or a third-party settings provider.
    """
    if platform not in ('onebot', 'qqofficial') or kind not in ('group', 'private'):
        return False
    section = config.get('onebot' if platform == 'onebot' else 'qq_official', {})
    suffix = 'groups' if kind == 'group' else 'users'
    if section.get('chat_policy', 'whitelist') == 'blacklist':
        return str(account_id) not in {str(value) for value in section.get('blocked_' + suffix, [])}
    # Read-only compatibility for simple legacy settings providers. Settings
    # migrates real persisted owner_ids into allowed_users on load/save.
    values = section.get('allowed_' + suffix, [])
    if kind == 'private' and platform == 'onebot' and section.get('owner_ids'):
        values = [*values, *section['owner_ids']]
    return str(account_id) in {str(value) for value in values}


def private_allowed(config, platform, account_id):
    return chat_allowed(config, platform, 'private', account_id)


class AccessManager:
    def __init__(self, settings, *, clock=time.time):
        self.settings, self.clock = settings, clock
        self._secret = str(getattr(settings, 'token', '') or secrets.token_urlsafe(48)).encode()
        self._lock = threading.RLock()
        self._pairings = {}

    @staticmethod
    def _fields(identity):
        if not isinstance(identity, dict):
            return None
        fields = {key:identity.get(key, '') for key in ('platform','account_id','instance_id')}
        if any(not isinstance(value, str) for value in fields.values()):
            return None
        return fields

    def _proof(self, fields):
        raw = json.dumps(fields, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()
        return hmac.new(self._secret, b'mikubot:trusted-identity:v1:' + raw, hashlib.sha256).hexdigest()

    def identity(self, platform, account_id, instance_id=''):
        """Call only from an authenticated local API or a verified bridge."""
        fields = {'platform':str(platform), 'account_id':str(account_id), 'instance_id':str(instance_id)}
        if platform == 'local':
            if account_id not in ('shiori', 'mikubot') or instance_id:
                raise ValueError('本地管理员身份无效')
        else:
            normalize_administrators([fields])
        return dict(fields, proof=self._proof(fields))

    def valid_identity(self, identity):
        fields = self._fields(identity)
        return bool(fields and isinstance(identity.get('proof'), str)
                    and hmac.compare_digest(identity['proof'], self._proof(fields)))

    def is_admin(self, identity):
        if not self.valid_identity(identity):
            return False
        if identity['platform'] == 'local':
            return identity['account_id'] in ('shiori', 'mikubot') and not identity['instance_id']
        # A legacy in-memory settings adapter may not run Settings' migration;
        # real settings always clears owner_ids when loading or saving.
        if identity['platform'] == 'onebot' and identity['account_id'] in {
                str(value) for value in self.settings.get().get('onebot', {}).get('owner_ids', [])}:
            return True
        for item in self.settings.get().get('access', {}).get('administrators', []):
            if (item.get('platform') == identity['platform'] and item.get('account_id') == identity['account_id']
                    and (not item.get('instance_id') or item['instance_id'] == identity['instance_id'])):
                return True
        return False

    def list_administrators(self):
        entries = [{'platform':'local','account_id':'shiori','instance_id':'','label':'本地管理聊天','built_in':True},
                   *copy.deepcopy(self.settings.get().get('access', {}).get('administrators', []))]
        return [dict(item, id=self.administrator_id(item)) for item in entries]

    @staticmethod
    def administrator_id(identity):
        raw = '\x00'.join(str(identity.get(key, '')) for key in ('platform','account_id','instance_id'))
        return hashlib.sha256(raw.encode()).hexdigest()[:20]

    def remove(self, administrator_id):
        item = next((x for x in self.list_administrators() if x['id'] == administrator_id), None)
        if item is None:
            raise KeyError('管理员账号不存在')
        return self.revoke(item)

    def revoke(self, identity):
        fields = self._fields(identity)
        if not fields or fields['platform'] == 'local':
            raise ValueError('内置本地管理员不能移除')
        with self._lock:
            old = self.settings.get().get('access', {}).get('administrators', [])
            new = [item for item in old if any(item.get(key, '') != fields[key] for key in fields)]
            self.settings.update({'access':{'administrators':new}})
            return len(old) != len(new)

    def _expire(self):
        now = self.clock()
        for key, item in list(self._pairings.items()):
            if item['expires_at'] <= now:
                del self._pairings[key]

    def create_pairing(self, ttl_seconds=300, *, automatic=False):
        if type(automatic) is not bool:
            raise ValueError('自动配对开关无效')
        if automatic: ttl_seconds = 60
        if type(ttl_seconds) is not int or not 30 <= ttl_seconds <= 900:
            raise ValueError('配对码有效期须为30—900秒')
        with self._lock:
            self._expire()
            if len(self._pairings) >= 5:
                raise ValueError('最多同时保留5个配对请求，请先取消旧请求')
            item = {'id':secrets.token_urlsafe(12), 'code':secrets.token_hex(8 if automatic else 5).upper(),
                    'expires_at':self.clock() + ttl_seconds, 'status':'waiting', 'automatic':automatic}
            self._pairings[item['id']] = item
            return copy.deepcopy(item)

    def list_pairings(self):
        with self._lock:
            self._expire()
            return copy.deepcopy(list(self._pairings.values()))

    def status(self, pairing_id):
        with self._lock:
            self._expire()
            if pairing_id not in self._pairings:
                raise KeyError('配对请求已过期或不存在')
            return copy.deepcopy(self._pairings[pairing_id])

    @staticmethod
    def pairing_code(text):
        match = re.fullmatch(r'(?:/绑定|绑定)\s+([A-Fa-f0-9]{10}|[A-Fa-f0-9]{16})', str(text).strip())
        return match.group(1).upper() if match else None

    def capture_pairing(self, code, identity, *, private=True):
        if not private or not self.valid_identity(identity) or identity['platform'] not in ('onebot','qqofficial') or not identity['instance_id']:
            return False
        with self._lock:
            self._expire()
            for item in self._pairings.values():
                if item['status'] == 'waiting' and hmac.compare_digest(item['code'], str(code).upper()):
                    item.update(status='awaiting_confirmation', identity=copy.deepcopy(identity))
                    if item.get('automatic'):
                        pairing_id = item['id']
                        entry = self.confirm_pairing(pairing_id, '60秒临时配对')
                        item.update(status='bound', code='', entry=entry)
                        self._pairings[pairing_id] = item
                    return True
            return False

    def confirm_pairing(self, pairing_id, label=''):
        """Local authenticated UI must confirm the displayed account identity."""
        with self._lock:
            self._expire()
            item = self._pairings.get(pairing_id)
            if not item or item['status'] != 'awaiting_confirmation':
                raise ValueError('配对请求已过期或尚未收到QQ私聊')
            identity = item['identity']
            entry = {key:identity[key] for key in ('platform','account_id','instance_id')}
            entry['label'] = str(label).strip()[:80]
            cfg = self.settings.get()
            entries = normalize_administrators([*cfg['access']['administrators'], entry])
            section = 'onebot' if identity['platform'] == 'onebot' else 'qq_official'
            users = list(dict.fromkeys([*cfg[section].get('allowed_users', []), identity['account_id']]))
            self.settings.update({'access':{'administrators':entries}, section:{'allowed_users':users}})
            del self._pairings[pairing_id]
            return entry

    def cancel_pairing(self, pairing_id):
        with self._lock:
            return self._pairings.pop(pairing_id, None) is not None


def manager_for(settings, chat=None):
    """Share runtime signing secret and pending bindings with both bridges."""
    manager = getattr(chat, 'access', None) if chat is not None else None
    if isinstance(manager, AccessManager):
        return manager
    manager = getattr(settings, '_access_manager', None)
    if not isinstance(manager, AccessManager):
        manager = AccessManager(settings)
        settings._access_manager = manager
    return manager
