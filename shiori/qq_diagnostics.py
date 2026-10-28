"""QQ ingress diagnostics contain stages/IDs, never message bodies or secrets."""
from __future__ import annotations

import time
from collections import OrderedDict
from datetime import datetime, timezone


REASONS = {
    'connector_disabled': 'QQ 连接器尚未启用，或当前选择的是另一种连接器',
    'paused': '机器人已暂停，请先取消暂停',
    'invalid_scope': '消息中的账号或会话标识无效，请检查适配器的 OneBot v11 消息上报格式',
    'invalid_sender': '消息缺少有效发送人账号，请检查适配器的消息上报格式',
    'self_message': '这是机器人账号自身发送的消息，不会再次回复自己',
    'unsupported_message': '当前消息类型不是群聊或私聊',
    'group_not_allowed': '当前群未加入允许聊天的群列表，请在 QQ 设置中添加群号或群 OpenID',
    'private_not_allowed': '当前私聊被接收规则拦截，请检查黑名单/白名单；聊天权限与管理员权限分别设置',
    'scope_disabled': '当前会话已停用，请在会话设置中启用',
    'duplicate_message': '这条平台消息已经处理，不会重复回复',
    'empty_message': '消息没有可读取的文字或图片',
    'ingress_busy': '消息处理数量已满，本轮无法进入回复流程',
    'accepted': '消息已通过聊天权限检查，正在进入回复流程',
    'planner_skip': 'Planner 选择本轮旁听；私聊可切换为每条回复，群聊可检查 @、名字与主动回复设置',
    'passive_only': '当前群未触发直接回复，或主动接话已关闭；请检查 @、名字、引用回复开关',
    'quiet': '当前会话处于安静模式或静默时段，本轮只观察',
    'cooldown': '当前群处于主动回复冷却或判断间隔内，直接 @ 仍按当前开关处理',
    'interrupted': '成员打断了上一请求，已停止原回复',
    'planner_error': '回复判断未完成，请检查模型配置、预算和运行日志',
    'reply_failed': '回复生成未完成，请检查模型配置、预算和运行日志',
    'reply_empty': '模型没有返回可发送的回复，请检查模型和运行日志',
    'send_failed': '回复已生成，但平台未确认送达；请检查适配器的 API 连接与消息发送权限',
    'sent': '平台已确认回复送达',
    'command': '消息已交给本地命令处理',
    'admin_pairing': '已处理管理员配对消息；60秒快速配对自动绑定，手动模式需在管理员页面确认',
}


def private_scope(store, scope_id):
    """Upgrade only the previously implicit default; explicit Planner choices stay."""
    scope = store.get_scope(scope_id)
    if scope.get('profile_id') is not None:
        return scope
    if (':private:' in scope_id and scope.get('enabled', True) and scope.get('always_reply') is False
            and scope.get('plan_enabled') is None and (scope.get('settings') or {}).get('plan_enabled') is None):
        scope = store.upsert_scope({'id': scope_id, 'always_reply': True, 'plan_enabled': False})
        store.add_event('qq_private_policy', '私聊默认改为每条回复；可在该会话设置中明确启用 Planner', scope_id=scope_id)
    return scope


class QQDiagnostics:
    def __init__(self, store, platform):
        self.store, self.platform = store, platform
        self.message_events = self.heartbeat_events = self.accepted_messages = self.filtered_messages = 0
        self.last_message_at = None
        self.last_result = None
        self._emitted = OrderedDict()

    def heartbeat(self):
        self.heartbeat_events += 1

    def message(self):
        self.message_events += 1
        self.last_message_at = datetime.now(timezone.utc).isoformat()

    def mark(self, code, *, scope_id='', user_id='', stage=None, emit=True):
        if code not in REASONS:
            raise ValueError('未知 QQ 诊断阶段')
        stage = stage or ('accepted' if code == 'accepted' else 'sent' if code == 'sent' else 'blocked')
        if code == 'accepted':
            self.accepted_messages += 1
        elif stage in ('blocked', 'skipped', 'failed'):
            self.filtered_messages += 1
        self.last_result = {'at': datetime.now(timezone.utc).isoformat(), 'stage': stage, 'code': code,
                            'reason': REASONS[code], 'scope_id': str(scope_id)[:240], 'user_id': str(user_id)[:180]}
        # Rejected strangers cannot fill the event log on every message. The
        # global stage cap also bounds floods using many different account IDs.
        if emit and code not in ('accepted', 'sent', 'self_message', 'duplicate_message'):
            now = time.monotonic()
            key = code
            previous = self._emitted.get(key, -float('inf'))
            if now - previous >= 30:
                self._emitted[key] = now
                self._emitted.move_to_end(key)
                while len(self._emitted) > 64:
                    self._emitted.popitem(last=False)
                self.store.add_event('qq_ingress', REASONS[code], platform=self.platform,
                    code=code, stage=stage, scope_id=str(scope_id)[:240], user_id=str(user_id)[:180])

    def status(self, *, connected):
        if connected and not self.message_events:
            diagnostic = ('已连接并收到心跳，但尚未收到聊天消息；请检查适配器是否开启消息上报，而非仅心跳或 API 连接。'
                          if self.heartbeat_events else '连接已建立，尚未收到聊天消息；请向机器人发送一条测试消息。')
        elif self.last_result:
            diagnostic = self.last_result['reason']
        else:
            diagnostic = '尚未收到聊天消息。'
        return {'message_events_received': self.message_events, 'heartbeat_events_received': self.heartbeat_events,
                'accepted_messages': self.accepted_messages, 'filtered_messages': self.filtered_messages,
                'last_message_at': self.last_message_at, 'last_message_result': dict(self.last_result) if self.last_result else None,
                'diagnostic': diagnostic}
