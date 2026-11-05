"""Exercise review status, private visibility, identity separation and revisions."""
import asyncio
import time
from personal_context import PersonalContext

async def test_people(mem):
    owner = 'synthetic-people'
    records = []
    for key, activation, text in [
        ('alex-a', 'confirmed', 'Alex A is an executive coach. PRIVATE_PEOPLE_MARKER.'),
        ('alex-b', 'candidate', 'Alex B is an unconfirmed separate identity. UNCONFIRMED_MARKER.')]:
        r = await mem.records.ingest(entity=owner, collection='people', type='context', key=key,
            text=text, content={'text': text, 'kind': 'relationship', 'activation': activation,
                                'visibility': 'private', 'valid_until': 253402300799})
        records.append(r['inserted'][0]['id'])
    deadline = time.monotonic() + 60
    for rid in records:
        while not (await mem.record(rid))['ready']:
            assert time.monotonic() < deadline
            await asyncio.sleep(.3)
    frame = {'task': 'Who is Alex?', 'actions': ['research'],
             'channels': ['web'], 'audiences': ['self'], 'recipients': ['none'], 'domains': ['work']}
    kit = PersonalContext(mem, owner)
    internal = await kit.prepare(frame=frame, outward=False, flush=False)
    assert 'PRIVATE_PEOPLE_MARKER' in internal.content
    assert 'UNCONFIRMED_MARKER' not in internal.content
    assert 'PRIVATE_PEOPLE_MARKER' not in (await kit.prepare(frame=frame, outward=True, flush=False)).content
    assert 'PRIVATE_PEOPLE_MARKER' not in (await PersonalContext(mem, 'different-person').prepare(frame=frame, outward=False, flush=False)).content
    text = 'Alex A is a leadership coach. CORRECTED_PEOPLE_MARKER.'
    r = await mem.records.ingest(entity=owner, collection='people', type='context', key='alex-a',
        text=text, content={'text': text, 'kind': 'relationship', 'activation': 'confirmed',
                            'visibility': 'private', 'valid_until': 253402300799})
    while not (await mem.record(r['inserted'][0]['id']))['ready']:
        assert time.monotonic() < deadline
        await asyncio.sleep(.3)
    result = await kit.prepare(frame=frame, outward=False, flush=False)
    assert 'CORRECTED_PEOPLE_MARKER' in result.content and 'PRIVATE_PEOPLE_MARKER' not in result.content
    assert len((await mem.document(entity=owner, collections=['people']))['beliefs']) == 2
    print('PASS: reviewed people retrieval, unconfirmed exclusion, private visibility, owner isolation and keyed correction.')
