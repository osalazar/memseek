"""Focused source-completeness regressions, independent of token-budget overflow."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from memseek.artifacts import resolve_artifact_blocks
from memseek.search.engine import SearchRequestError
from memseek.search.pagination import page_arguments


class PaginationArguments(unittest.TestCase):
    def test_reject_invalid_and_unversioned_continuations(self):
        for arguments in ({'offset': 1}, {'offset': -1}, {'limit': True}, {'limit': 0},
                          {'limit': 1001}, {'snapshot': None}, {'snapshot': 'not-a-snapshot'}):
            with self.subTest(arguments=arguments), self.assertRaises(SearchRequestError):
                page_arguments(arguments)


class SourceCompleteness(unittest.IsolatedAsyncioTestCase):
    async def resolve(self, **page):
        now = datetime.now(timezone.utc)
        row = dict(id=uuid4(), occurred_at=now, collection='preferences', type='preference',
                   key='one', scores={}, content={'text': 'Never disclose private reasons.'}, enriched_at=now)
        cursor = SimpleNamespace(fetchall=AsyncMock(return_value=[row]))
        connection = SimpleNamespace(execute=AsyncMock(return_value=cursor))
        @asynccontextmanager
        async def connect():
            yield connection
        pool = SimpleNamespace(connection=connect)
        block = SimpleNamespace(document=None, view='hard_rules@1', args={}, max_tokens=2000, required=True)
        artifact = SimpleNamespace(name='test', version=1, parameters={}, blocks={'hard':block}, template='{{hard}}')
        view = SimpleNamespace(name='hard_rules', version=1, definition_hash='test', kind='search',
                               query={'scope':{'collections':['preferences']}})
        catalog = SimpleNamespace(resolve_view=lambda *args: view, collections={('preferences',1):None}, processors={})
        result = {'hits':[{'id':str(row['id'])}], **page}
        with patch('memseek.artifacts.execute_view', AsyncMock(return_value=result)), \
             patch('memseek.artifacts._scope_max_seq', AsyncMock(return_value=1)), \
             patch('memseek.artifacts.package_ref', return_value=None), \
             patch('memseek.artifacts._resolve_learning_target', return_value=None):
            return await resolve_artifact_blocks(pool, workspace='test', artifact=artifact,
                parameters={}, catalog=catalog,
                settings=SimpleNamespace(max_artifact_input_records=255, max_artifact_render_tokens=50000))

    async def test_partial_source_is_flagged_even_when_all_returned_records_fit(self):
        result = await self.resolve(has_more=True, offset=0)
        self.assertEqual(len(result.input_ids), 1)
        self.assertIn('Never disclose', result.rendered)
        self.assertTrue(result.truncated)
        self.assertTrue(result.blocks[0].truncated)

    async def test_last_page_alone_is_not_the_entire_source(self):
        result = await self.resolve(has_more=False, offset=100)
        self.assertTrue(result.truncated)

    async def test_render_preview_overflow_does_not_discard_complete_hit_records(self):
        result = await self.resolve(has_more=False, offset=0, rendered_truncated=True, truncated=True)
        self.assertFalse(result.truncated)
        self.assertIn('Never disclose', result.rendered)


if __name__ == '__main__':
    unittest.main()
