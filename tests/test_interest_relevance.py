"""Authored pages and local persistence only; no live searches or model calls."""
import copy
import hashlib
import json

import pytest

from mikubot.db import Store
from mikubot.knowledge import PersonaKnowledge, _interest_focuses, assess_topic_relevance


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.parametrize('interest,title,body', [
    ('关东煮与薄荷甜点', '冬日关东煮做法', '关东煮可以加入萝卜和鱼丸。汤底煮好后放入食材。小火慢煮更容易入味。关东煮适合温热食用。'),
    ('关东煮与薄荷甜点', '薄荷甜点的清凉风味', '薄荷甜点可以搭配巧克力。薄荷的风味较为清爽。甜点中可以加入少量薄荷叶。适当降低糖量会让香气更清晰。'),
    ('轻音社与低音贝斯', '低音贝斯的入门练习', '低音贝斯承担节奏与和声的连接。练习低音贝斯时可以先从慢速节拍开始。右手的稳定拨弦能帮助保持乐句清晰。'),
    ('东京文京区与放学校道', '东京文京区的街道观察', '东京文京区有许多适合散步的街道。东京文京区保留了多样的街区景观。步行参观时应当遵守交通信号，并注意当地居民的生活。'),
    ('便利店新口味饭团', '便利店推出新口味饭团', '便利店推出新口味饭团，尝试加入烤鱼与蔬菜。新口味饭团的包装标注了主要配料。购买便利店饭团前可以先查看过敏原提示。'),
    ('冰美式咖啡与常松的鞋带', '冰美式咖啡的调配方法', '冰美式咖啡可以使用浓缩咖啡与冰水调配。调节水量会改变冰美式咖啡的浓度。豆子的烘焙程度也会影响苦味与香气。'),
    ('女子高中生日常与碎碎念', '女子高中生的日常安排', '女子高中生的日常包括上课与课外活动。放学后可以参加社团。合理安排休息有助于保持精力。女子高中生的日常也需要为朋友相处留出时间。'),
])
def test_coordinated_interests_admit_each_substantive_subject(interest, title, body):
    result = assess_topic_relevance(interest, title, body)
    assert result['relevant'], result
    assert result['main_text'] and result['matched_focus'] in _interest_focuses(interest)


@pytest.mark.parametrize('value,expected', [
    ('千与千寻', ['千与千寻']), ('和平精英', ['和平精英']),
    ('《爱丽丝与仙境旅行》', ['《爱丽丝与仙境旅行》']),
    ('《爱丽丝与仙境旅行》与音乐', ['《爱丽丝与仙境旅行》', '音乐']),
    ('关东煮与薄荷甜点以及冰美式咖啡', ['关东煮', '薄荷甜点', '冰美式咖啡']),
    ('music and anime', ['music', 'anime']),
    ('日本音乐制作', ['日本音乐制作']), ('Japanese anime', ['Japanese anime']),
])
def test_subject_split_preserves_titles_names_and_qualifiers(value, expected):
    assert _interest_focuses(value) == expected


@pytest.mark.parametrize('interest,title,body', [
    ('日本音乐制作', '音乐制作的录音流程', '音乐制作团队公开了录音流程。制作音乐需要安排乐器与声音混合。音乐制作过程有许多不同的工作环节。'),
    ('Japanese anime', 'Anime updates', 'Anime news covers new animation releases. Anime audiences discuss animation and enjoy anime interviews from various countries.'),
    ('常松的鞋带', '常松的音乐创作', '常松的音乐分享了很多创作想法。常松的作品风格丰富。音乐制作环节需要协调演奏与录音。常松的音乐计划仍在推进。'),
    ('关东煮与薄荷甜点', '今日财经新闻', '金融市场关注新的季度报告。投资者讨论利率与企业盈利。这里的报道聚焦经济走势，并没有讨论料理或甜点。'),
    ('音乐', '音乐资讯导航', '首页 | 音乐 | 登录 | 搜索\n歌曲 专辑 演唱 乐队\n音乐 新闻 推荐 阅读\n注册 音乐 帮助 中心'),
    ('音乐', '今日财经新闻', '金融市场公布新的季度报告。相关企业正在讨论下一季度的经营计划。关注财报可以帮助理解市场变化。\n相关推荐\n音乐制作团队公布音乐计划。音乐制作涉及录音与编曲。'),
    ('音乐', '音乐会员账号密码', '音乐会员免费账号密码分享。账号：fixture-only 密码：fake-value。音乐账号可以共享。音乐服务登录教程包含账号列表。'),
])
def test_qualifiers_navigation_recommendation_tails_and_credentials_stay_rejected(interest, title, body):
    assert not assess_topic_relevance(interest, title, body)['relevant']


def test_substantive_body_survives_short_paragraphs_but_not_title_alone():
    body = '\n'.join(['关东煮适合小火慢煮。', '萝卜可以提前焯水。', '鱼丸需要充分加热。',
                      '汤底加入适量昆布。', '关东煮可以温热食用。'])
    result = assess_topic_relevance('关东煮与薄荷甜点', '关东煮的家庭做法', body)
    assert result['relevant'] and len(result['main_text']) >= 40
    assert not assess_topic_relevance('关东煮', '关东煮的家庭做法', '首页\n登录\n注册\n菜单')['relevant']


@pytest.mark.parametrize('body', [
    'Japanese anime studios share their new work with fans every week',
    'Japanese anime studios share new work\nThese anime teams share clips with fans\nTheir music and art show how anime is made',
])
def test_unpunctuated_english_prose_is_not_navigation(body):
    assert assess_topic_relevance('Japanese anime', 'Japanese anime studio news', body)['relevant']


def test_unpunctuated_english_menu_is_not_substantive_body():
    body = '\n'.join(['Home Login Search Anime News', 'Music Menu Search Login', 'Home Anime Login Search'])
    assert not assess_topic_relevance('anime', 'Anime news', body)['relevant']


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / 'interest-rotation')
    store.save_persona({'id': 'topic', 'name': '兴趣伙伴', 'system_prompt': '自然回答',
                       'interests': ['关东煮与薄荷甜点', '音乐'], 'exploration_enabled': True})
    cfg = {'paused': False, 'exploration': {'enabled': True, 'daily_searches': 30, 'max_results': 4,
                                          'valid_days': 90, 'interval_hours': 24}}
    queries = []
    async def search(query):
        queries.append(query)
        return {'links': []}
    async def read(url):
        raise AssertionError('Unexpected fetch')
    service = PersonaKnowledge(store, cfg, None, search, read)
    yield store, cfg, service, queries, search, read
    store.close()


@pytest.mark.anyio
async def test_compound_focus_rotation_persists_and_keeps_interest_association(env):
    store, cfg, service, queries, search, read = env
    await service.explore('topic', manual=True)
    await service.explore('topic', manual=True)
    reboot = PersonaKnowledge(store, cfg, None, search, read)
    result = await reboot.explore('topic', manual=True)
    assert [query.split(' 最新资讯')[0] for query in queries] == ['关东煮', '音乐', '薄荷甜点']
    assert result['last_result']['interest'] == '关东煮与薄荷甜点'
    assert result['last_result']['query'].startswith('薄荷甜点 ')
    assert reboot.status()['personas']['topic']['last_focus'] == '薄荷甜点'
    PersonaKnowledge.validate_state(json.loads(reboot.export_state()))


@pytest.mark.anyio
async def test_excluded_interests_do_not_stall_later_interests_or_spend_extra_quota(env):
    store, cfg, service, queries, _, _ = env
    store.save_persona({'id': 'topic', 'interests': ['关东煮', '音乐'], 'excluded_topics': ['关东煮']})
    for _ in range(3):
        await service.explore('topic', manual=True)
    assert len(queries) == 3 and all(query.startswith('音乐 ') for query in queries)
    assert service.status()['searches_today'] == 3


@pytest.mark.anyio
async def test_excluding_one_compound_focus_keeps_other_focus_available(env):
    store, cfg, service, queries, _, _ = env
    store.save_persona({'id': 'topic', 'interests': ['关东煮与薄荷甜点'], 'excluded_topics': ['关东煮']})
    await service.explore('topic', manual=True)
    assert queries[0].startswith('薄荷甜点 ')
    assert service.status()['searches_today'] == 1


@pytest.mark.anyio
async def test_all_excluded_returns_without_search_or_quota_and_reconfig_recovers(env):
    store, cfg, service, queries, _, _ = env
    store.save_persona({'id': 'topic', 'excluded_topics': ['关东煮', '薄荷甜点', '音乐']})
    result = await service.explore('topic', manual=True)
    assert result['skipped'] and result['reason'] == 'excluded_topics'
    assert not queries and service.status()['searches_today'] == 0
    store.save_persona({'id': 'topic', 'excluded_topics': ['关东煮', '薄荷甜点']})
    await service.explore('topic', manual=True)
    assert queries[0].startswith('音乐 ')


@pytest.mark.anyio
async def test_query_stays_within_search_limit_without_losing_date_suffix(env):
    store, cfg, service, queries, _, _ = env
    store.save_persona({'id': 'topic', 'interests': ['测试主题' * 40]})
    await service.explore('topic', manual=True)
    assert len(queries[0]) == 128 and queries[0].endswith('月')


@pytest.mark.anyio
async def test_focus_cursor_prunes_removed_interests_and_accepts_old_state(env):
    store, cfg, service, queries, _, _ = env
    old = json.loads(service.export_state())
    old['personas']['topic'] = {'cursor': 0}
    PersonaKnowledge.validate_state(old)
    await service.explore('topic', manual=True)
    store.save_persona({'id': 'topic', 'interests': ['动画']})
    await service.explore('topic', manual=True)
    schedule = json.loads(service.export_state())['personas']['topic']
    expected = hashlib.sha256('动画'.encode()).hexdigest()[:16]
    assert set(schedule['focus_cursors']) == {expected}
    assert schedule['last_focus'] == '动画'


@pytest.mark.parametrize('field,value', [
    ('focus_cursors', []), ('focus_cursors', {'bad-key': 0}),
    ('focus_cursors', {'a' * 16: True}), ('focus_cursors', {'a' * 16: -1}),
    ('focus_cursors', {'a' * 16: 1000001}), ('last_focus', []), ('last_focus', 'a' * 161),
])
def test_focus_schedule_rejects_corrupt_imports(env, field, value):
    state = json.loads(env[2].export_state())
    state['personas']['topic'] = {field: copy.deepcopy(value)}
    with pytest.raises(ValueError, match='子主题'):
        PersonaKnowledge.validate_state(state)
