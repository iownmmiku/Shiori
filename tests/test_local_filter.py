"""Local pre-filter (social.local_filter_kind): unprompted group batches that hold
nothing the Planner could weigh are skipped without a model call, but never a
question, never a member who is talking with the bot, and never something the
Planner can actually see (pictures, once image captions describe them)."""
import json
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

from shiori.db import Store
from shiori.qq_diagnostics import REASONS
from shiori.runtime_logs import summary
from shiori.settings import DEFAULTS, Settings as SettingsStore
from shiori.social import SocialEngine, local_filter_kind
from shiori.statistics import activity

GROUP = 'qq:group:202'
ALL_TIME = ('2000-01-01T00:00:00Z', '2999-01-01T00:00:00Z')
PICTURE = ['https://example.test/a.png']


@pytest.fixture
def anyio_backend():
    return 'asyncio'


class Settings:
    def __init__(self, mode='on', **planner):
        self.config = deepcopy(DEFAULTS)
        self.config['model'].update(base_url='https://test.invalid/v1', model='mock')
        self.config['onebot'].update(enabled=True, chat_policy='blacklist')
        self.config['planner'].update({'min_interval_ms': 0, 'local_filter': mode, **planner})
        self.config['time']['awareness_enabled'] = False

    def get(self):
        return deepcopy(self.config)


class Planner:
    def __init__(self):
        self.calls = []

    async def complete(self, messages, **kwargs):
        self.calls.append(messages)
        payload = json.loads(messages[-1]['content'])
        return {'content': json.dumps({'action': 'skip', 'target_message_id': payload['current_message_id'],
                                       'target_user_id': payload['current_user_id'], 'addressed_to_bot': False,
                                       'audience': 'group', 'intent': '', 'reason': '成员之间在聊', 'needed_info': [],
                                       'value': 10}, ensure_ascii=False)}


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path)
    value.upsert_scope({'id': GROUP, 'cooldown_seconds': 0, 'merge_window_ms': 0})
    yield value
    value.close()


def say(store, user, text, name='阿九', **metadata):
    return store.add_message(GROUP, user, 'user', text, sender_name=name, **metadata)


def events(store, kind):
    return [item['details'] for item in store.list_events(50) if item['kind'] == kind]


def item(text, **metadata):
    return {'content': text, 'metadata': metadata}


def test_what_counts_as_nothing_to_judge():
    assert local_filter_kind([item('[图片]', images=PICTURE)]) == 'picture'
    assert local_filter_kind([item('[猫想要]')]) == 'picture'          # a named sticker
    assert local_filter_kind([item('')]) == 'picture'                   # a QQ face alone leaves no text
    for text in ('🤔', '666', '2=2', '...', '-1'):
        assert local_filter_kind([item(text)]) == 'symbols', text
    for text in ('哈哈哈哈', 'hhhh', 'www', '好的。', '笑死', '确实', '草草', '嗯嗯！', 'OK'):
        assert local_filter_kind([item(text)]) == 'reaction', text
    # Questions, ordinary words and anything longer always go to the Planner.
    for text in ('？', '?', '[图片]？', '好的？', '这是谁', '漏了', '宝宝', '不是', '有无卫', '今天打本吗'):
        assert local_filter_kind([item(text)]) is None, text
    # A batch is skipped only when every message in it is.
    assert local_filter_kind([item('[图片]', images=PICTURE), item('哈哈')]) == 'reaction'
    assert local_filter_kind([item('哈哈'), item('这个怎么弄')]) is None
    assert local_filter_kind([]) is None
    # With image captions on, the Planner can see what a picture shows.
    assert local_filter_kind([item('[图片]', images=PICTURE)], pictures_described=True) is None
    assert local_filter_kind([item('哈哈')], pictures_described=True) == 'reaction'


def test_the_setting_defaults_to_on_and_is_validated(tmp_path):
    settings = SettingsStore(tmp_path)
    assert settings.get()['planner']['local_filter'] == 'on'
    assert settings.update({'planner': {'local_filter': 'shadow'}})['planner']['local_filter'] == 'shadow'
    for bad in ('screen', '', None, True):
        with pytest.raises(ValueError):
            settings.update({'planner': {'local_filter': bad}})


@pytest.mark.anyio
async def test_on_skips_without_a_model_call_and_does_not_hold_up_the_next_message(store):
    planner = Planner()
    # A long interval between Planner calls: a skipped picture must not use it up.
    social = SocialEngine(store, Settings('on', min_interval_ms=60000), planner)
    result = await social.plan(store.get_scope(GROUP), say(store, '303', '[图片]', images=PICTURE))
    assert not result['reply'] and '本地预筛' in result['reason'] and not planner.calls
    logged = events(store, 'planner_local_skip')
    assert len(logged) == 1 and logged[0]['local_filter'] == {'mode': 'on', 'kind': 'picture'}
    assert logged[0]['source_ids'] and summary('planner_local_skip', logged[0]) == '未调用 Planner · 本地预筛：只有图片或表情'
    await social.plan(store.get_scope(GROUP), say(store, '303', '今天有人打本吗'))
    assert len(planner.calls) == 1
    await social.close()


@pytest.mark.anyio
async def test_questions_words_and_members_talking_with_the_bot_still_reach_the_planner(store):
    planner = Planner()
    social = SocialEngine(store, Settings('on'), planner)
    for text in ('？', '漏了'):
        await social.plan(store.get_scope(GROUP), say(store, '303', text))
    assert len(planner.calls) == 2
    # A member who @'d the bot a moment ago is talking with it: their "哈哈" may be an answer.
    say(store, '505', '你看过这个没', name='小林', at_bot=True)
    await social.plan(store.get_scope(GROUP), say(store, '505', '哈哈', name='小林'))
    assert len(planner.calls) == 3
    # So is a member the bot just answered.
    social._participants[(GROUP, '606')] = __import__('time').monotonic()
    await social.plan(store.get_scope(GROUP), say(store, '606', '[图片]', name='阿青', images=PICTURE))
    assert len(planner.calls) == 4 and not events(store, 'planner_local_skip')
    await social.close()


@pytest.mark.anyio
async def test_an_at_with_a_reaction_is_never_filtered(store):
    planner = Planner()
    social = SocialEngine(store, Settings('on'), planner)
    result = await social.plan(store.get_scope(GROUP), say(store, '303', '哈哈', at_bot=True), direct=True)
    assert result['reply'] and not events(store, 'planner_local_skip')
    await social.close()


@pytest.mark.anyio
async def test_a_merged_batch_is_judged_message_by_message(store):
    planner = Planner()
    social = SocialEngine(store, Settings('on'), planner)

    def batch(*lines):
        stored = [say(store, user, text, **meta) for user, text, meta in lines]
        labelled = '\n'.join(f"[说话人 {m['user_id']}] {m['content']}" for m in stored)
        return dict(stored[-1], content=labelled,
                    metadata=dict(stored[-1]['metadata'], merged_message_ids=[m['id'] for m in stored]))

    # The speaker labels of a merged prompt are not the members' text.
    result = await social.plan(store.get_scope(GROUP), batch(('303', '[图片]', {'images': PICTURE}), ('404', '哈哈哈', {})))
    assert '本地预筛' in result['reason'] and not planner.calls
    assert events(store, 'planner_local_skip')[0]['local_filter']['kind'] == 'reaction'
    await social.plan(store.get_scope(GROUP), batch(('303', '哈哈', {}), ('404', '这个副本怎么打', {})))
    assert len(planner.calls) == 1
    await social.close()


@pytest.mark.anyio
async def test_pictures_go_to_the_planner_while_captions_describe_them(store):
    planner = Planner()
    settings = Settings('on')
    settings.config['image_captions']['enabled'] = True
    social = SocialEngine(store, settings, planner)
    await social.plan(store.get_scope(GROUP), say(store, '303', '[图片]', images=PICTURE))
    assert len(planner.calls) == 1
    await social.plan(store.get_scope(GROUP), say(store, '303', '笑死'))
    assert len(planner.calls) == 1 and len(events(store, 'planner_local_skip')) == 1
    await social.close()


@pytest.mark.anyio
async def test_shadow_only_marks_the_planners_record_and_off_does_nothing(store):
    planner = Planner()
    social = SocialEngine(store, Settings('shadow'), planner)
    await social.plan(store.get_scope(GROUP), say(store, '303', '[图片]', images=PICTURE))
    await social.plan(store.get_scope(GROUP), say(store, '303', '今天有人打本吗'))
    assert len(planner.calls) == 2 and not events(store, 'planner_local_skip')
    marked, plain = events(store, 'planner_timing')[1], events(store, 'planner_timing')[0]
    assert marked['local_filter'] == {'mode': 'shadow', 'kind': 'picture'} and 'local_filter' not in plain
    assert '本地预筛（影子）：会跳过，只有图片或表情' in summary('planner_timing', marked)
    await social.close()

    social = SocialEngine(store, Settings('off'), planner)
    await social.plan(store.get_scope(GROUP), say(store, '303', '哈哈'))
    assert len(planner.calls) == 3 and 'local_filter' not in events(store, 'planner_timing')[0]
    await social.close()


def test_statistics_count_skips_and_shadow_verdicts(store):
    for kind in ('picture', 'reaction', 'picture'):
        store.add_event('planner_local_skip', '本地预筛', scope_id=GROUP, outcome='skip', local_filter={'mode': 'on', 'kind': kind})
    for verdict in ({'mode': 'shadow', 'kind': 'symbols'}, {'mode': 'shadow', 'kind': 'picture'}, None):
        store.add_event('planner_timing', 'Planner 判断分阶段耗时', scope_id=GROUP, outcome='skip',
                        gate={'result': 'below_threshold'}, **({'local_filter': verdict} if verdict else {}))
    decisions = activity(store, ALL_TIME)['decisions']
    assert decisions['planner_calls'] == 3                              # skipped batches never reached the Planner
    assert decisions['outcomes']['skip'] == 3
    assert decisions['local_filter'] == {'mode': 'on', 'skipped': 3, 'would_skip': 2}
    assert activity(store, ('2999-01-01T00:00:00Z', '2999-01-02T00:00:00Z'))['decisions']['local_filter']['mode'] == 'off'
    assert 'local_filter' in REASONS and '本地预筛' in REASONS['local_filter']


def test_the_statistics_page_shows_the_local_filter_line():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node unavailable')
    script = Path(__file__).resolve().parent.parent / 'shiori' / 'static' / 'statistics.js'
    program = r'''
const fs = require("fs"), file = process.argv[process.argv.length - 1];
const e = value => String(value ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const source = fs.readFileSync(file, "utf8");
const body = source.slice(source.indexOf("function activityPanels"), source.indexOf("function markup"));
const purposeNames = source.slice(source.indexOf("const purposeNames"), source.indexOf("function activityPanels"));
const count = value => Number(value || 0).toLocaleString();
const panels = new Function("e", "count", purposeNames + body + "; return activityPanels;")(e, count);
const on = panels({decisions:{planner_calls:9,outcomes:{reply:0,skip:9,failed:0},local_filter:{mode:"on",skipped:4,would_skip:0}}});
const shadow = panels({decisions:{planner_calls:9,outcomes:{reply:0,skip:9,failed:0},local_filter:{mode:"shadow",skipped:0,would_skip:5}}});
const off = panels({decisions:{planner_calls:9,outcomes:{},local_filter:{mode:"off"}}});
if (!on.includes("本地预筛跳过 4 条")) { console.error("on line missing"); process.exit(1); }
if (!shadow.includes("本地预筛（影子）：5 条会被跳过")) { console.error("shadow line missing"); process.exit(1); }
if (off.includes("本地预筛")) { console.error("off should show nothing"); process.exit(1); }
'''
    result = subprocess.run([node, '-e', program, str(script)], capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
