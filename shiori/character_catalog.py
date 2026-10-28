"""CSP-style core-site discovery: choose an identity before researching it.

Bangumi's public character/subject APIs provide IDs and aliases; MediaWiki is
a fallback. Search snippets never become evidence or invented character cards.
All API and HTML requests retain tools.py's public-address and size checks.
"""
from __future__ import annotations

import asyncio
import re
import unicodedata
from urllib.parse import quote, urlencode

from .tools import request_public_json, read_public

BGM = 'https://api.bgm.tv/v0'


def _key(value):
    return re.sub(r'[\W_]+', '', unicodedata.normalize('NFKC', str(value))).casefold()


def wiki_works(text):
    """Extract credited works from identity statements, never search keywords."""
    lead = unicodedata.normalize('NFKC', str(text)[:2000])
    names = []
    for match in re.finditer(r'(?<![不未认認])(?:是|为|為)([^。！？\n]{1,320}?)(?:角色|人物)', lead[:700]):
        credit = match[1]
        quoted = re.findall(r'《([^》\n]{1,160})》|「([^」\n]{1,160})」|『([^』\n]{1,160})』', credit)
        if quoted:
            names.extend(next(part for part in group if part) for group in quoted)
        else:
            credit = re.sub(r'(?:中|里|裡|内|內)?的?(?:(?:虚构|虛構|登场|登場|主要|重要|一名|一位|一个|一個))*$', '', credit).strip()
            names.append(credit)
            names.append(re.sub(r'^(?:电子游戏|電子遊戲|电视动画|電視動畫|轻小说|輕小說|游戏|遊戲|动画|動畫|漫画|漫畫|小说|小說)\s*', '', credit))
    for match in re.finditer(r'fictional character[^.\n]{0,80}?\b(?:from|in)\s+(?:the\s+)?(?:(?:video game|anime|manga|novel|series)\s+)?([^\.\n]{1,160})', lead[:700], re.I):
        names.append(match[1].strip(' "\''))
    return list(dict.fromkeys(name.strip() for name in names if 1 <= len(name.strip()) <= 160))[:12]


def work_matches(works, requested, aliases=()):
    wanted = {_key(name) for name in (requested, *aliases) if name}
    return any(_key(name) in wanted for name in works)


def _rows(value):
    rows = value.get('data', []) if isinstance(value, dict) else value
    return [row for row in rows[:50] if isinstance(row, dict)] if isinstance(rows, list) else []


def _id(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value < 10**10


def _infobox(detail):
    value = detail.get('infobox')
    return [row for row in value[:60] if isinstance(row, dict)] if isinstance(value, list) else []


def _names(detail):
    names = [str(detail.get(key) or '')[:160] for key in ('name_cn', 'name')]
    for item in _infobox(detail):
        if not isinstance(item, dict) or item.get('key') not in ('简体中文名', '别名'):
            continue
        value = item.get('value')
        if isinstance(value, str):
            names.append(value[:160])
        elif isinstance(value, list):
            names.extend(str(x.get('v', ''))[:160] for x in value[:20] if isinstance(x, dict))
    return list(dict.fromkeys(x.strip() for x in names if x.strip()))[:24]


def _page(detail, works):
    names = _names(detail)
    title = names[0] if names else ''
    # Infobox strings are data, never interpreted as instructions or HTML.
    fields = []
    for item in _infobox(detail):
        if isinstance(item, dict) and isinstance(item.get('value'), str):
            fields.append(str(item.get('key', ''))[:80] + '：' + item['value'][:500])
    body = ('角色：' + title + '\n别名：' + '、'.join(names) + '\n作品：' + '、'.join(works)
            + '\n' + str(detail.get('summary') or '')[:10000] + '\n' + '\n'.join(fields))[:16000]
    return {'url': f'https://bgm.tv/character/{detail["id"]}', 'title': title, 'text': body}


class CharacterCatalog:
    def __init__(self, *, json_fn=None, read_fn=None):
        self.json_fn = json_fn or request_public_json
        self.read_fn = read_fn or read_public

    async def lookup(self, query, work=''):
        failures, roles = [], []
        gate = asyncio.Semaphore(3)
        deadline = asyncio.get_running_loop().time() + 25

        async def api(path, payload=None):
            try:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError()
                async with asyncio.timeout(min(7, remaining)):
                    async with gate:
                        return await self.json_fn(BGM + path, payload)
            except (ValueError, OSError, TimeoutError) as exc:
                failures.append({'source': 'bangumi', 'url': BGM + path,
                                 'error': str(exc)[:200] or '资料接口超时'})
                return {}

        characters, subjects = await asyncio.gather(
            api('/search/characters?limit=8', {'keyword': query}),
            api('/search/subjects?limit=3', {'keyword': work or query, 'sort': 'match'}))
        work_query = not work and any(_key(query) == _key(row.get(field, ''))
                                     for row in _rows(subjects) for field in ('name', 'name_cn'))
        direct = {r['id']: r for r in _rows(characters) if _id(r.get('id'))}
        if work_query:
            # A character can be named after the work (e.g. Eromanga Sensei).
            # That match must not hide the actual cast of a work-only query.
            direct = {}
        subject_map = {}
        # A work-only query lists its cast. A character query uses the direct
        # identity search; an optional work narrows it without losing aliases.
        had_direct = bool(direct)
        if not direct or work:
            for subject in _rows(subjects)[:3]:
                if not _id(subject.get('id')):
                    continue
                label = str(subject.get('name_cn') or subject.get('name') or '')[:160]
                if not label or _key(work or query) not in _key(label + str(subject.get('name', ''))):
                    continue
                cast = await api(f'/subjects/{subject["id"]}/characters')
                for row in _rows(cast)[:24]:
                    if _id(row.get('id')):
                        subject_map.setdefault(row['id'], []).append(label)
                        if not had_direct or row['id'] in direct:
                            direct.setdefault(row['id'], row)
        from_work = work_query or (not _rows(characters) and bool(subject_map))

        async def role(character_id):
            detail, related = await asyncio.gather(api(f'/characters/{character_id}'),
                                                   api(f'/characters/{character_id}/subjects'))
            if not isinstance(detail, dict) or detail.get('id') != character_id:
                return
            names = _names(detail)
            if not names:
                return
            works = list(dict.fromkeys(subject_map.get(character_id, []) + [str(r.get('name_cn') or r.get('name') or '')[:160]
                        for r in _rows(related)]))[:12]
            works = [x for x in works if x]
            if not from_work and not any(_key(query) in _key(name) for name in names):
                return
            if work and not any(_key(work) in _key(name) for name in works):
                return
            display = next((str(item['value'])[:160] for item in _infobox(detail)
                            if isinstance(item, dict) and item.get('key') == '简体中文名' and isinstance(item.get('value'), str)), names[0])
            page = _page(detail, works)
            return {'id': f'bgm-{character_id}', 'name': display, 'aliases': names,
                    'work': works[0] if works else '作品信息待核对', 'works': works,
                    'description': str(detail.get('summary') or '')[:360],
                    'url': page['url'], 'source': 'Bangumi', '_page': page}

        resolved = await asyncio.gather(*(role(i) for i in list(direct)[:8]))
        roles = [r for r in resolved if r]
        if not from_work:
            roles.sort(key=lambda r: not any(_key(query) == _key(name) for name in r['aliases']))
        if not roles:
            aliases = []
            for subject in _rows(subjects):
                names = [str(subject.get(key) or '') for key in ('name', 'name_cn')]
                if work and any(_key(work) == _key(name) for name in names):
                    aliases.extend(name for name in names if name)
            roles = await self._wiki(query, work, failures, aliases)
        return {'characters': roles, 'failures': failures,
                'warnings': ([] if roles else ['核心站点暂未找到角色，请换用完整角色名或作品名称重试。'])}

    async def _wiki(self, query, work, failures, aliases=()):
        async def find(base):
            params = {'action': 'query', 'format': 'json', 'formatversion': '2', 'generator': 'search',
                      'gsrsearch': query + (' ' + work if work else ''), 'gsrlimit': '4',
                      'prop': 'extracts|info', 'explaintext': '1', 'exchars': '12000', 'inprop': 'url'}
            url = base + '/api.php?' + urlencode(params)
            try:
                data = await asyncio.wait_for(self.json_fn(url), 7)
                pages = data.get('query', {}).get('pages', [])
                result = []
                for row in pages[:4] if isinstance(pages, list) else []:
                    title, body = str(row.get('title') or ''), str(row.get('extract') or '')[:16000]
                    if not title or _key(query) not in _key(title) or len(body) < 80:
                        continue
                    if not re.search(r'(?:是|为|為)[^。\n]{0,100}(?:角色|人物)|fictional character', body[:500], re.I):
                        continue
                    works = wiki_works(body)
                    if work and not work_matches(works, work, aliases):
                        failures.append({'source': base, 'error': '备用来源未能核实限定作品，未作为角色候选'})
                        continue
                    credited = next((name for name in works if work_matches([name], work, aliases)), works[0] if works else '作品信息待核对')
                    page_base = 'https://zh.wikipedia.org/wiki' if 'wikipedia' in base else base
                    page = {'title': title[:240], 'text': body, 'url': page_base + '/' + quote(title.replace(' ', '_'))}
                    result.append({'id': ('moegirl-' if 'moegirl' in base else 'wikipedia-') + str(row['pageid']),
                                   'name': title[:160], 'aliases': [title[:160]], 'work': credited,
                                   'works': works, 'work_verified': bool(works), '_work_aliases': list(aliases),
                                   'description': body[:360], 'url': page['url'],
                                   'source': '萌娘百科' if 'moegirl' in base else 'Wikipedia', '_page': page})
                return result
            except (ValueError, OSError, TimeoutError, AttributeError, KeyError, TypeError) as exc:
                failures.append({'source': base, 'url': url, 'error': str(exc)[:200] or '资料接口超时'})
                return []
        groups = await asyncio.gather(find('https://zh.moegirl.org.cn'), find('https://zh.wikipedia.org/w'))
        return [r for group in groups for r in group][:8]

    async def research(self, role):
        """Read complementary sources after identity confirmation, not before it."""
        primary = role['_page']
        pages, failures = [primary], []
        urls = ['https://zh.moegirl.org.cn/' + quote(role['name'])]
        # Known title is a hint only. No unverified search landing page is used.
        for url in urls:
            try:
                page = await asyncio.wait_for(self.read_fn(url), 7)
                body, title = str(page.get('text') or ''), str(page.get('title') or '')
                if (len(body) < 80 or not any(_key(alias) in _key(title + body[:4000]) for alias in role['aliases'])
                        or any(x in (title + body[:1000]).lower() for x in ('just a moment', 'verify you are human', '请完成验证'))):
                    raise ValueError('补充来源缺少可核对的角色正文')
                pages.append(page)
            except (ValueError, OSError, TimeoutError) as exc:
                failures.append({'url': url, 'reason': str(exc)[:200] or '补充来源超时'})
        return pages, failures
