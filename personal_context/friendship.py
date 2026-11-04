"""Conservative proposals from curated notes, not a measure of personal intimacy.

The caller must have permission to process the source and supply its real provenance.
This first version recognizes explicit friend-group mentions only. It never treats
work frequency, a shared calendar, or a contact count as evidence of closeness.
"""
import json
import re

def proposal(item):
    notes=item.get('notes','')
    # Narrow positive source language; ambiguous/negative identity stays excluded.
    positive=re.search(r'(?i)(?:^friend-group plus\b|\bfriend-group on Gmail\b)',notes)
    if not positive or re.search(r'(?i)\b(?:not friends|not a friend|do not merge|may be same|unconfirmed identity)\b',notes):
        return None
    limitations=['Source is a curated summary, not original messages.',
                 'Friend-group membership does not establish closeness.',
                 'Mutual outreach, sustained personal contact and emotional closeness are not established.',
                 'The source entry has not been endorsed as a close friendship.']
    if item.get('last'): limitations.append('Source activity reference: '+item['last']+'; recency is not live-verified.')
    return {'name':item['name'],'confidence':'low','evidence':[notes], 'limitations':limitations}

async def infer_from_items(mem,owner,items,source,sha256):
    current={b['key']:await mem.record(b['id']) for b in (await mem.document(entity=owner,collections=['friendship_suggestions']))['beliefs']}
    proposed=[]
    for item in items:
        p=proposal(item)
        if not p: continue
        key=item['id']
        previous=current.get(key)
        # A rerun cannot undo an owner decision or create duplicate current proposals.
        if previous and previous['content']['activation']!='candidate': continue
        text=(f"Possible close friend — {p['name']}. UNCONFIRMED INFERENCE; low confidence. "
              f"Evidence in {source}, line {item['line']}: {item['notes']} "
              +'Limitations: '+' '.join(p['limitations'])+' Owner review required; no action permission.')
        content={'text':text,'kind':'relationship','activation':'candidate','visibility':'private','valid_until':253402300799,
                 'review_id':key,'source_line':item['line'],'source_sha256':sha256,**p}
        if previous and previous['content']==content:
            proposed.append(previous['id']); continue
        evidence=json.dumps({'name':item['name'],'notes':item['notes'],'activity_reference':item.get('last',''),'sha256':sha256,'line':item['line']},ensure_ascii=False)
        src=await mem.records.ingest(entity=owner,collection='relationship_sources',type='source',key=key,
            text=evidence,content={'text':evidence,'authority':'external','source':source})
        result=await mem.records.ingest(entity=owner,collection='friendship_suggestions',type='suggestion',key=key,
            text=text,content=content,derived_from=[src['inserted'][0]['id']])
        proposed.append(result['inserted'][0]['id'])
    return proposed

async def review_suggestion(mem,owner,key,decision,owner_words):
    """Trusted caller must establish direct owner confirmation/rejection in context."""
    if decision not in {'confirmed','rejected'} or not owner_words.strip():
        raise ValueError('Explicit owner decision and wording required')
    rows=(await mem.document(entity=owner,collections=['friendship_suggestions']))['beliefs']
    record=await mem.record(next(b['id'] for b in rows if b['key']==key))
    c=record['content']
    review=await mem.records.ingest(entity=owner,collection='people_reviews',type='review',key='friendship:'+key,
        text=owner_words,content={'text':owner_words,'authority':'owner','source':'Direct owner friendship review; trusted caller attribution'})
    evidence_id=review['inserted'][0]['id']
    if decision=='confirmed':
        text=f"{c['name']} is a close friend of {owner}, explicitly confirmed by the owner. Private context; no action permission."
        await mem.records.ingest(entity=owner,collection='people',type='context',key='friendship:'+key,text=text,
            content={'text':text,'kind':'relationship','activation':'confirmed','visibility':'private','valid_until':253402300799},derived_from=[evidence_id])
    else:
        existing=(await mem.document(entity=owner,collections=['people']))['beliefs']
        if any(b['key']=='friendship:'+key for b in existing):
            await mem.records.ingest(entity=owner,collection='people',type='context',key='friendship:'+key,tombstone=True,derived_from=[evidence_id])
    updated={**c,'activation':decision,'text':f"Owner {decision} close-friend suggestion for {c['name']}. Owner wording: {owner_words}"}
    await mem.records.ingest(entity=owner,collection='friendship_suggestions',type='suggestion',key=key,text=updated['text'],content=updated,derived_from=[evidence_id])
