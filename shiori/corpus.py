"""Bounded, non-extracting corpus reader for persona distillation.

Uploaded data is kept separate from memories. ZIP members are read in memory;
no member name is ever used as an output path.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import stat
import zipfile
from collections import Counter
from html.parser import HTMLParser
from pathlib import PurePosixPath

from .documents import extract_document

MAX_UPLOAD_BYTES = 24 * 1024 * 1024
MAX_TOTAL_BYTES = 48 * 1024 * 1024
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_FILES = 200
MAX_TOTAL_CHARS = 1_000_000
MAX_FILE_CHARS = 300_000
FORMATS = {'.txt', '.md', '.csv', '.json', '.jsonl', '.html', '.htm', '.srt', '.vtt', '.pdf', '.docx'}
SPEAKER_KEYS = ('speaker', 'sender', 'from', 'author', 'user', 'nickname', 'name', '发送者', '昵称', '说话者')
TEXT_KEYS = ('text', 'content', 'message', 'body', 'msg', '文字', '内容', '消息')


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'head'):
            self.hidden += 1
        elif not self.hidden and tag in ('p', 'div', 'br', 'li', 'tr', 'h1', 'h2', 'h3'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'head') and self.hidden:
            self.hidden -= 1
        elif not self.hidden and tag in ('p', 'div', 'li', 'tr'):
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _decode(raw):
    if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
        try:
            return raw.decode('utf-16')
        except UnicodeDecodeError as exc:
            raise ValueError('UTF-16 文本无效') from exc
    if b'\x00' in raw:
        # UTF-16 exports are common, but a BOM is required to distinguish them
        # from executable/binary input.
        raise ValueError('不是可读取的文本')
    for encoding in ('utf-8-sig', 'gb18030'):
        try:
            text = raw.decode(encoding)
            if sum(ord(ch) < 32 and ch not in '\n\r\t' for ch in text) > max(1, len(text) // 200):
                raise ValueError('文本中含有过多二进制控制字符')
            return text
        except UnicodeDecodeError:
            continue
    raise ValueError('无法识别文本编码，请转换为 UTF-8')


def _speaker_label(label, identity):
    original = identity
    if len(identity) > 100:
        identity = identity[:80] + '#' + hashlib.sha256(identity.encode()).hexdigest()[:16]
    if not label or label == original:
        return identity
    suffix = f' ({identity})'
    return label[:120 - len(suffix)] + suffix


def _speaker(value):
    if isinstance(value, dict):
        identity = next((str(value[key]).strip() for key in ('user_id', 'id')
                         if isinstance(value.get(key), (str, int)) and not isinstance(value[key], bool)), '')
        label = next((str(value[key]).strip() for key in ('nickname', 'name', 'display_name')
                      if isinstance(value.get(key), str) and value[key].strip()), '')
        if identity:
            return _speaker_label(label, identity)
        for key in ('nickname', 'name', 'display_name', 'user_id', 'id'):
            if key in value:
                return _speaker(value[key])
        return ''
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        return str(value).strip()[:120]
    return ''


def _message(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        # OneBot-style message segments: images and tool payloads aren't text.
        return ''.join(str(x.get('data', {}).get('text', '')) for x in value
                       if isinstance(x, dict) and x.get('type') == 'text' and isinstance(x.get('data'), dict))
    return ''


def _records(value, *, depth=0, budget=None):
    if budget is None:
        budget = [100_000]
    if depth > 12 or budget[0] <= 0:
        return []
    budget[0] -= 1
    if isinstance(value, list):
        return [record for item in value for record in _records(item, depth=depth + 1, budget=budget)]
    if not isinstance(value, dict):
        return []
    sender = next((_speaker(value[k]) for k in SPEAKER_KEYS if k in value and _speaker(value[k])), '')
    # Preserve a top-level stable sender ID when exports put the display name
    # and account identity in separate fields.
    account = next((str(value[k]).strip() for k in ('sender_id', 'user_id', 'author_id')
                    if isinstance(value.get(k), (str, int)) and not isinstance(value[k], bool)), '')
    if account and sender and not isinstance(value.get('sender'), dict) and not sender.endswith(f'({account})'):
        sender = _speaker_label(sender, account)
    text = next((_message(value[k]) for k in TEXT_KEYS if k in value and _message(value[k]).strip()), '')
    if sender and text:
        return [{'speaker': sender, 'text': text.strip()}]
    return [record for item in value.values() if isinstance(item, (dict, list))
            for record in _records(item, depth=depth + 1, budget=budget)]


def _plain_segments(text):
    parts = []
    current = None
    # Avoid URLs and timestamps being misread as names.
    pattern = re.compile(r'^(?:\[?\d{4}[-/]\d{1,2}[-/]\d{1,2}[ T]\d{1,2}:\d{2}(?::\d{2})?\]?\s*)?([^\s:：\[\]<>]{1,40})\s*[:：]\s*(.+)$')
    for line in text.splitlines():
        match = pattern.match(line)
        if match and match.group(1).lower() not in ('http', 'https', 'ftp') and not match.group(1).isdigit():
            current = {'speaker': match.group(1), 'text': match.group(2)}
            parts.append(current)
        elif line.strip():
            if current:
                current['text'] += '\n' + line
            else:
                parts.append({'speaker': '', 'text': line})
    # A single heading is rarely a reliably identified speaker.
    return parts if sum(bool(x['speaker']) for x in parts) >= 2 else []


def read_file(name, raw):
    suffix = PurePosixPath(name).suffix.lower()
    if suffix not in FORMATS:
        raise ValueError('暂不支持该格式')
    if not raw:
        raise ValueError('文件为空')
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError('单文件超过 8MB，请拆分')
    warnings, segments = [], []
    if suffix in ('.pdf', '.docx'):
        result = extract_document(name, raw)
        text, warnings = result['text'], result['warnings']
    else:
        text = _decode(raw)
        try:
            if suffix == '.json':
                segments = _records(json.loads(text))
            elif suffix == '.jsonl':
                rows = [json.loads(line) for line in text.splitlines() if line.strip()]
                segments = _records(rows)
            elif suffix == '.csv':
                try:
                    dialect = csv.Sniffer().sniff(text[:8192], delimiters=',;\t')
                except csv.Error:
                    dialect = csv.excel
                segments = _records(list(csv.DictReader(io.StringIO(text), dialect=dialect)))
            elif suffix in ('.html', '.htm'):
                parser = _HTMLText()
                parser.feed(text)
                text = ''.join(parser.parts)
            elif suffix in ('.srt', '.vtt'):
                text = '\n'.join(line for line in text.splitlines()
                                 if line.strip() and not line.strip().isdigit() and '-->' not in line
                                 and not line.strip().startswith(('WEBVTT', 'NOTE ', 'STYLE')))
        except (json.JSONDecodeError, RecursionError, csv.Error) as exc:
            raise ValueError('结构化文件格式无效') from exc
        if segments:
            text = '\n'.join(f"{x['speaker']}：{x['text']}" for x in segments)
        else:
            segments = _plain_segments(text)
    text = text.strip()
    if not text or '\x00' in text:
        raise ValueError('没有可读取的正文；扫描 PDF 请先 OCR')
    if len(text) > MAX_FILE_CHARS:
        raise ValueError('单文件文字超过 30 万字，请拆分')
    if not segments:
        segments = _plain_segments(text)
    if not segments:
        warnings.append('未可靠识别说话者；此文件将按完整资料分析，不能自动区分引用与转述')
    return {'text': text, 'segments': segments, 'characters': len(text), 'warnings': warnings,
            'speakers': dict(Counter(x['speaker'] for x in segments if x['speaker'])),
            'sha256': hashlib.sha256(raw).hexdigest()}


def _safe_member(info):
    name = info.filename
    normalized = name.replace('\\', '/')
    path = PurePosixPath(normalized)
    if (normalized.startswith('/') or any(part in ('..', '') for part in path.parts)
            or any(':' in part or '\x00' in part for part in path.parts)
            or len(normalized) > 500):
        raise ValueError('ZIP 含有不安全的文件路径')
    if stat.S_ISLNK(info.external_attr >> 16):
        raise ValueError('ZIP 不支持符号链接')
    if info.flag_bits & 1:
        raise ValueError('ZIP 已加密，请先解密')
    if info.file_size > MAX_FILE_BYTES:
        return '单文件超过 8MB，请拆分'
    if info.file_size > 1024 * 1024 and info.file_size > max(1, info.compress_size) * 200:
        raise ValueError('ZIP 压缩比异常，请先拆分文件')
    return ''


def inspect_corpus(raw, name):
    """Return a public file inventory and internal parsed documents, no writes."""
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError('语料需非空且不超过 24MB')
    name = str(name or '语料.zip')[:200]
    files, documents, seen = [], [], set()
    total_chars = 0

    def add(filename, data=None, reason=''):
        nonlocal total_chars
        record = {'id': f'f{len(files) + 1}', 'name': filename, 'status': 'skipped',
                  'characters': 0, 'warnings': [], 'reason': reason, 'speakers': {}}
        files.append(record)
        if reason:
            return
        try:
            result = read_file(filename, data)
            if total_chars + result['characters'] > MAX_TOTAL_CHARS:
                record['reason'] = '语料总文字超过 100 万字，请拆分上传'
                return
            total_chars += result['characters']
            record.update({key: result[key] for key in ('characters', 'warnings', 'speakers')}, status='readable')
            documents.append({'id': record['id'], 'name': filename, **result})
        except ValueError as exc:
            record.update(status='failed', reason=str(exc))

    if PurePosixPath(name.replace('\\', '/')).suffix.lower() == '.zip':
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                infos = archive.infolist()
                if len(infos) > MAX_FILES * 3 or sum(not x.is_dir() for x in infos) > MAX_FILES:
                    raise ValueError('ZIP 最多包含 200 个文件')
                if sum(x.file_size for x in infos) > MAX_TOTAL_BYTES:
                    raise ValueError('ZIP 解压后超过 48MB，请拆分')
                for info in infos:
                    reason = _safe_member(info)
                    if info.is_dir():
                        continue
                    filename = info.filename.replace('\\', '/')
                    if filename.casefold() in seen:
                        raise ValueError('ZIP 含有重复文件名')
                    seen.add(filename.casefold())
                    if filename.startswith('__MACOSX/') or PurePosixPath(filename).name.startswith('.'):
                        reason = '忽略系统隐藏文件'
                    elif PurePosixPath(filename).suffix.lower() not in FORMATS:
                        reason = '暂不支持该格式（嵌套 ZIP 不会自动展开）'
                    if reason:
                        add(filename, reason=reason)
                    else:
                        with archive.open(info) as stream:
                            data = stream.read(MAX_FILE_BYTES + 1)
                        add(filename, data)
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError) as exc:
            raise ValueError('ZIP 无效、损坏或使用了不支持的压缩方式') from exc
    else:
        add(PurePosixPath(name.replace('\\', '/')).name, raw)
    speakers = Counter()
    for document in documents:
        speakers.update(document['speakers'])
    return {'name': name, 'files': files, 'speakers': [{'id': k, 'name': k, 'messages': v} for k, v in speakers.most_common(200)],
            'readable_files': len(documents), 'characters': total_chars,
            'warnings': ['上传仅进入蒸馏任务，不写入长期记忆。分析时选中的文字会发送给你配置的模型服务。'],
            '_documents': documents}


def corpus_chunks(documents, *, target_speaker='', selected_files=None, chunk_chars=7000):
    """Bounded, attributed chunks. Other speakers remain context, not evidence."""
    selected = set(selected_files) if selected_files is not None else None
    items = [item for item in documents if selected is None or item['id'] in selected]
    if not items:
        raise ValueError('请至少选择一个可读取文件')
    if target_speaker and not any(target_speaker in item['speakers'] for item in items):
        raise ValueError('所选文件中没有找到该说话者')
    chunks, buffer, length = [], [], 0

    def append(text, attribution):
        nonlocal buffer, length
        available = chunk_chars - len(attribution) - 2
        if available < 100:
            raise ValueError('文件名或归属标记过长，无法生成可靠的语料分块')
        for offset in range(0, len(text), available):
            part = attribution + '\n' + text[offset:offset + available]
            if length + len(part) > chunk_chars and buffer:
                chunks.append('\n'.join(buffer))
                buffer, length = [], 0
            buffer.append(part)
            length += len(part) + 1

    for item in items:
        header = f"[文件 {item['id']}：{item['name']}]"
        if target_speaker:
            # Preserve one neighboring message on each side as context only.
            segments = item['segments']
            targets = {i for i, row in enumerate(segments) if row['speaker'] == target_speaker}
            indices = sorted({j for i in targets for j in (i - 1, i, i + 1) if 0 <= j < len(segments)})
            for i in indices:
                row = segments[i]
                tag = '目标本人（仅此可作表达证据）' if i in targets else '其他人／上下文（不可归因给目标）'
                append(row['text'], f"{header}\n[{tag}，消息 {i + 1}，说话者 {row['speaker'] or '未识别'}]")
        else:
            append(item['text'], header)
    if buffer:
        chunks.append('\n'.join(buffer))
    if not chunks:
        raise ValueError('所选材料没有可分析的文字')
    return chunks
