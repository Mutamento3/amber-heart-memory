"""No whole-file reads, independent limits and disk-backed Gemini day sorting."""

from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from memory_core.storage.config import Settings
from memory_core.imports.import_formats import ImportFormatError, parse_upload
from memory_core.imports.import_stream import iter_upload
from memory_core.tests.test_import_formats import claude, uploaded, packed, gemini


class BoundedFile(BytesIO):
    def read(self,size=-1):
        if size<0 or size>65536:
            raise AssertionError('unbounded read')
        return super().read(size)


class ImportStreamTests(unittest.TestCase):
    def test_json_uses_bounded_reads_and_yields_before_reaching_end(self):
        records=[claude(key='synthetic-'+str(n)) for n in range(1000)]
        data=json.dumps(records).encode()
        stream=BoundedFile(data)
        items=iter_upload('claude',[('conversations.json',stream)],Settings(3))
        first=next(items)
        self.assertEqual(first.message_count,2)
        self.assertLess(stream.tell(),len(data))
        self.assertEqual(1+sum(1 for _ in items),1000)

    def test_zip_opens_members_without_reading_them_whole(self):
        files=packed(uploaded([claude()]))
        with patch.object(ZipFile,'read',side_effect=AssertionError('archive.read forbidden')):
            items=list(iter_upload('claude',files,Settings(3)))
        self.assertEqual(len(items),1)

    def test_upload_and_json_limits_are_independent_and_total_across_shards(self):
        records=[claude(key='synthetic-'+str(n)) for n in range(30)]
        files=packed(uploaded(records))
        expanded=len(uploaded(records)[0][1])
        self.assertLess(len(files[0][1]),expanded)
        self.assertEqual(len(list(iter_upload('claude',files,Settings(3),
            max_bytes=len(files[0][1]),max_json_bytes=expanded))),30)
        with self.assertRaisesRegex(ImportFormatError,'上限'):
            list(iter_upload('claude',files,Settings(3),max_json_bytes=expanded-1))
        with self.assertRaisesRegex(ImportFormatError,'上限'):
            list(iter_upload('claude',files,Settings(3),max_bytes=len(files[0][1])-1))
        shards=packed(uploaded(records[:15],'conversations-000.json')+
            uploaded(records[15:],'conversations-001.json'))
        with self.assertRaisesRegex(ImportFormatError,'上限'):
            list(iter_upload('chatgpt',shards,Settings(3),max_json_bytes=10))

    def test_actual_byte_limit_catches_direct_json_and_malformed_tail(self):
        data=uploaded([claude()])[0][1]
        with self.assertRaisesRegex(ImportFormatError,'上限'):
            list(iter_upload('claude',[('conversations.json',BoundedFile(data))],Settings(3),max_json_bytes=10))
        with self.assertRaises(ImportFormatError):
            list(iter_upload('claude',[('conversations.json',BytesIO(data+b'not-json'))],Settings(3)))

    def test_gemini_mixed_order_and_gems_group_by_day_without_metadata(self):
        records=gemini()
        extra=dict(records[0],title='Prompted 合成 Gem 聊天。',time='2025-03-14T19:01:00Z',
            subtitles=[dict(name='Synthetic Gem was used in this chat. Manage your Gems.',value='GEM_METADATA_ONLY')])
        records.insert(1,extra)
        with TemporaryDirectory() as scratch:
            items=list(iter_upload('gemini',uploaded(records),Settings(3),temp_dir=scratch))
            self.assertEqual(list(Path(scratch).iterdir()),[])
        self.assertEqual([item.business_date for item in items],['2025-03-14','2025-03-15'])
        self.assertEqual(len(items[1].segments),4)
        self.assertIn('合成 Gem 聊天。',[s['text'] for s in items[1].segments])
        self.assertNotIn('GEM_METADATA_ONLY',json.dumps([s for item in items for s in item.segments]))

    def test_duplicate_streamed_sources_still_skip_or_reject_conflicts(self):
        records=[claude(),claude()]
        self.assertEqual(len(parse_upload('claude',uploaded(records),Settings(3))),1)
        records[1]['chat_messages'][0]['text']='合成改变的原文。'
        with self.assertRaisesRegex(ImportFormatError,'不同原文'):
            list(iter_upload('claude',uploaded(records),Settings(3)))
