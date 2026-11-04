import asyncio
import time
from personal_context import PersonalContext
from personal_context.friendship import infer_from_items, review_suggestion

async def test_friendship(mem):
    owner='synthetic-friendship'
    items=[{'id':'friend-a','name':'Synthetic Alex','line':1,'notes':'Friend-group plus joint project.','last':'Older summary'},
           {'id':'work','name':'Synthetic Work','line':2,'notes':'Daily meetings. Frequent work dinners.'},
           {'id':'ambiguous','name':'Synthetic Ambiguous','line':3,'notes':'Friend-group plus work; do not merge.'}]
    ids=await infer_from_items(mem,owner,items,'synthetic source','synthetic-hash')
    assert len(ids)==1
    assert ids==await infer_from_items(mem,owner,items,'synthetic source','synthetic-hash')
    query=lambda o,v:mem.query_view('friendship_suggestions@1',owner=o,visibility=v)
    assert len((await query(owner,['private']))['hits'])==1
    assert not (await query(owner,['shareable']))['hits']
    assert not (await query('other-owner',['private']))['hits']
    frame={'task':'Who are my close friends? Synthetic Alex?', 'actions':['research'],'channels':['web'],'audiences':['self'],'recipients':['none'],'domains':['personal']}
    kit=PersonalContext(mem,owner)
    assert 'UNCONFIRMED INFERENCE' not in (await kit.prepare(frame=frame,outward=False,flush=False)).content
    await review_suggestion(mem,owner,'friend-a','confirmed','Synthetic Alex is my close friend.')
    assert not (await query(owner,['private']))['hits']
    deadline=time.monotonic()+60
    people=(await mem.document(entity=owner,collections=['people']))['beliefs']
    while not (await mem.record(people[0]['id']))['ready']:
        assert time.monotonic()<deadline
        await asyncio.sleep(.3)
    assert 'Synthetic Alex is a close friend' in (await kit.prepare(frame=frame,outward=False,flush=False)).content
    assert not await infer_from_items(mem,owner,items,'synthetic source','synthetic-hash')
    await review_suggestion(mem,owner,'friend-a','rejected','Synthetic Alex is not a close friend; retract that relationship.')
    assert not (await mem.document(entity=owner,collections=['people']))['beliefs']
    assert not await infer_from_items(mem,owner,items,'synthetic source','synthetic-hash')
    assert not (await query(owner,['private']))['hits']
    print('PASS: friendship evidence threshold, ambiguity exclusion, idempotence, private isolation, explicit confirmation and revocation.')
