"""Model-authored message boundaries, plus bounded transport protection."""
from __future__ import annotations

import re
import json
import unicodedata


DEFAULT_OUTPUT = {'enabled': True, 'mode': 'model', 'target_chars': 160, 'max_chars': 320,
                  'interval_ms': 600, 'max_parts': 8, 'platform_max_chars': 1800,
                  'reply_max_chars': 0, 'message_max_chars': 0, 'remove_parentheses': False}
_PROTECTED_TEXT = r'(?P<fence>`{3,}|~{3,})[\s\S]*?(?:(?P=fence)|$)|`[^`\n]+`|\[(?:\\.|[^\]\\\n])*\]\(|https?://[^\s<>]+'


class OutputLimitError(ValueError):
    """A protected atom cannot be delivered intact under the local transport cap."""


def _markdown_destination_end(text, opening):
    """Find an inline link's closing parenthesis without a nesting-depth limit."""
    depth, index, quote, angled = 1, opening + 1, None, False
    while index < len(text):
        char = text[index]
        if char == '\\':
            index += 2
            continue
        if quote:
            if char == quote:
                quote = None
        elif angled:
            if char == '>':
                angled = False
        elif char == '<':
            angled = True
        elif char in ('"', "'") and text[index - 1].isspace():
            quote = char
        elif char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return None


def _protected_spans(text):
    """Yield opaque code/link spans, extending links past nested URL parentheses.

    The regular expression finds candidates, while the iterative parser owns
    the complete destination. An unfinished destination conservatively keeps
    the remaining text opaque rather than exposing a partial link to cutters.
    """
    pattern, position = re.compile(_PROTECTED_TEXT), 0
    while match := pattern.search(text, position):
        start, end = match.span()
        if text[start] == '[':
            parsed = _markdown_destination_end(text, end - 1)
            end = parsed if parsed is not None else len(text)
        yield start, end
        position = end


def _authored_boundaries(text):
    pattern = re.compile(r'(?:\r?\n[ \t]*){2,}')
    position = 0
    for start, end in _protected_spans(text):
        yield from pattern.finditer(text, position, start)
        position = end
    yield from pattern.finditer(text, position)


def strip_memory_citations(text, memory_ids, *, user_text=''):
    """Remove only known internal citation markers, before storage/pacing.

    Never guess that an arbitrary hex string is an internal ID. Literal user
    examples, code, links and URLs are preserved.
    """
    tokens = set()
    for key in memory_ids:
        if isinstance(key, str) and len(key) >= 8:
            tokens.update((key.casefold(), key[:8].casefold()))
    if not tokens: return text
    citation = re.compile(r'\[([^\[\]\n]{1,400})\]|【([^【】\n]{1,400})】')
    def remove(match):
        marker = match.group()
        if marker in user_text: return marker
        body = match.group(1) or match.group(2)
        body = re.sub(r'^(?:记忆|来源|memory)(?:\s*ID)?\s*[:：]?\s*', '', body, flags=re.I)
        parts = re.split(r'[,，;；、\s]+', body.strip())
        return '' if parts and all(part.casefold() in tokens for part in parts) else marker
    result, position = [], 0
    for start, end in _protected_spans(text):
        result.extend((citation.sub(remove, text[position:start]), text[start:end]))
        position = end
    result.append(citation.sub(remove, text[position:]))
    return ''.join(result)


def transport_limit(config, scope=None):
    scope_id = str((scope or {}).get('id', ''))
    section = 'qq_official' if scope_id.startswith('qqofficial:') else 'onebot'
    override = (config.get(section) or {}).get('max_message_chars', 0)
    return max(40, min(20000, int(override or (config.get('output') or {}).get('platform_max_chars', 1800))))


def output_config(config, scope=None):
    result = dict(DEFAULT_OUTPUT, **(config.get('output') or {}))
    scope = scope or {}
    local = scope.get('output') or (scope.get('settings') or {}).get('output') or {}
    result.update({key: value for key, value in local.items() if key in result and value is not None})
    for key in DEFAULT_OUTPUT:
        value = scope.get('output_' + key, (scope.get('settings') or {}).get('output_' + key))
        if value is not None:
            result[key] = value
    if result['mode'] not in ('model', 'length'):
        raise ValueError('输出分段模式无效')
    result['target_chars'] = max(20, min(2000, int(result['target_chars'])))
    result['max_chars'] = max(result['target_chars'], min(4000, int(result['max_chars'])))
    result['max_parts'] = max(1, min(30, int(result['max_parts'])))
    result['interval_ms'] = max(0, min(5000, int(result['interval_ms'])))
    result['platform_max_chars'] = transport_limit(config, scope)
    for key, maximum in (('reply_max_chars', 20000), ('message_max_chars', 4000)):
        if type(result[key]) is not int or not 0 <= result[key] <= maximum:
            raise ValueError(f'{key}须为0—{maximum}的整数')
    if type(result['remove_parentheses']) is not bool:
        raise ValueError('remove_parentheses须为布尔值')
    return result


def _atoms(text):
    # Markdown links, fenced and inline code, and bare URLs are indivisible.
    position = 0
    for start, end in _protected_spans(text):
        yield from _graphemes(text[position:start])
        yield text[start:end]
        position = end
    yield from _graphemes(text[position:])


def _graphemes(text):
    cluster = ''
    regional = 0
    for char in text:
        code = ord(char)
        attached = (unicodedata.combining(char) != 0 or char == '\u200d' or cluster.endswith('\u200d')
                    or 0xFE00 <= code <= 0xFE0F or 0x1F3FB <= code <= 0x1F3FF
                    or 0xE0100 <= code <= 0xE01EF)
        flag = 0x1F1E6 <= code <= 0x1F1FF
        if cluster and not attached and not (flag and regional == 1):
            yield cluster
            cluster = ''
            regional = 0
        cluster += char
        regional = regional + 1 if flag else 0
    if cluster:
        yield cluster


def generation_instructions(config, scope=None):
    options = output_config(config, scope)
    constraints = []
    if options['reply_max_chars']:
        constraints.append(f'这轮所有消息合计不超过{options["reply_max_chars"]}个字符，优先简洁完整地说清重点。')
    if options['message_max_chars']:
        constraints.append(f'每条独立消息不超过{options["message_max_chars"]}个字符；需要多条时，请自己在完整句意结束后用空行分隔。')
    if options['remove_parentheses']:
        constraints.append('不要在普通回复中生成圆括号及其中的内容，包括（动作描写）和(旁白)；必要信息直接写进正文。代码、链接及用户要求的完整JSON保持原样。')
    if constraints:
        constraints.append('字符含中文、英文、标点和空格，不计换行。预先安排各条内容，最后一条也要简短，不要把剩余内容堆到最后。')
    common = '\n【回复内容要求】' + ''.join(constraints) if constraints else ''
    if not options['enabled']:
        return common + '\n消息格式：本次只发送一条完整消息，不主动拆成多条。可保留必要的列表、代码和文档排版。'
    if options['mode'] == 'length': return common
    return common + ('\n【消息发送格式】在生成回复时自行决定每条独立消息的边界。需要另发一条时，使用一个空行（两个换行）分隔；'
            '单个换行只是同一条消息内的排版。可以只回复几个字、一句话，也可以在一条里说好几句；不要固定段数，不要为凑长度拆开一句话。'
            f'本次最多建议{options["max_parts"]}条，能一条说清就只发一条。提前安排每条的信息量，最后一条不要明显长于前面几条，也不要把剩余内容全堆到最后。'
            '不要为了分段添加序号、JSON包装、特殊分隔符或解释文字。完整代码用代码围栏包住，代码内空行不是消息边界；链接保持完整。'
            '用户要求可直接复制的JSON、表格、完整文档或原文时，保持其完整格式放在一条中，不为聊天节奏打断。')


def _json_document(text):
    if not str(text).lstrip().startswith(('{', '[')):
        return False
    try:
        return isinstance(json.loads(text), (dict, list))
    except (ValueError, RecursionError):
        return False


def _remove_plain_parentheses(text):
    pairs, stack = [], []
    for index, char in enumerate(text):
        if char in '(（':
            stack.append((char, index))
        elif char in ')）' and stack and {'(': ')', '（': '）'}[stack[-1][0]] == char:
            _, start = stack.pop()
            pairs.append((start, index + 1))
    result, cursor = [], 0
    for start, end in sorted(pairs, key=lambda span: (span[0], -span[1])):
        if start >= cursor:
            result.append(text[cursor:start])
            cursor = end
    result.append(text[cursor:])
    return ''.join(result)


def remove_parenthetical_content(text):
    """Remove paired round-parenthesis prose; preserve code, links and JSON.

    Unmatched delimiters remain literal. A protected atom also separates prose
    regions, so a parenthesis around code cannot erase a code example.
    """
    text = str(text)
    if _json_document(text):
        return text
    result, cursor = [], 0
    for start, end in _protected_spans(text):
        result.extend((_remove_plain_parentheses(text[cursor:start]), text[start:end]))
        cursor = end
    result.append(_remove_plain_parentheses(text[cursor:]))
    return ''.join(result)


def reply_char_count(text, *, stop_after=None):
    count = 0
    for cluster in _graphemes(str(text)):
        if cluster not in ('\r', '\n', '\r\n'):
            count += 1
            if stop_after is not None and count > stop_after:
                break
    return count


def _shortened(text, limit):
    """Shorten display only; never invent a new message or break an opaque atom."""
    if reply_char_count(text, stop_after=limit) <= limit:
        return text, False
    if limit <= 1:
        return '…'[:limit], True
    fallback = '内容超出字数上限，请查看原文。'
    if _json_document(text):
        return _shortened(fallback, limit)[0], True
    prefix, used, natural_end, prefix_length = [], 0, 0, 0
    for atom in _atoms(text):
        size = reply_char_count(atom, stop_after=limit)
        if used + size > limit - 1:
            break
        prefix.append(atom)
        used += size
        prefix_length += len(atom)
        if len(atom) == 1 and atom in '。！？!?；;\n':
            natural_end = prefix_length
    if not prefix:
        return _shortened(fallback, limit)[0], True
    candidate = ''.join(prefix).rstrip()
    # Prefer a finished sentence where there is one near the limit. This is a
    # configured display cap, not an algorithm for making additional messages.
    boundary = natural_end
    if boundary and boundary >= len(candidate) // 2:
        candidate = candidate[:boundary].rstrip()
    return candidate + '…', True


def prepare_reply(text, config=None, scope=None):
    """Apply explicit content preferences once; retain source for manual viewing."""
    original = str(text)
    options = output_config(config or {}, scope)
    cleaned = remove_parenthetical_content(original) if options['remove_parentheses'] else original
    removed = cleaned != original
    if removed:
        cleaned = cleaned.strip()
    parts = model_reply_parts(cleaned, config, scope)
    final, used, limited = [], 0, False
    for index, part in enumerate(parts):
        remaining = options['reply_max_chars'] - used if options['reply_max_chars'] else None
        if remaining is not None and remaining <= 0:
            limited = True
            break
        caps = [cap for cap in (remaining, options['message_max_chars']) if cap is not None and cap > 0]
        if caps:
            part, changed = _shortened(part, min(caps))
            limited = limited or changed
        if part.strip():
            final.append(part)
            used += reply_char_count(part)
        if options['reply_max_chars'] and used >= options['reply_max_chars'] and index < len(parts) - 1:
            limited = True
            break
    reply = '\n\n'.join(final) if limited else cleaned
    result = {'reply': reply, 'reply_parts': final,
              'output_adjustment': {'limited': limited, 'removed_parentheses': removed},
              'output_filtered': bool(original.strip() and not reply.strip())}
    if reply != original:
        result['original_reply'] = original
    return result


def _balanced_authored_parts(parts, maximum):
    """Merge adjacent authored parts with the smallest possible longest group.

    The only allowed cuts are existing model boundaries. Joining a group keeps
    its blank lines and original order; an indivisible long paragraph stays
    intact even when it determines the optimal maximum length.
    """
    if len(parts) <= maximum:
        return parts
    lengths = [len(part) for part in parts]
    low, high = max(lengths), sum(lengths) + 2 * (len(parts) - 1)
    while low < high:
        bound = (low + high) // 2
        count, size = 1, 0
        for length in lengths:
            if size and size + 2 + length > bound:
                count, size = count + 1, length
            else:
                size += length + (2 if size else 0)
        if count <= maximum:
            high = bound
        else:
            low = bound + 1
    result, group, size = [], [], 0
    remaining = maximum
    for index, part in enumerate(parts):
        # The second condition leaves at least one authored part for each
        # remaining group when a large paragraph makes the bound permissive.
        if group and (size + 2 + len(part) > low or len(parts) - index < remaining):
            result.append('\n\n'.join(group))
            remaining -= 1
            group, size = [], 0
        size += len(part) + (2 if group else 0)
        group.append(part)
    if group:
        result.append('\n\n'.join(group))
    return result


def model_reply_parts(text, config=None, scope=None):
    """Read only authored blank-line boundaries; never infer stops from length.

    Whitespace-only messages are ignored. Code, links and inline code are
    opaque, and a complete JSON value remains one copyable message. A flood of
    authored paragraphs is merged evenly at existing boundaries without loss.
    """
    text = str(text)
    options = output_config(config or {}, scope)
    if not text.strip(): return []
    if not options['enabled']: return [text]
    if options['mode'] == 'length': return _length_parts(text, options)
    if text.lstrip().startswith(('{', '[')):
        try:
            if isinstance(json.loads(text), (dict, list)): return [text.strip()]
        except (ValueError, RecursionError): pass
    parts, start = [], 0
    for match in _authored_boundaries(text):
        part = text[start:match.start()].strip()
        if part: parts.append(part)
        start = match.end()
    tail = text[start:].strip()
    if tail: parts.append(tail)
    maximum = options['max_parts']
    if len(parts) > maximum:
        parts = _balanced_authored_parts(parts, maximum)
    return parts


def split_output(text, config=None, scope=None, *, reply_parts=None):
    """Preserve model choices; split further only at the actual transport cap."""
    options = output_config(config or {}, scope)
    normalize = lambda value: re.sub(r'\n[ \t]*\n(?:[ \t]*\n)*', '\n\n', value.replace('\r\n', '\n').replace('\r', '\n')).strip()
    valid = (isinstance(reply_parts, list) and 0 < len(reply_parts) <= 30
             and all(isinstance(part, str) and part.strip() for part in reply_parts)
             and normalize('\n\n'.join(reply_parts)) == normalize(str(text)))
    # Re-grouping the already shortened paragraphs can change character
    # weights and exceed a per-message cap. The saved final boundaries win.
    authored = reply_parts if valid else model_reply_parts(text, config, scope)
    return [chunk for part in authored
            for chunk in _hard_parts(part, options['platform_max_chars']) if chunk.strip()]


def _length_parts(text, options):
    """Prefer natural stops near target length, with no fixed number of sentences.

    An oversized protected atom or the final max-parts tail may exceed max_chars.
    Preserving code/links and complete content takes precedence over the soft cap.
    """
    text = str(text)
    if not text:
        return []
    if not options['enabled'] or len(text) <= options['target_chars']:
        return [text] if text.strip() else []
    atoms = list(_atoms(text))
    parts = []
    while atoms and len(parts) < options['max_parts'] - 1:
        total, boundary = 0, 0
        strong, weak = [], []
        for index, atom in enumerate(atoms):
            if total + len(atom) > options['max_chars'] and index:
                break
            total += len(atom)
            boundary = index + 1
            end = atom[-1:]
            if end in '\n。！？!?；;':
                strong.append((boundary, total))
            elif end in '，, ':
                weak.append((boundary, total))
            elif end == '.' and (index + 1 == len(atoms) or atoms[index + 1].isspace()):
                strong.append((boundary, total))
        if boundary == len(atoms):
            if total <= options['target_chars']:
                break
        candidates = strong or [pair for pair in weak if pair[1] >= options['target_chars'] // 2]
        if candidates:
            cut = min(candidates, key=lambda pair: abs(pair[1] - options['target_chars']))[0]
        else:
            # A target length is not a reason to cut a sentence in half. Keep
            # it whole up to the transport limit; only that hard cap must win.
            cut, size = 0, 0
            for atom in atoms:
                if size + len(atom) > options['platform_max_chars'] and cut:
                    break
                size += len(atom)
                cut += 1
                if atom[-1:] in '\n。！？!?；;' and size >= options['target_chars']:
                    break
        # Closing quotation marks belong to the sentence they close, including
        # long sentences allowed to exceed the preferred length.
        while cut < len(atoms) and atoms[cut] in '”’」』）)]':
            cut += 1
        # Scan a whitespace prefix once instead of repeatedly joining a growing
        # prefix when a provider emits thousands of empty lines.
        if not ''.join(atoms[:cut]).strip():
            while cut < len(atoms) and not atoms[cut].strip():
                cut += 1
            if cut < len(atoms):
                cut += 1
        parts.append(''.join(atoms[:cut]))
        atoms = atoms[cut:]
    if atoms:
        tail = ''.join(atoms)
        if parts and not tail.strip():
            parts[-1] += tail
        else:
            parts.append(tail)
    return [part for part in parts if part.strip()]


def _hard_parts(text, limit):
    """The transport cap wins over max_parts; no truncation or broken URLs/code."""
    parts, current = [], ''
    for atom in _atoms(text):
        if len(atom) > limit:
            raise OutputLimitError('完整链接、代码或字符超过当前发送长度上限；原文已保留在本地对话记录。')
        if current and len(current) + len(atom) > limit:
            parts.append(current)
            current = ''
        current += atom
    if current:
        if parts and not current.strip() and len(parts[-1]) + len(current) <= limit:
            parts[-1] += current
        else:
            parts.append(current)
    return parts


def split_transport_output(text, config, scope=None):
    """Adapters enforce only their hard cap; conversational pacing belongs to Flow."""
    return [part.strip() for part in _hard_parts(str(text), transport_limit(config, scope)) if part.strip()]
