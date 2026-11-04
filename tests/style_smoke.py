"""Native retrieval/learning checks, called by the complete ZIP smoke suite."""
from personal_context import PersonalContext, ContextIncomplete
from memseek.render import escape_untrusted
from personal_context.style import style_entity, style_entities_for_frame

SCOPE = dict(channel='chat', audience='friends', domain='personal', recipient='any', language='en')
FRAME = dict(task='Draft a casual invitation to friends.', actions=['communicate'], channels=['chat'],
             audiences=['friends'], domains=['personal'], recipients=['group:friends'], language='en')

async def test_style(mem, live):
    owner = 'style-test'; kit = PersonalContext(mem,owner)
    entity = style_entity(owner,SCOPE)
    content = dict(text='Use short conversational bursts and an occasional playful opening.',
        activation='learned', basis='Synthetic fixture only', evidence_ids=['synthetic'])
    await mem.records.ingest(entity=entity,collection='style_profiles',type='style',key='voice',text=content['text'],content=content)
    bound = await kit.prepare(frame=FRAME,flush=False)
    assert 'conversational bursts' in bound.content
    for change in [dict(language='fr'),dict(channels=['email'],domains=['work'],audiences=['client']),
                   dict(actions=['research'],channels=['web']),dict(language=None)]:
        other = await kit.prepare(frame={**FRAME,**change},flush=False)
        assert 'conversational bursts' not in other.content
    assert 'conversational bursts' not in (await PersonalContext(mem,'someone-else').prepare(frame=FRAME,flush=False)).content
    candidate = {**content,'activation':'candidate'}
    await mem.records.ingest(entity=entity,collection='style_profiles',type='style',key='voice',text=candidate['text'],content=candidate)
    assert 'conversational bursts' not in (await kit.prepare(frame=FRAME,flush=False)).content
    assert len((await mem.query_view('style_audit@1',style_entities=[entity]))['hits'])==1
    print('PASS: learned style routing by owner/channel/audience/domain/language; candidates and research excluded.')
    # A profile can exceed search-preview length while fitting the artifact budget.
    long_text = 'Short sentences. '*20 + 'MIDDLE: retain <all> & punctuation guidance. ' + 'Friendly closings. '*23
    assert 500 < len(long_text) <= 900
    long_content = {**content, 'text': long_text}
    await mem.records.ingest(entity=entity,collection='style_profiles',type='style',key='voice',text=long_text,content=long_content)
    complete = await kit.prepare(frame=FRAME,flush=False)
    assert escape_untrusted(long_text) in complete.content
    assert '[...] truncated [...]' not in complete.content
    snapshot = await mem.record(complete.snapshot_id)
    native = await mem.render_artifact('interaction_context@2', **snapshot['content']['parameters'])
    assert escape_untrusted(long_text) in native['rendered']
    assert not native['manifest']['truncated']
    # Two Unicode-heavy records exceed the block budget and must stop preparation.
    budget_owner = 'style-budget'
    for recipient in ['any', FRAME['recipients'][0]]:
        target = style_entity(budget_owner, {**SCOPE, 'recipient': recipient})
        heavy = {**content, 'text': '🙂'*900}
        await mem.records.ingest(entity=target,collection='style_profiles',type='style',key='voice',text=heavy['text'],content=heavy)
    try:
        await PersonalContext(mem,budget_owner).prepare(frame=FRAME,flush=False)
    except ContextIncomplete:
        pass
    else:
        raise AssertionError('Full-record budget overflow must stop preparation')
    print('PASS: complete long profiles via SDK/API, escaped text, and full-text overflow rejection.')
    if not live:
        return
    samples = []
    messages = ['Hey crew, game night? Bring your questionable tactics 😄',
        'Quick one: who is in for dinner?', 'Count me in. My calendar has surrendered 😂',
        'Hey legends, coffee this week?', 'Perfect, see you there!',
        'Two options: tacos or pizza. Democracy starts now 😄',
        'Who is up for a walk?', 'I am in! Snacks are my contribution to society.',
        'Hey crew, drinks later?', 'Booked. Zero planning skills required from you 😄',
        'Quick check: still good for tonight?', 'Yes please. My couch can survive without me.']
    for i,text in enumerate(messages):
        samples.append(dict(event_id=f'original-{i}',text=text,source='synthetic-owner-writing',
            timestamp=f'2026-09-{10+i//3:02}T12:{i:02}:00+00:00',authority='owner',origin='authored'))
    result = await kit.learn_style(scope=SCOPE,samples=samples)
    profile = result['profile']
    assert profile['content']['activation']=='learned',profile['content']
    assert set(profile['content']['evidence_ids']) <= {s['event_id'] for s in samples}
    assert profile['derived_from'], 'Missing native batch citation'
    assert (await kit.learn_style(scope=SCOPE,samples=samples)).get('unchanged')
    persisted = await PersonalContext(mem,owner).prepare(frame=FRAME,flush=False)
    assert profile['content']['text'] in persisted.content
    await kit.observe('For all my communications, be direct and playful. Avoid overengineered wording or pointless noise.',source='explicit-owner-preference')
    explicit = await kit.prepare(frame=FRAME,flush=False)
    assert 'playful' in explicit.content.lower()
    assert 'take precedence' in explicit.content
    # Replace the evidence with one example: the old learned profile must disappear.
    weak = await kit.learn_style(scope=SCOPE,samples=[samples[0]],replace=True)
    assert weak['profile']['content']['activation']=='candidate'
    assert profile['content']['text'] not in (await kit.prepare(frame=FRAME,flush=False)).content
    history = await mem.document_history(entity=entity,collection='style_profiles',key='voice')
    assert profile['id'] in str(history)
    print('PASS: REAL style derivation, exact sample references, native provenance, retry dedupe, persisted retrieval, explicit preference precedence, weak replacement and version history.')
