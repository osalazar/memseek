"""Exercise generated native MCP schemas and calls, not a custom bridge."""
import time
import json
from memseek.render import escape_untrusted
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from personal_context.client import wait_run
from personal_context.style import style_entity, style_entities_for_frame
from style_smoke import SCOPE, FRAME

async def test_mcp_style(mem, url, key, live):
    entity=style_entity('mcp-test',SCOPE)
    async with httpx2.AsyncClient(headers={'Authorization':'Bearer '+key}) as http:
        async with Client(streamable_http_client(url+'/mcp',http_client=http,terminate_on_close=False)) as client:
            result=await client.list_tools()
            tools=result.tools if hasattr(result,'tools') else result
            assert {t.name for t in tools}=={'context','hard_rules','preferences','remember','record','learn_style','styles','people','friendship_suggestions','extended_context'}
            if live:
                result=await client.call_tool('learn_style',dict(entity=entity,type='style_batch',text='Owner-authored communication examples',
                    content={'text':'Owner-authored communication examples','scope':SCOPE,
                    'samples':[dict(event_id='mcp-owner-1',text='Yep, see you soon!',timestamp='2026-09-01T12:00:00Z',
                        source='synthetic',authority='owner',origin='authored')]}))
                assert not result.is_error,result
                await wait_run(mem,(await mem.run_processor('maintain_style',entity=entity))['job_id'])
                doc=await mem.document(entity=entity,collections=['style_profiles'])
                assert len(doc['beliefs'])==1
                record=await mem.record(doc['beliefs'][0]['id'])
                assert record['content']['activation']=='candidate'
                assert not (await client.call_tool('record',{'id':record['id']})).is_error
            assert not (await client.call_tool('styles',{'style_entities':[entity]})).is_error
            params={k:v for k,v in FRAME.items() if k!='language'}
            params.update(owner='mcp-test',now=int(time.time()),visibility=['shareable'],
                          style_entities=style_entities_for_frame('mcp-test',FRAME))
            context=await client.call_tool('context',params)
            assert not context.is_error,context
            long_text='Short messages. '*25+'MIDDLE: <retain> & full profile. '+'Clear endings. '*25
            assert 500 < len(long_text) <= 900
            data=dict(text=long_text,activation='learned',basis='Synthetic full-text regression',evidence_ids=['fixture'])
            await mem.records.ingest(entity=entity,collection='style_profiles',type='style',key='voice',text=long_text,content=data)
            full=await client.call_tool('context',params)
            assert not full.is_error,full
            payload=full.structured_content or json.loads(next(c.text for c in full.content if c.type=='text'))
            assert escape_untrusted(long_text) in payload['rendered']
            assert not payload['manifest']['truncated']
            assert '[...] truncated [...]' not in payload['rendered']
    print('PASS: native HTTP MCP ten-tool schema, style ingest/derivation/audit/record/context calls.' if live else 'PASS: native HTTP MCP ten-tool schema and style audit/context calls.')
