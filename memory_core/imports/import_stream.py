"""Bounded JSON reads and disk grouping for official exports."""

from datetime import datetime
from collections import Counter
from contextlib import closing, contextmanager
from hashlib import sha256
from io import BufferedReader, BytesIO, RawIOBase, TextIOWrapper
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3
from tempfile import TemporaryDirectory
from zipfile import BadZipFile, ZipFile
from zlib import error as ZipDataError

import ijson

from ..storage.clock import business_day_label
from .import_formats import (ImportDay, ImportFormatError, MAX_UPLOAD_BYTES,
    MAX_JSON_BYTES, SOURCES, _object, _identifier, _hash, _chatgpt, _claude,
    _split, _memory, _gemini)
from .kelivo import kelivo_days
from .operit import operit_days
from .deepseek import FORMAT_ERROR as DEEPSEEK_FORMAT_ERROR, indexed_session_id, session_days


class JsonReader(RawIOBase):
    """Read bounded chunks and enforce actual bytes across all ZIP JSON members."""
    def __init__(self, source, budget):
        self.source, self.budget = source, budget

    def readable(self):
        return True

    def readinto(self, buffer):
        chunk = self.source.read(min(len(buffer), 65536))
        self.budget['used'] += len(chunk)
        if self.budget['used'] > self.budget['limit']:
            raise ImportFormatError('解压后的 JSON 总量超过 1 GiB 上限')
        buffer[:len(chunk)] = chunk
        return len(chunk)


def json_records(stream):
    try:
        if stream.peek(3).startswith(b'\xef\xbb\xbf'):
            stream.read(3)
        events = ijson.parse(stream,use_float=True)
        if next(events,None) != ('','start_array',None):
            raise ImportFormatError('需要官方导出的 JSON 记录数组')
        yield from ijson.items(events,'item')
    except (ijson.JSONError,UnicodeError,OverflowError,RecursionError):
        raise ImportFormatError('文件不是有效的 UTF-8 官方 JSON 导出，请勿上传 HTML') from None


def upload_limit_message(source):
    if source == 'kelivo':
        return '上传文件超过 90 MiB 上限，请在 Kelivo 只勾选要导入的类别后重新备份'
    if source == 'deepseek':
        return 'DeepSeek 导出 ZIP 超过 90 MiB 上限'
    return '上传文件超过 90 MiB 上限，请将 JSON 单独压成 ZIP 后上传'


def upload_files(source, uploads, max_bytes):
    uploaded = 0
    for filename, stream in uploads:
        if not isinstance(filename,str):
            raise ImportFormatError('上传文件格式不正确')
        if isinstance(stream,bytes):
            stream = BytesIO(stream)
        if not all(hasattr(stream,method) for method in ('read','seek','tell')):
            raise ImportFormatError('上传文件格式不正确')
        stream.seek(0,2)
        uploaded += stream.tell()
        stream.seek(0)
        if uploaded > max_bytes:
            raise ImportFormatError(upload_limit_message(source))
        yield filename, stream


def documents(source, uploads, max_json_bytes):
    declared = 0
    budget = dict(used=0,limit=max_json_bytes)
    for filename, stream in uploads:
        suffix = PurePosixPath(filename).suffix.lower()
        if source == 'memory':
            if suffix not in ('.md','.txt'):
                raise ImportFormatError('记忆文件只接受 UTF-8 的 .md 或 .txt')
            with TextIOWrapper(BufferedReader(JsonReader(stream,budget)),encoding='utf-8-sig') as text:
                yield text
        elif suffix == '.json':
            with BufferedReader(JsonReader(stream,budget)) as document:
                yield document
        elif suffix == '.zip':
            try:
                with ZipFile(stream) as archive:
                    found = False
                    for member in sorted(archive.infolist(),key=lambda entry:entry.filename):
                        path = PurePosixPath(member.filename)
                        if source == 'gemini':
                            # Takeout localizes the directories. _gemini
                            # retains only the Gemini records by their header.
                            selected = path.name.lower() == 'myactivity.json'
                        elif source == 'operit':
                            selected = path.suffix.lower() == '.json' and not path.name.startswith('._') and '__MACOSX' not in path.parts
                        else:
                            pattern = r'conversations(?:-\d+)?\.json' if source=='chatgpt' else r'conversations\.json'
                            selected = re.fullmatch(pattern,path.name) is not None
                        if not selected or member.is_dir():
                            continue
                        if path.is_absolute() or '..' in path.parts or '\\' in member.filename or member.flag_bits & 1:
                            raise ImportFormatError('导出压缩包包含不支持的路径或加密文件')
                        declared += member.file_size
                        if declared > max_json_bytes:
                            raise ImportFormatError('解压后的 JSON 总量超过 1 GiB 上限')
                        found = True
                        with archive.open(member) as member_stream:
                            with BufferedReader(JsonReader(member_stream,budget)) as document:
                                yield document
                    if not found:
                        raise ImportFormatError('ZIP 内没有找到所选来源的导出 JSON，请核对导出格式')
            except (BadZipFile,EOFError,RuntimeError,NotImplementedError,ZipDataError):
                raise ImportFormatError('无法读取 ZIP，请使用未加密的 JSON 压缩文件') from None
        else:
            raise ImportFormatError('请选择官方 JSON 导出或 ZIP 压缩包。Gemini 请在 Takeout 选择 JSON')


def gemini_days(documents, settings, temp_dir):
    # Takeout may mix years in any order. Keep sorting data on disk, never in
    # an all-history Python list. Only one business day's material is yielded.
    with TemporaryDirectory(prefix='gemini-',dir=temp_dir) as directory:
        with closing(sqlite3.connect(str(Path(directory)/'days.sqlite'))) as db:
            db.execute('PRAGMA cache_size=-2048')
            db.execute('PRAGMA temp_store=FILE')
            db.execute('PRAGMA journal_mode=OFF')
            db.execute('CREATE TABLE segments(day TEXT,instant REAL,ordinal INTEGER,payload TEXT,PRIMARY KEY(day,instant,ordinal)) WITHOUT ROWID')
            ordinal = 0
            for document in documents:
                for segment in _gemini(json_records(document),settings):
                    instant = datetime.fromisoformat(segment['timestamp'])
                    day = business_day_label(instant,settings).isoformat()
                    segment['segment_id'] = f'm-{ordinal}'
                    db.execute('INSERT INTO segments VALUES (?,?,?,?)',
                        (day,instant.timestamp(),ordinal,json.dumps(segment,ensure_ascii=False)))
                    ordinal += 1
            db.commit()
            for (day,) in db.execute('SELECT DISTINCT day FROM segments ORDER BY day'):
                segments = [json.loads(row[0]) for row in db.execute(
                    'SELECT payload FROM segments WHERE day=? ORDER BY instant,ordinal',(day,))]
                key = _hash('gemini-activity')
                yield ImportDay('gemini',key,'Gemini 活动记录',day,f'import:gemini:{key}:{day}',segments)


def _deepseek_object(document):
    if document.peek(3).startswith(b'\xef\xbb\xbf'):
        document.read(3)
    values = ijson.items(document, '', use_float=True)
    value = next(values, None)
    if not isinstance(value, dict) or next(values, None) is not None:
        raise ImportFormatError(DEEPSEEK_FORMAT_ERROR)
    return value


def deepseek_days(files, settings, max_json_bytes):
    budget = dict(used=0, limit=max_json_bytes)
    for filename, stream in files:
        if PurePosixPath(filename).suffix.lower() != '.zip':
            raise ImportFormatError('DeepSeek 聊天记录请上传完整的官方导出 ZIP')
        try:
            with ZipFile(stream) as archive:
                counts = Counter(member.filename for member in archive.infolist())

                @contextmanager
                def document(name):
                    if counts[name] != 1:
                        raise ImportFormatError(DEEPSEEK_FORMAT_ERROR)
                    member = archive.getinfo(name)
                    if member.flag_bits & 1:
                        raise ImportFormatError('请上传未加密的 DeepSeek 官方导出 ZIP')
                    if budget['used'] + member.file_size > budget['limit']:
                        raise ImportFormatError('解压后的 JSON 总量超过 1 GiB 上限')
                    with archive.open(member) as raw, BufferedReader(JsonReader(raw, budget)) as bounded:
                        yield bounded

                # Only the manifest, index and indexed session JSON are opened.
                # Account data and attachments are never read from the archive.
                # The manifest only marks a DeepSeek export. Its format version
                # is not checked: the structure checks below decide what is readable.
                with document('manifest.json') as manifest:
                    _deepseek_object(manifest)
                seen = set()
                with document('chat_session_index.json') as index:
                    for record in json_records(index):
                        key = indexed_session_id(record)
                        if key in seen:
                            raise ImportFormatError('DeepSeek 导出目录包含重复对话编号，请核对导出文件')
                        seen.add(key)
                        with document(f'chat_sessions/{key}.json') as session:
                            yield from session_days(_deepseek_object(session), key, settings)
        except (ijson.JSONError, UnicodeError, OverflowError, RecursionError):
            raise ImportFormatError(DEEPSEEK_FORMAT_ERROR) from None
        except (BadZipFile, EOFError, RuntimeError, NotImplementedError, ZipDataError):
            raise ImportFormatError('无法读取 DeepSeek 导出 ZIP，请核对文件是否完整') from None


def iter_upload(source, uploads, settings, *, max_bytes=MAX_UPLOAD_BYTES,
                max_json_bytes=MAX_JSON_BYTES, temp_dir=None):
    """Stream (filename, seekable binary file) pairs into one material at a time."""
    if source not in SOURCES:
        raise ImportFormatError('只支持 ChatGPT、Claude、Gemini、DeepSeek、Kelivo、Operit 导出或记忆文件')
    files = upload_files(source,uploads,max_bytes)
    seen = {}

    def items():
        if source == 'kelivo':
            yield from kelivo_days(files,settings,temp_dir)
            return
        if source == 'deepseek':
            yield from deepseek_days(files,settings,max_json_bytes)
            return
        streams = documents(source,files,max_json_bytes)
        if source == 'operit':
            yield from operit_days(streams,settings)
            return
        if source == 'gemini':
            yield from gemini_days(streams,settings,temp_dir)
            return
        for document in streams:
            if source == 'memory':
                yield from _memory(document)
                continue
            for record in json_records(document):
                conversation = _object(record)
                key = _identifier((conversation.get('id') or conversation.get('conversation_id'))
                    if source=='chatgpt' else (conversation.get('uuid') or conversation.get('id')))
                title = conversation.get('title') if source=='chatgpt' else (
                    conversation.get('name') or conversation.get('title'))
                if not isinstance(title,str) or not title.strip():
                    title = '未命名对话'
                segments = (_chatgpt if source=='chatgpt' else _claude)(conversation,settings)
                yield from _split(source,key,title,segments,settings)

    try:
        for item in items():
            digest = sha256(json.dumps(item.segments,ensure_ascii=False,sort_keys=True).encode()).digest()
            previous = seen.get(item.source_key)
            if previous is not None:
                if previous != digest:
                    raise ImportFormatError('同一来源同一天存在不同原文，请每次上传同一份完整导出')
                continue
            seen[item.source_key] = digest
            yield item
    except UnicodeError:
        raise ImportFormatError('导入文件必须使用 UTF-8 编码') from None
    if not seen:
        raise ImportFormatError('没有找到可导入的文字消息或记忆正文')
