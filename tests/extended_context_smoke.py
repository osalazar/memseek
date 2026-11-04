import asyncio,time
from personal_context import PersonalContext

async def test_extended_context(mem):
    text='Private hotel preference: EXTENDED_PRIVATE_SUITE.'
    r=await mem.records.ingest(entity='synthetic-extended',collection='extended_context',type='context',key='hotel',text=text,
        content={'text':text,'kind':'fact','activation':'confirmed','visibility':'private','valid_until':253402300799})
    rid=r['inserted'][0]['id'];deadline=time.monotonic()+60
    while not (await mem.record(rid))['ready']:
        assert time.monotonic()<deadline
        await asyncio.sleep(.3)
    frame={'task':'What hotel room should I choose?','actions':['research'],'channels':['web'],'audiences':['self'],'recipients':['none'],'domains':['travel']}
    kit=PersonalContext(mem,'synthetic-extended')
    assert 'EXTENDED_PRIVATE_SUITE' in (await kit.prepare(frame=frame,outward=False,flush=False)).content
    assert 'EXTENDED_PRIVATE_SUITE' not in (await kit.prepare(frame=frame,outward=True,flush=False)).content
    assert 'EXTENDED_PRIVATE_SUITE' not in (await PersonalContext(mem,'another-owner').prepare(frame=frame,outward=False,flush=False)).content
    print('PASS: extended context retrieved privately, excluded from outward context, and isolated by owner.')
