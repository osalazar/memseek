"""Real PostgreSQL, REST and native MCP pagination regressions. No model calls."""
import asyncio
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

import httpx
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from memseek.auth import create_workspace
from memseek.config import Settings
from memseek.db import pool_lifespan
from memseek.sdk import MemseekClient
from smoke import pref, FRAME
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from audit_preferences import read_all, request, NoRedirect, build_opener


def payload(result):
    assert not result.is_error, str(result)
    return result.structured_content or json.loads(next(c.text for c in result.content if c.type == 'text'))


async def main():
    url = os.environ.get('MEMSEEK_URL', 'http://api:8000')
    workspace = 'paging-' + uuid4().hex[:12]
    async with pool_lifespan(Settings()) as pool:
        credential = await create_workspace(pool, workspace)
        other = await create_workspace(pool, workspace + '-other')
    headers = {'Authorization': 'Bearer ' + credential.api_key}
    async with httpx.AsyncClient(base_url=url, headers=headers, timeout=180) as http:
        async with MemseekClient(url, credential.api_key) as mem:
            await mem.catalog.publish(package='personal_context@0.2.1', directory='/app/catalog')
            async with MemseekClient(url, other.api_key) as separate:
                await separate.catalog.publish(package='personal_context@0.2.1', directory='/app/catalog')
            fixtures = []
            for owner, hard, soft in [('oscar-repro', 207, 19), ('large', 1207, 19)]:
                fixtures += [dict(entity=owner, **pref(f'hard-{i}', f'Hard rule {i}.', strength='hard')) for i in range(hard)]
                fixtures += [dict(entity=owner, **pref(f'soft-{i}', f'Soft rule {i}.')) for i in range(soft)]
            fixtures += [dict(entity='filters', **pref('active', 'Applies.', strength='hard')),
                         dict(entity='filters', **pref('candidate', 'Unconfirmed.', strength='hard', activation='candidate')),
                         dict(entity='filters', **pref('expired', 'Expired.', strength='hard', valid_until=1)),
                         dict(entity='unrelated-owner', **pref('secret', 'OTHER_OWNER_MARKER', strength='hard'))]
            for i in range(0, len(fixtures), 100):
                await mem.records.ingest_many(fixtures[i:i+100])
            print('Seeded isolated synthetic workspace:', workspace, flush=True)
            fixed_now = int(time.time())
            async with httpx2.AsyncClient(headers=headers) as transport:
                async with Client(streamable_http_client(url+'/mcp', http_client=transport, terminate_on_close=False)) as client:
                    tools = (await client.list_tools()).tools
                    for name in ('hard_rules', 'preferences'):
                        schema = next(t.input_schema for t in tools if t.name == name)
                        assert schema['properties']['limit']['default'] == 500
                        assert schema['properties']['limit']['maximum'] == 1000
                        assert {'offset', 'snapshot'} <= schema['properties'].keys()
                    for name, count in [('hard_rules', 207), ('preferences', 226)]:
                        args = {'owner': 'oscar-repro', **({'now': fixed_now} if name == 'hard_rules' else {})}
                        result = payload(await client.call_tool(name, args))
                        assert result['total_count'] == result['count'] == count
                        assert result['has_more'] is False and result['next_offset'] is None
                        assert len({h['id'] for h in result['hits']}) == count
                    print('PASS: Oscar reproduction returns all 207 hard rules and 226 preferences by default.', flush=True)
                    for name, count in [('hard_rules', 1207), ('preferences', 1226)]:
                        args = {'owner': 'large', 'limit': 200, **({'now': fixed_now} if name == 'hard_rules' else {})}
                        ids = []
                        snapshot = None
                        while True:
                            result = payload(await client.call_tool(name, args))
                            snapshot = snapshot or result['snapshot']
                            assert result['snapshot'] == snapshot
                            assert result['total_count'] == count and result['count'] <= 200
                            assert result['offset'] == len(ids)
                            assert [h['rank'] for h in result['hits']] == list(range(len(ids)+1, len(ids)+result['count']+1))
                            ids += [h['id'] for h in result['hits']]
                            if not result['has_more']:
                                assert result['next_offset'] is None
                                break
                            assert result['truncated'] and result['next_offset'] == len(ids)
                            args.update(offset=result['next_offset'], snapshot=snapshot)
                        assert len(ids) == len(set(ids)) == count
                        # Boundary beyond the old 1,000-candidate ceiling; changing page size is valid.
                        again = payload(await client.call_tool(name, {**args, 'offset': 1000, 'limit': 1000}))
                        assert [h['id'] for h in again['hits']] == ids[1000:]
                        print(f'PASS: {name} paginates {count} unique records with stable ties and exact totals.', flush=True)
                    maximum = payload(await client.call_tool('preferences', {'owner':'large','limit':1000}))
                    assert maximum['count'] == 1000 and maximum['total_count'] == 1226 and maximum['has_more']
                    default = payload(await client.call_tool('preferences', {'owner':'large'}))
                    assert default['count'] == 500 and default['total_count'] == 1226 and default['has_more']
                    filters = payload(await client.call_tool('hard_rules', {'owner':'filters','now':fixed_now}))
                    assert filters['total_count'] == 1 and filters['hits'][0]['key'] == 'active'
                    empty = payload(await client.call_tool('preferences', {'owner':'absent'}))
                    assert empty['hits'] == [] and empty['total_count'] == 0 and empty['next_offset'] is None
                    first = payload(await client.call_tool('preferences', {'owner':'oscar-repro','limit':100}))
                    for args in ({'limit':0}, {'limit':1001}, {'offset':-1}, {'offset':1}, {'snapshot':'invalid'}):
                        response = await http.post('/views/preference_audit@1/query', json={'owner':'oscar-repro', **args})
                        assert response.status_code == 422, response.text
                    mismatch = await http.post('/views/preference_audit@1/query', json={'owner':'large','offset':100,'snapshot':first['snapshot']})
                    assert mismatch.status_code == 422 and mismatch.json()['error'] == 'page_changed'
                    # Same count, different record: the fingerprint must still reject continuation.
                    await mem.records.ingest(entity='oscar-repro', **pref('hard-0','Corrected rule.',strength='hard'))
                    changed = await http.post('/views/preference_audit@1/query', json={'owner':'oscar-repro','offset':100,'snapshot':first['snapshot']})
                    assert changed.status_code == 422 and changed.json()['error'] == 'page_changed', changed.text
                    refreshed = payload(await client.call_tool('preferences', {'owner':'oscar-repro'}))
                    assert refreshed['total_count'] == 226 and 'Corrected rule.' in str(refreshed['hits'])
                    async with MemseekClient(url, other.api_key) as separate:
                        isolated = await separate.query_view('preference_audit@1', owner='oscar-repro')
                        assert isolated['total_count'] == 0
                    print('PASS: validation, empty reads, owner/workspace isolation, candidate/expiry filtering and same-count correction detection.', flush=True)
                    context = payload(await client.call_tool('context', {**FRAME,'owner':'large','now':fixed_now}))
                    assert context['manifest']['truncated'], 'Partial source must never appear complete in context'
                    print('PASS: oversized context is explicitly truncated, never silently complete.', flush=True)
            # Array filtering must happen before count and pagination, even past candidate caps.
            await mem.records.ingest(entity='large', **pref('late-match','Late scoped rule.', audiences=['client']))
            scoped = await mem.query_view('applicable_preferences@1', owner='large', now=fixed_now,
                actions=['communicate'],channels=['email'],audiences=['client'],recipients=['contact:sarah'],domains=['work'])
            # Existing soft defaults [any] do not match these explicit-only arrays.
            assert scoped['total_count'] == 0
            scoped = await mem.query_view('applicable_preferences@1', owner='large', now=fixed_now,
                actions=['any','communicate'],channels=['any','email'],audiences=['client'],recipients=['any','contact:sarah'],domains=['any','work'])
            assert scoped['total_count'] == 1 and scoped['hits'][0]['key'] == 'late-match'
            print('PASS: exact array predicates and totals beyond ranked candidate limits.', flush=True)
            long_text = 'Scoped original style guidance. ' * 23
            await mem.records.ingest(entity='paging-style', collection='style_profiles', type='style', key='voice',
                text=long_text, content={'text':long_text,'activation':'learned','basis':'Synthetic test','evidence_ids':['fixture']})
            styles = await mem.query_view('style_audit@1', style_entities=['paging-style'])
            assert long_text == styles['hits'][0]['text'] and long_text in styles['rendered']
            opener = build_opener(NoRedirect())
            def call(path, data=None):
                return request(opener, url, credential.api_key, path, data)
            report = await asyncio.to_thread(read_all, call, 'preference_audit@1', {'owner':'oscar-repro'}, 100)
            assert report['complete'] and report['unique_records'] == 226 and report['pages'] == 3
            print('PASS: full long text in structured hits/rendering and the shipped read-only audit helper.', flush=True)
    print('PAGINATION TESTS PASSED. No provider calls or real memories used.', flush=True)


if __name__ == '__main__':
    asyncio.run(main())
