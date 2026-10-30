"""Bounded, canonical evidence reads for the authenticated management UI."""
import json


def evidence_page(store, jargon_id, *, offset=0, limit=50):
    if type(offset) is not int or offset < 0 or offset > 100000 or type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError('来源分页参数无效')
    with store._lock:
        item = store.get_jargon(jargon_id)
        evidence = item.get('evidence') or {}
        sources = item.get('source_ids')
        values = list(sources) if isinstance(sources, list) else []
        if isinstance(evidence, dict):
            if isinstance(evidence.get('message_ids'), list):
                values.extend(evidence['message_ids'])
        elif isinstance(evidence, list):
            values.extend(entry.get('message_id') for entry in evidence if isinstance(entry, dict))
        ids = list(dict.fromkeys(value for value in values if isinstance(value, str) and value))
        selected = ids[offset:offset + limit]
        rows = store._conn.execute('SELECT id,scope_id,data FROM messages WHERE id IN (SELECT value FROM json_each(?))',
                                   (json.dumps(selected),)).fetchall() if selected else []
        found = {row[0]: row for row in rows}
        result = []
        for key in selected:
            row = found.get(key)
            if row is None:
                result.append({'id': key, 'status': 'missing'})
            elif row[1] != item['scope_id']:
                result.append({'id': key, 'status': 'unavailable'})
            else:
                source = json.loads(row[2])
                result.append({'id': key, 'status': 'available', 'message': {
                    field: source.get(field) for field in ('id', 'user_id', 'content', 'created_at')}})
    return {'items': result, 'total': len(ids), 'offset': offset,
            'next_offset': offset + limit, 'has_more': offset + limit < len(ids)}
