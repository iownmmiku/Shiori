"""Disposable, incremental lexical candidates; never an authorization cache.

Store owns the lock. Text and permission checks still come from the main DB.
FTS5 is optional: an ordinary inverted table supplies the same bounded API.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
import math
import sqlite3
import time


LOGGER = logging.getLogger(__name__)
SCHEMA_VERSION = 2
BUILD_BATCH = 64
QUERY_TERMS = 64
BUILD_SECONDS = 0.04


def query_terms(text):
    # Imported on first search, after db and memory have finished importing.
    from .memory import tokenize
    terms = tokenize(str(text)[:12000])
    # Single CJK characters are noisy; retain them for single-character queries.
    words = [term for term in terms if len(term) > 1 or term.isascii()]
    return sorted(words or list(terms), key=lambda value: (-len(value), value))[:QUERY_TERMS]


@dataclass(frozen=True)
class Boundary:
    scope_id: str
    user_id: str
    origins: tuple[str, ...]
    public_group: bool
    now: str

    def sql(self, *, alias='', derived=False):
        prefix = alias + '.' if alias else ''
        prop = prefix + 'property' if derived else f"COALESCE(json_extract({prefix}data,'$.property'),'')"
        shared = prefix + 'shareable=1' if derived else f"json_extract({prefix}data,'$.shareable') IS 1"
        sql = f"""{prefix}status='active'
            AND {prefix}scope_id IN ({','.join('?' for _ in self.origins) or 'NULL'})
            AND {prop}!='memory_relation'
            AND ({prefix}visibility='group' OR
                ({prefix}visibility='private' AND {prefix}user_id=? AND ?=0))
            AND ({prefix}scope_id=? OR ({shared} AND {prefix}visibility='group'
                AND {prefix}kind IN ('fact','preference','event','note') AND {prop}!='conversation_digest'))
            AND ({prefix}valid_from IS NULL OR {prefix}valid_from='' OR {prefix}valid_from<=?)
            AND ({prefix}valid_to IS NULL OR {prefix}valid_to='' OR {prefix}valid_to>?)"""
        return sql, [*self.origins, self.user_id, int(self.public_group), self.scope_id, self.now, self.now]


class MemorySearchIndex:
    """One Store-local connection, serialized by Store._lock."""

    def __init__(self, path):
        self.path = path
        self.connection = None
        self.mode = 'uninitialized'
        self.warning = ''
        self.initialized = False
        self.pending_sync = None
        try:
            self._open()
        except sqlite3.DatabaseError:
            try:
                self.recreate()
            except Exception:
                self.close()
                raise

    def _create_fts(self):
        self.connection.execute('CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(tokens)')

    def _open(self):
        self.connection = sqlite3.connect(self.path, check_same_thread=False, timeout=5)
        self.connection.execute('PRAGMA busy_timeout=5000')
        if self.connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise sqlite3.DatabaseError('memory search integrity check failed')
        version = self.connection.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, SCHEMA_VERSION):
            raise sqlite3.DatabaseError('memory search schema changed')
        tables = {row[0] for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if version == SCHEMA_VERSION and not {'documents', 'postings', 'source_state', 'index_state'} <= tables:
            raise sqlite3.DatabaseError('memory search tables missing')
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS documents (
                row_id INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE,
                scope_id TEXT NOT NULL, user_id TEXT NOT NULL, kind TEXT NOT NULL,
                status TEXT NOT NULL, visibility TEXT NOT NULL, shareable INTEGER NOT NULL,
                property TEXT NOT NULL, valid_from TEXT, valid_to TEXT,
                pinned INTEGER NOT NULL, importance REAL NOT NULL, updated_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS search_scope_rank ON documents(
                scope_id,status,pinned DESC,importance DESC,updated_at DESC);
            CREATE TABLE IF NOT EXISTS postings (
                term TEXT NOT NULL,row_id INTEGER NOT NULL,PRIMARY KEY(term,row_id));
            CREATE INDEX IF NOT EXISTS postings_document ON postings(row_id);
            CREATE TABLE IF NOT EXISTS source_state (
                id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS index_state (
                key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        try:
            self._create_fts()
            self.mode = 'fts5'
        except sqlite3.OperationalError as exc:
            if 'no such module' not in str(exc).lower():
                raise
            self.mode = 'inverted'
            self.warning = 'FTS5不可用，记忆候选检索已使用普通倒排索引。'
            LOGGER.warning(self.warning)
        self.connection.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
        stored_mode = self.connection.execute("SELECT value FROM index_state WHERE key='mode'").fetchone()
        incomplete_fts = (self.mode == 'fts5' and
            self.connection.execute('SELECT COUNT(*) FROM memory_fts').fetchone()[0] !=
            self.connection.execute('SELECT COUNT(*) FROM documents').fetchone()[0])
        if stored_mode is None or stored_mode[0] != self.mode or incomplete_fts:
            # A cache built without FTS cannot reuse empty/new FTS tables later.
            self.connection.execute('DELETE FROM source_state')
            self.connection.execute('DELETE FROM documents')
            self.connection.execute('DELETE FROM postings')
            if self.mode == 'fts5':
                self.connection.execute('DELETE FROM memory_fts')
            self.connection.execute("INSERT OR REPLACE INTO index_state VALUES('mode',?)", (self.mode,))
        self.connection.commit()

    def recreate(self):
        self.close()
        for suffix in ('', '-wal', '-shm', '-journal'):
            self.path.with_name(self.path.name + suffix).unlink(missing_ok=True)
        LOGGER.warning('记忆候选索引损坏或不兼容，正在从主数据库重建。')
        self.initialized = False
        self._open()

    def close(self):
        self.pending_sync = None
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def _replace(self, record):
        identifier = record['id']
        old = self.connection.execute('SELECT row_id FROM documents WHERE id=?', (identifier,)).fetchone()
        if old:
            self._remove(identifier)
        if record.get('status') != 'active':
            return
        importance = record.get('importance', 0.5)
        if type(importance) not in (int, float) or not math.isfinite(importance) or not 0 <= importance <= 1:
            importance = 0.5
        cursor = self.connection.execute("""INSERT INTO documents(
            id,scope_id,user_id,kind,status,visibility,shareable,property,valid_from,valid_to,
            pinned,importance,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                identifier, record.get('scope_id', ''), record.get('user_id', ''), record.get('kind', ''),
                record.get('status', ''), record.get('visibility', ''), int(record.get('shareable') is True),
                str(record.get('property') or ''), record.get('valid_from'), record.get('valid_to'),
                int(record.get('pinned') is True), float(importance), str(record.get('updated_at') or '')))
        from .memory import tokenize
        text = '\n'.join(str(record.get(key) or '') for key in ('content', 'subject', 'property', 'title', 'filename'))
        terms = list(tokenize(text))
        if self.mode == 'fts5':
            self.connection.execute('INSERT INTO memory_fts(rowid,tokens) VALUES (?,?)',
                                    (cursor.lastrowid, ' '.join(terms)))
        else:
            self.connection.executemany('INSERT INTO postings(term,row_id) VALUES (?,?)',
                                        ((term, cursor.lastrowid) for term in terms))

    def _remove(self, identifier):
        row = self.connection.execute('SELECT row_id FROM documents WHERE id=?', (identifier,)).fetchone()
        if row:
            if self.mode == 'fts5':
                self.connection.execute('DELETE FROM memory_fts WHERE rowid=?', (row[0],))
            else:
                self.connection.execute('DELETE FROM postings WHERE row_id=?', (row[0],))
            self.connection.execute('DELETE FROM documents WHERE row_id=?', (row[0],))

    def start_sync(self, dirty=None):
        """Prepare a resumable pass. Store serializes each bounded step.

        A full pass records hashes for existing rows and uses a rowid cursor,
        never keeping a main-database cursor/transaction between batches.
        Store catches writes between batches and schedules a dirty catch-up or
        restarts validation after external edits, rollback or restore.
        """
        self.initialized = False
        if dirty is None:
            self.pending_sync = {'mode': 'full', 'rowid': 0,
                'remaining': dict(self.connection.execute('SELECT id,fingerprint FROM source_state'))}
        else:
            self.pending_sync = {'mode': 'dirty', 'ids': list(dirty)}

    def sync_step(self, source):
        """Process at most 64 records / roughly 40ms, then release Store lock."""
        pending = self.pending_sync
        if pending is None:
            return True
        started = time.monotonic()
        with self.connection:
            if pending['mode'] == 'full':
                rows = source.execute('SELECT rowid,id,data FROM memories WHERE rowid>? ORDER BY rowid LIMIT ?',
                                      (pending['rowid'], BUILD_BATCH)).fetchall()
                processed = 0
                for rowid, identifier, raw in rows:
                    fingerprint = hashlib.sha256(raw.encode('utf-8')).hexdigest()
                    if pending['remaining'].pop(identifier, None) != fingerprint:
                        self._replace(json.loads(raw))
                        self.connection.execute('INSERT OR REPLACE INTO source_state VALUES(?,?)', (identifier, fingerprint))
                    pending['rowid'] = rowid
                    processed += 1
                    if time.monotonic() - started >= BUILD_SECONDS:
                        break
                if processed == len(rows) and len(rows) < BUILD_BATCH:
                    self.pending_sync = {'mode': 'deleted', 'ids': list(pending['remaining'])}
            else:
                keys = pending['ids'][-BUILD_BATCH:]
                rows = {}
                if pending['mode'] == 'dirty' and keys:
                    rows = dict(source.execute('SELECT id,data FROM memories WHERE id IN (' + ','.join('?' for _ in keys) + ')', keys))
                for identifier in reversed(keys):
                    self._remove(identifier)
                    self.connection.execute('DELETE FROM source_state WHERE id=?', (identifier,))
                    if identifier in rows:
                        raw = rows[identifier]
                        self._replace(json.loads(raw))
                        self.connection.execute('INSERT INTO source_state VALUES(?,?)',
                            (identifier, hashlib.sha256(raw.encode('utf-8')).hexdigest()))
                    pending['ids'].pop()
                    if time.monotonic() - started >= BUILD_SECONDS:
                        break
                if not pending['ids']:
                    self.pending_sync = None
        return self.pending_sync is None

    def sync(self, source, dirty=None):
        """Small synchronous catch-up; large/cold passes run in Store's worker."""
        full = not self.initialized or dirty is None
        self.start_sync(None if full else dirty)
        while not self.sync_step(source):
            pass
        self.initialized = True

    def candidates(self, boundary, query, limit):
        where, params = boundary.sql(alias='d', derived=True)
        terms = query_terms(query)
        result = []
        # Reserve room for semantic/empty-query fallback without displacing a
        # strong lexical hit with a recent but unrelated record.
        supplements = min(24, max(2, limit // 8)) if terms else limit
        lexical_limit = max(1, limit - supplements)
        if terms:
            if self.mode == 'fts5':
                expression = ' OR '.join('"' + term.replace('"', '""') + '"' for term in terms)
                rows = self.connection.execute(f"""SELECT d.id FROM memory_fts
                    JOIN documents d ON d.row_id=memory_fts.rowid
                    WHERE memory_fts MATCH ? AND {where}
                    ORDER BY bm25(memory_fts),d.pinned DESC,d.importance DESC,d.updated_at DESC,d.id
                    LIMIT ?""", (expression, *params, lexical_limit))
            else:
                marks = ','.join('?' for _ in terms)
                rows = self.connection.execute(f"""SELECT d.id FROM postings p
                    JOIN documents d ON d.row_id=p.row_id WHERE p.term IN ({marks}) AND {where}
                    GROUP BY d.row_id ORDER BY COUNT(*) DESC,d.pinned DESC,d.importance DESC,d.updated_at DESC,d.id
                    LIMIT ?""", (*terms, *params, lexical_limit))
            result.extend(row[0] for row in rows)
        count = limit - len(result)
        if count:
            # Include both durable important facts and new context. Fetch at
            # most limit from either SQL projection, never decode all records.
            important = self.connection.execute(f"""SELECT d.id FROM documents d WHERE {where}
                ORDER BY d.pinned DESC,d.importance DESC,d.updated_at DESC,d.id LIMIT ?""", (*params, limit))
            recent = self.connection.execute(f"""SELECT d.id FROM documents d WHERE {where}
                ORDER BY d.updated_at DESC,d.id LIMIT ?""", (*params, limit))
            ranked = [row[0] for row in important]
            newest = [row[0] for row in recent]
            for position in range(max(len(ranked), len(newest))):
                for source in (ranked, newest):
                    if position < len(source) and source[position] not in result:
                        result.append(source[position])
                        if len(result) >= limit:
                            return result
        return result


def sql_fallback(source, boundary, query, limit):
    """Last-resort SQL scan when a disposable cache cannot be written.

    This is slower than the index, but returns bounded IDs and decodes no body
    in Python. Search terms are data, never LIKE wildcards or SQL expressions.
    """
    where, params = boundary.sql(alias='m')
    terms = query_terms(query)[:16]
    matches, values = [], []
    for term in terms:
        matches.append("(instr(lower(json_extract(m.data,'$.content')),?)>0 OR "
                       "instr(lower(COALESCE(json_extract(m.data,'$.subject'),'')),?)>0)")
        values.extend((term, term))
    result = []
    if matches:
        rows = source.execute(f"""SELECT m.id FROM memories m WHERE {where}
            AND ({' OR '.join(matches)}) ORDER BY m.updated_at DESC,m.id LIMIT ?""", (*params, *values, limit))
        result.extend(row[0] for row in rows)
    if len(result) >= limit:
        return result[:limit]
    rows = source.execute(f"""SELECT m.id FROM memories m WHERE {where}
        ORDER BY (json_extract(m.data,'$.pinned') IS 1) DESC,
        COALESCE(json_extract(m.data,'$.importance'),0.5) DESC,m.updated_at DESC,m.id LIMIT ?""", (*params, limit))
    for row in rows:
        if row[0] not in result:
            result.append(row[0])
            if len(result) >= limit:
                break
    return result
