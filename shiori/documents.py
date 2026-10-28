"""Text extraction for bounded, user-imported local knowledge documents."""
from __future__ import annotations

import io
import json
from pathlib import Path
import zipfile
import xml.etree.ElementTree as ET

MAX_BYTES = 8 * 1024 * 1024
MAX_CHARS = 300000
FORMATS = {'.txt', '.md', '.csv', '.json', '.pdf', '.docx'}


def extract_document(name, raw):
    suffix = Path(name).suffix.lower()
    if suffix not in FORMATS:
        raise ValueError('支持 TXT、Markdown、CSV、JSON、PDF 和 DOCX 资料')
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError('资料需非空且不超过 8MB')
    details = {'format': suffix.removeprefix('.'), 'warnings': [], 'pages': None}
    if suffix == '.docx':
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                if sum(x.file_size for x in archive.infolist()) > 32 * 1024 * 1024:
                    raise ValueError('DOCX 解压体积超过 32MB')
                xml = archive.read('word/document.xml')
                if b'<!DOCTYPE' in xml.upper() or b'<!ENTITY' in xml.upper():
                    raise ValueError('DOCX 含不支持的 XML 声明')
                root = ET.fromstring(xml)
                ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
                text = '\n'.join(''.join(p.itertext()) if not p.findall('.//w:t', ns) else ''.join(t.text or '' for t in p.findall('.//w:t', ns)) for p in root.findall('.//w:p', ns))
        except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
            raise ValueError('DOCX 无效或缺少正文') from exc
        details['warnings'].append('提取正文与表格中的文字，图片、批注和复杂版式未作为内容导入')
    elif suffix == '.pdf':
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(raw))
            if reader.is_encrypted:
                raise ValueError('请先解密 PDF 再导入')
            if len(reader.pages) > 300:
                raise ValueError('单份 PDF 最多 300 页，请拆分导入')
            details['pages'] = len(reader.pages)
            parts, length, has_text = [], 0, False
            for index, page in enumerate(reader.pages):
                body = page.extract_text() or ''
                has_text = has_text or bool(body.strip())
                part = f'【第 {index + 1} 页】\n' + body
                length += len(part)
                if length > MAX_CHARS:
                    raise ValueError('资料文字超过 30 万字，请拆分导入')
                parts.append(part)
            text = '\n\n'.join(parts)
            if not has_text:
                raise ValueError('PDF 没有可提取的正文；扫描件请先进行 OCR')
        except ImportError as exc:
            raise ValueError('当前安装缺少 PDF 解析器，请使用完整版或安装 pypdf') from exc
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError('PDF 正文读取失败，请检查文件是否损坏或加密') from exc
        details['warnings'].append('PDF 依赖其文字层，扫描件不支持 OCR，提取顺序可能与视觉排版不同')
    else:
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            try:
                text = raw.decode('gb18030')
            except UnicodeDecodeError as exc:
                raise ValueError('请使用 UTF-8 或常见中文文本编码') from exc
        if suffix == '.json':
            try:
                json.loads(text)
            except (json.JSONDecodeError, RecursionError) as exc:
                raise ValueError('JSON 资料格式无效') from exc
    if not text.strip() or '\x00' in text:
        raise ValueError('资料为空或不是可读取的文本')
    if len(text) > MAX_CHARS:
        raise ValueError('资料文字超过 30 万字，请拆分导入')
    return {'text': text.strip(), 'characters': len(text.strip()), **details}
