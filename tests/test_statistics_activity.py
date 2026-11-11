"""Statistics page: what the tokens were spent on, where a reply's time goes,
and how reply decisions ended. All of it is read from records already kept."""
import shutil
import subprocess
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from shiori.db import Store
from shiori.settings import DEFAULTS
from shiori.statistics import activity, snapshot

GROUP = 'qq:group:202'
ALL_TIME = ('2000-01-01T00:00:00Z', '2999-01-01T00:00:00Z')


class Settings:
    def get(self):
        return deepcopy(DEFAULTS)


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path)
    yield value
    value.close()


def test_usage_is_split_by_purpose_largest_first(store):
    for purpose, tokens, calls in (('planner', 4000, 3), ('chat', 8000, 1), ('jargon_learning', 6000, 1)):
        for _ in range(calls):
            store.record_usage({'purpose': purpose, 'model': 'same-model', 'input_tokens': tokens, 'output_tokens': 100, 'scope_id': GROUP})
    store.record_usage({'model': 'same-model', 'input_tokens': 10, 'output_tokens': 1})   # old rows without a purpose are chat
    purposes = activity(store, ALL_TIME)['purposes']
    assert [(item['purpose'], item['calls'], item['input_tokens']) for item in purposes] == [
        ('planner', 3, 12000), ('chat', 2, 8010), ('jargon_learning', 1, 6000)]
    assert purposes[0]['output_tokens'] == 300
    # The page gets it with everything else. Its window ends at "now" and leaves that instant out, and a coarse
    # clock can put rows written a moment ago at exactly that instant, so the window here ends a second later.
    later = datetime.now(timezone.utc) + timedelta(seconds=1)
    assert snapshot(store, Settings(), 1, now=later)['purposes'][0]['purpose'] == 'planner'


def test_reply_time_is_the_median_of_each_step(store):
    for total, flow, memory in ((12000, 9000, 1000), (14000, 10000, 1400), (20000, 11000, 3000)):
        store.add_event('qq_reply_timing', 'QQ 回复全程耗时', scope_id=GROUP, ingress_to_complete_ms=total, flow_ms=flow,
                        planner_ms=4, text_delivery_ms=2500,
                        chat_timings={'memory_recall_ms': memory, 'context_ms': 1000, 'reply_model_ms': 2600})
    store.add_event('qq_reply_timing', '旧版本的记录没有这些字段', scope_id=GROUP, flow_ms=8000)
    replies = activity(store, ALL_TIME)['replies']
    assert replies['count'] == 4
    assert replies['median_ms'] == {'total': 14000, 'wait': 4000, 'planner': 4, 'memory': 1400, 'context': 1000,
                                    'model': 2600, 'delivery': 2500}
    empty = activity(store, ('2999-01-01T00:00:00Z', '2999-01-02T00:00:00Z'))['replies']
    assert empty['count'] == 0 and empty['median_ms']['total'] is None


def test_reply_decisions_count_outcomes_the_prescreen_and_unwanted_replies(store):
    verdict = {'ok': True, 'mode': 'screen', 'screened': False, 'p_bot': 0.6, 'p_join': 0.7}
    for outcome, gate in (('reply', 'reply'), ('reply', 'addressed'), ('skip', 'below_threshold'), ('failed', None)):
        store.add_event('planner_timing', 'Planner 判断分阶段耗时', scope_id=GROUP, outcome=outcome,
                        **({'gate': {'result': gate, 'value': 80}} if gate else {}), prescreen=verdict)
    store.add_event('planner_timing', 'Planner 判断分阶段耗时', scope_id=GROUP, outcome='skip',
                    prescreen={'ok': False, 'mode': 'screen', 'error': 'timeout'})
    for _ in range(5):
        store.add_event('planner_prescreen', '快速预筛判断为不用接，未调用 Planner', scope_id=GROUP, outcome='skip',
                        prescreen=dict(verdict, screened=True))
    store.add_event('planner_feedback', '主动接话后被说没喊它', scope_id=GROUP, feedback='unwanted')
    decisions = activity(store, ALL_TIME)['decisions']
    assert decisions['planner_calls'] == 5
    assert decisions['outcomes'] == {'reply': 2, 'skip': 2, 'failed': 1, 'cancelled': 0}
    assert decisions['unprompted_replies'] == 2 and decisions['unwanted_replies'] == 1
    assert decisions['prescreen'] == {'mode': 'screen', 'judged': 10, 'screened': 5, 'failed': 1}


def test_shadow_verdicts_count_what_would_have_been_screened(store):
    for screened in (True, True, False):
        store.add_event('planner_timing', 'Planner 判断分阶段耗时', scope_id=GROUP, outcome='skip',
                        gate={'result': 'below_threshold'}, prescreen={'ok': True, 'mode': 'shadow', 'screened': screened})
    store.add_event('planner_timing', 'Planner 判断分阶段耗时', scope_id=GROUP, outcome='skip', gate={'result': 'below_threshold'})
    store.add_event('planner_prescreen', '快速预筛（影子）晚于 Planner 完成', scope_id=GROUP, planner_outcome='skip',
                    prescreen={'ok': True, 'mode': 'shadow', 'screened': True})
    decisions = activity(store, ALL_TIME)['decisions']
    assert decisions['planner_calls'] == 4
    assert decisions['prescreen'] == {'mode': 'shadow', 'judged': 4, 'screened': 3, 'failed': 0}
    assert activity(store, ('2999-01-01T00:00:00Z', '2999-01-02T00:00:00Z'))['decisions']['prescreen']['mode'] == 'off'


def test_the_page_script_renders_the_three_panels():
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
const html = panels({purposes:[{purpose:"planner",calls:3,input_tokens:7500,output_tokens:500},{purpose:"mystery",calls:1,input_tokens:2000,output_tokens:0}],
  replies:{count:3,median_ms:{total:12500,wait:3100,planner:4,memory:1200,context:1050,model:2500,delivery:2750}},
  decisions:{planner_calls:9,outcomes:{reply:2,skip:6,failed:1,cancelled:0},prescreen:{mode:"shadow",judged:9,screened:7,failed:0},unprompted_replies:2,unwanted_replies:1}});
for (const text of ["回复判断", "80%", "mystery", "12.5 秒", "等待合并消息", "3.1 秒", "2 / 6 / 1", "其中 7 条会被筛掉", "没喊你"])
  if (!html.includes(text)) { console.error("missing: " + text); process.exit(1); }
const empty = panels({});
if (!empty.includes("暂无模型用量记录") || !empty.includes("暂无 QQ 回复")) { console.error("empty state missing"); process.exit(1); }
'''
    result = subprocess.run([node, '-e', program, str(script)], capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
