"""Checking a reply's source messages costs the same in a small chat and in a
group with tens of thousands of messages. These checks run on the event loop
several times per reply; when SQLite walked the whole chat for them, a busy
group froze every conversation for seconds at a time."""
import pytest

from shiori.db import Store, message_fingerprint

GROUP, OTHER = 'qq:group:1', 'qq:group:2'
FILLER = 6000


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path)
    yield value
    value.close()


def fill(store, scope_id, count):
    """Many older messages in one chat, written in one go."""
    with store._lock, store._conn:
        store._conn.execute(
            "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<?) "
            "INSERT INTO messages(id,scope_id,user_id,role,created_at,data) "
            "SELECT 'filler-'||?||'-'||i, ?, 'u'||(i%7), 'user', "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now','-'||i||' minutes'), '{}' FROM n",
            (count, scope_id, scope_id))


def steps(store, call):
    """SQLite virtual-machine work done by one call, in units of 20 instructions."""
    ticks = [0]

    def tick():
        ticks[0] += 1
        return 0
    store._conn.set_progress_handler(tick, 20)
    try:
        result = call()
    finally:
        store._conn.set_progress_handler(None, 20)
    return ticks[0], result


def lookups(store, scope_id, messages):
    ids = [m['id'] for m in messages]
    fingerprints = {m['id']: message_fingerprint(m) for m in messages}
    snapshot = store.message_source_snapshot(scope_id, fingerprints)
    assert snapshot is not None
    return {
        'snapshot': lambda: store.message_source_snapshot(scope_id, fingerprints),
        'snapshot_current': lambda: store.message_source_snapshot_current(scope_id, snapshot),
        'sources_current': lambda: store.message_sources_current(scope_id, ids),
        'ordered': lambda: store.ordered_messages(list(reversed(ids)), scope_id=scope_id),
    }


def test_source_checks_do_not_grow_with_the_chat(store):
    small = [store.add_message(GROUP, 'u1', 'user', f'消息 {n}') for n in range(14)]
    before = {name: steps(store, call) for name, call in lookups(store, GROUP, small).items()}
    fill(store, GROUP, FILLER)
    after = {name: steps(store, call) for name, call in lookups(store, GROUP, small).items()}
    for name in before:
        assert after[name][1] == before[name][1], name          # same answer
        # A walk over 6,000 rows would cost hundreds of ticks; a key lookup stays where it was.
        assert after[name][0] <= before[name][0] * 2 + 10, (name, before[name][0], after[name][0])
    assert [m['id'] for m in after['ordered'][1]] == [m['id'] for m in small]   # still in chat order


def test_a_message_of_another_chat_still_fails_the_check(store):
    fill(store, GROUP, 200)
    own = [store.add_message(GROUP, 'u1', 'user', f'消息 {n}') for n in range(3)]
    foreign = store.add_message(OTHER, 'u1', 'user', '别的群')
    mixed = own + [foreign]
    fingerprints = {m['id']: message_fingerprint(m) for m in mixed}
    assert store.message_source_snapshot(GROUP, fingerprints) is None
    assert not store.message_sources_current(GROUP, [m['id'] for m in mixed])
    assert [m['id'] for m in store.ordered_messages([m['id'] for m in mixed], scope_id=GROUP)] == [m['id'] for m in own]
    snapshot = store.message_source_snapshot(GROUP, {m['id']: message_fingerprint(m) for m in own})
    assert store.message_source_snapshot_current(GROUP, snapshot)
    assert not store.message_source_snapshot_current(GROUP, dict(snapshot, **{foreign['id']: 'x'}))
    with store._lock, store._conn:                        # an edited source fails closed
        store._conn.execute("UPDATE messages SET data=json_set(data,'$.content','改过') WHERE id=?", (own[0]['id'],))
    assert not store.message_source_snapshot_current(GROUP, snapshot)
