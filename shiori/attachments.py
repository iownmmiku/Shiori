"""Owned document references, bounded uploads and conservative reclamation.

Removing a selection releases storage when it is safe. Explicit deletion remains
a revocation: a model send or a computer action must fail its source guard.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
import threading

from .runs import TERMINAL

MAX_REFERENCE_CHARS = 80000
MAX_REFERENCES = 5
TEMPORARY_LIMIT = 200
REFERENCE_LIFETIME = timedelta(hours=24)


async def bounded_parse(function, *args):
    """Keep the caller's parse slot until the real worker exits on cancellation."""
    worker = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not worker.cancelled():
            worker.exception()
        raise


class AttachmentLibrary:
    def __init__(self, data_dir, store, runs):
        self.root = Path(data_dir) / 'chat_attachments'
        self.imports = Path(data_dir) / 'document_imports'
        self.store, self.runs = store, runs
        self._lock = threading.RLock()
        self._active = Counter()
        self._request = ContextVar('attachment_request', default=None)

    @staticmethod
    def _now():
        return datetime.now(timezone.utc)

    @staticmethod
    def _id(value):
        if isinstance(value, dict):
            value = value.get('id')
        if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{32}', value):
            raise ValueError('附件标识无效')
        return value

    @staticmethod
    def _write(path, record):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.' + secrets.token_hex(6) + '.tmp')
        try:
            temporary.write_text(json.dumps(record, ensure_ascii=False), 'utf-8')
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _load(self, scope_id, value):
        ident = self._id(value)
        path = self.root / (ident + '.json')
        if not path.exists():
            path = self.imports / (ident + '.json')
        try:
            record = json.loads(path.read_text('utf-8'))
        except FileNotFoundError as exc:
            raise ValueError('附件已不存在，请重新上传') from exc
        except (ValueError, OSError) as exc:
            raise ValueError('附件记录无法读取，请在资料管理中核查') from exc
        if not isinstance(record, dict):
            raise ValueError('附件记录无法读取，请在资料管理中核查')
        if record.get('scope_id') != scope_id or record.get('user_id') != 'owner':
            raise PermissionError('附件不属于当前聊天')
        if record.get('id') != ident:
            raise ValueError('附件记录标识不一致')
        return path, record

    def _job_references(self):
        # The durable payload survives a refresh; only active jobs retain a
        # reference. Old interrupted computer work is never automatically run.
        refs = {}
        with self.runs._lock:
            for job in list(self.runs._jobs.values()):
                if job.get('status') in TERMINAL:
                    continue
                for item in job.get('_payload', {}).get('attachments') or []:
                    if isinstance(item, dict) and isinstance(item.get('id'), str):
                        refs.setdefault(item['id'], []).append(job['id'])
        return refs

    def _in_use(self, ident, job_refs=None):
        return bool(self._active[ident] or ident in (job_refs if job_refs is not None else self._job_references()))

    @contextmanager
    def request(self):
        held = set()
        token = self._request.set(held)
        try:
            yield
        finally:
            self._request.reset(token)
            with self._lock:
                for ident in held:
                    self._active[ident] -= 1
                    if not self._active[ident]:
                        del self._active[ident]
                self._collect_ids(held)

    def resolve(self, scope_id, ids, *, acquire=True):
        if not isinstance(ids, list) or len(ids) > MAX_REFERENCES:
            raise ValueError('一次最多引用5个附件')
        with self._lock:
            records = [self._load(scope_id, value)[1] for value in ids]
            if any(record.get('purpose') == 'memory' for record in records):
                raise ValueError('长期资料通过记忆检索使用，请重新上传为本次参考')
            if sum(len(record.get('text', '')) for record in records) > MAX_REFERENCE_CHARS:
                raise ValueError('本次参考内容最多80000字符，请分次选择附件')
            held = self._request.get() if acquire else None
            if held is not None:
                for record in records:
                    if record['id'] not in held:
                        held.add(record['id'])
                        self._active[record['id']] += 1
            return records

    def current(self, scope_id, records):
        try:
            current = self.resolve(scope_id, [record['id'] for record in records], acquire=False)
            return all(left['sha256'] == right['sha256'] and left['text'] == right['text']
                       for left, right in zip(records, current))
        except (ValueError, KeyError, PermissionError, OSError):
            return False

    def create(self, scope_id, name, raw, parsed, purpose):
        if purpose not in ('context', 'memory'):
            raise ValueError('附件用途无效')
        text = parsed['text']
        if purpose == 'context' and len(text) > MAX_REFERENCE_CHARS:
            raise ValueError('本次参考附件最多80000字符；请拆分文件，或选择保存为长期资料')
        with self._lock:
            self.collect()
            if purpose == 'context' and len(list(self.root.glob('*.json'))) >= TEMPORARY_LIMIT:
                raise ValueError('临时参考附件已达到200个，请打开资料管理清理；长期资料不占此配额')
            ident, created = secrets.token_hex(16), self._now()
            record = {'id': ident, 'name': name, 'scope_id': scope_id, 'user_id': 'owner',
                      'purpose': purpose, 'sha256': hashlib.sha256(raw).hexdigest(),
                      'created_at': created.isoformat(), 'characters': len(text), 'bytes': len(raw)}
            if purpose == 'memory':
                # Store the document body once. The small receipt is pruned after a
                # note is deleted without ever touching other knowledge.
                memory = self.store.create_memory({'scope_id': scope_id, 'user_id': 'owner', 'kind': 'note',
                    'content': text, 'subject': name, 'visibility': 'private', 'filename': name,
                    'file_hash': record['sha256'], 'attachment_id': ident, 'pinned': True})
                record['memory_id'] = memory['id']
                try:
                    self._write(self.imports / (ident + '.json'), record)
                except BaseException:
                    self.store.delete_memory(memory['id'])
                    raise
                return dict(record, preview=text[:2000], memory=memory)
            record.update(text=text, state='pending', expires_at=(created + REFERENCE_LIFETIME).isoformat())
            self._write(self.root / (ident + '.json'), record)
            return {**{key: value for key, value in record.items() if key != 'text'}, 'preview': text[:2000]}

    def _eligible(self, record, job_refs):
        if record.get('purpose') != 'context' or self._in_use(record['id'], job_refs):
            return False
        return record.get('state') == 'released' or bool(record.get('expires_at') and record['expires_at'] <= self._now().isoformat())

    def _collect_ids(self, ids):
        refs = self._job_references()
        deleted = []
        for ident in ids:
            path = self.root / (ident + '.json')
            try:
                record = json.loads(path.read_text('utf-8'))
                if self._eligible(record, refs):
                    path.unlink()
                    deleted.append(ident)
            except (OSError, ValueError, KeyError, TypeError):
                # Unknown or damaged legacy material is never silently erased.
                continue
        return deleted

    def collect(self):
        with self._lock:
            removed = self._collect_ids(path.stem for path in self.root.glob('*.json'))
            for path in self.imports.glob('*.json'):
                try:
                    record = json.loads(path.read_text('utf-8'))
                    if record.get('purpose') != 'memory' or not record.get('memory_id'):
                        continue
                    with self.store._lock:
                        memory = self.store._conn.execute('SELECT status FROM memories WHERE id=?',
                                                          (record['memory_id'],)).fetchone()
                    if memory is None or memory[0] == 'deleted':
                        path.unlink()
                except (ValueError, OSError, TypeError):
                    continue
            return removed

    def release(self, scope_id, ids):
        if not isinstance(ids, list) or len(ids) > TEMPORARY_LIMIT:
            raise ValueError('请选择最多200份资料')
        deleted, retained = [], []
        with self._lock:
            for value in ids:
                ident = self._id(value)
                try:
                    path, record = self._load(scope_id, ident)
                except ValueError as exc:
                    if not (self.root / (ident + '.json')).exists() and not (self.imports / (ident + '.json')).exists():
                        continue  # Safe to repeat after request completion/GC.
                    raise exc
                if record.get('purpose') != 'context':
                    retained.append(ident)
                    continue
                record['state'] = 'released'
                self._write(path, record)
                if self._collect_ids([ident]):
                    deleted.append(ident)
                else:
                    retained.append(ident)
        return {'ok': True, 'deleted': deleted, 'retained': retained}

    def delete(self, scope_id, ident):
        with self._lock:
            path, _ = self._load(scope_id, ident)
            path.unlink()  # Deliberate revocation, including an active request.
        return {'ok': True}

    def preview(self, scope_id, ident):
        with self._lock:
            _, record = self._load(scope_id, ident)
            return {'id': record['id'], 'name': record.get('name', ''),
                    'purpose': record.get('purpose', 'legacy_unknown'),
                    'preview': str(record.get('text', ''))[:2000], 'memory_id': record.get('memory_id'),
                    'in_use': self._in_use(record['id'])}

    def list(self, scope_id, *, offset=0, limit=50):
        offset, limit = max(0, offset), max(1, min(limit, 100))
        with self._lock:
            self.collect()
            refs, items = self._job_references(), []
            for root in (self.root, self.imports):
                for path in root.glob('*.json'):
                    try:
                        _, record = self._load(scope_id, path.stem)
                    except (ValueError, OSError, PermissionError):
                        continue
                    public = {key: value for key, value in record.items() if key != 'text'}
                    public.update(purpose=record.get('purpose', 'legacy_unknown'),
                                  characters=record.get('characters', len(record.get('text', ''))),
                                  stored_bytes=path.stat().st_size, in_use=self._in_use(record['id'], refs),
                                  job_ids=refs.get(record['id'], []))
                    items.append(public)
            items.sort(key=lambda item: item.get('created_at', ''), reverse=True)
            return {'items': items[offset:offset + limit], 'total': len(items), 'offset': offset, 'limit': limit,
                    'temporary_count': len(list(self.root.glob('*.json'))), 'temporary_limit': TEMPORARY_LIMIT}

    def cleanup(self, scope_id, *, preview=True):
        with self._lock:
            refs, eligible = self._job_references(), []
            for path in self.root.glob('*.json'):
                try:
                    _, record = self._load(scope_id, path.stem)
                    if self._eligible(record, refs):
                        eligible.append(record['id'])
                except (OSError, ValueError, PermissionError):
                    continue
            deleted = [] if preview else self._collect_ids(eligible)
            return {'preview': preview, 'eligible_ids': eligible, 'deleted': deleted,
                    'message': '只回收已释放或过期且未被使用的临时参考；旧版用途未知资料须预览后手动删除。'}
