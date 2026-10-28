"""Auditable reconciliation of unknown consumption; no automatic write-offs."""
import hashlib
import json
import math
import re

from .db import utc_now


def breakdown(store, start, end):
    groups = {key: {'calls': 0, 'tokens': 0, 'cost': 0.0} for key in ('confirmed', 'estimated', 'unconfirmed')}
    with store._lock:
        rows = store._conn.execute("""SELECT CASE WHEN json_extract(data,'$.unconfirmed')=1 THEN 'unconfirmed'
            WHEN json_extract(data,'$.estimated')=1 THEN 'estimated' ELSE 'confirmed' END,
            COUNT(*),COALESCE(SUM(input_tokens+output_tokens),0),COALESCE(SUM(cost),0)
            FROM usage WHERE created_at>=? AND created_at<? GROUP BY 1""", (start, end)).fetchall()
    for key, calls, tokens, cost in rows:
        groups[key] = {'calls': int(calls), 'tokens': int(tokens), 'cost': float(cost)}
    return groups


def reconcile(store, usage_id, payload):
    if not isinstance(payload, dict):
        raise ValueError('对账内容须为对象')
    identifier = payload.get('reconciliation_id', '')
    if not isinstance(identifier, str) or not re.fullmatch(r'[a-fA-F0-9-]{16,64}', identifier):
        raise ValueError('请提供本次对账的独立标识')
    action = payload.get('action')
    if action not in ('actual', 'not_billed'):
        raise ValueError('请选择核实实际用量或确认未计费')
    note = payload.get('note')
    if not isinstance(note, str) or not 1 <= len(note.strip()) <= 1000:
        raise ValueError('请填写核对依据，最多1000字；不要填写密钥')
    values = {'input_tokens': 0, 'output_tokens': 0, 'cost': 0.0}
    if action == 'actual':
        for key in ('input_tokens', 'output_tokens'):
            value = payload.get(key)
            if type(value) is not int or not 0 <= value <= 100000000:
                raise ValueError('实际Token须为0—100000000的整数')
            values[key] = value
        cost = payload.get('cost')
        if type(cost) not in (int, float) or not math.isfinite(cost) or not 0 <= cost <= 1000000:
            raise ValueError('实际费用须为0—1000000的有限数值')
        values['cost'] = float(cost)
    fingerprint = hashlib.sha256(json.dumps([action, values, note.strip()], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    with store._lock, store._conn:
        original = store._get('usage', usage_id)
        previous = original.get('reconciliation') or {}
        if previous.get('id') == identifier:
            if previous.get('fingerprint') != fingerprint:
                raise ValueError('同一对账标识不能用于不同修改')
            return original
        if not original.get('unconfirmed'):
            raise ValueError('该记录已核实或不属于待核实消费，请刷新列表')
        record = dict(original, **values, estimated=False, unconfirmed=False, cost_known=True,
                      original_estimate={key: original.get(key) for key in ('input_tokens', 'output_tokens', 'cost', 'cost_known', 'estimated', 'unconfirmed')},
                      reconciliation={'id': identifier, 'fingerprint': fingerprint, 'action': action,
                                      'note': note.strip(), 'at': utc_now(), 'actor': 'local_admin'})
        store._write('usage', record, ('created_at', 'input_tokens', 'output_tokens', 'cost'))
        store.add_event('usage_reconciled', '管理员核实了未知模型消费', usage_id=usage_id,
                        reconciliation_id=identifier, action=action)
        return record
