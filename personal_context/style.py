"""Trusted routing and evidence preparation; Memseek owns learning, storage and retrieval."""
import hashlib
import json
import re
from datetime import datetime

SCOPE_FIELDS = ('channel', 'audience', 'domain', 'recipient', 'language')


def normalize_scope(scope):
    if set(scope) != set(SCOPE_FIELDS):
        raise ValueError('Style scope requires channel, audience, domain, recipient and language')
    result = {}
    for field in SCOPE_FIELDS:
        value = scope[field]
        if not isinstance(value, str) or not re.fullmatch(r'[a-z0-9][a-z0-9:._-]{0,79}', value):
            raise ValueError(f'Invalid style {field}; use canonical lowercase labels')
        if value in {'unknown', 'none', 'any'} and not (field == 'recipient' and value == 'any'):
            raise ValueError(f'Style {field} must be known; no inferred global scope')
        result[field] = value
    return result


def style_entity(owner, scope):
    """Stable, owner-bound entity: the model cannot alter a derivation's target entity."""
    if not isinstance(owner, str) or not owner.strip():
        raise ValueError('An owner is required')
    canonical = json.dumps([owner, normalize_scope(scope)], sort_keys=True, separators=(',', ':'))
    return 'style:' + hashlib.sha256(canonical.encode()).hexdigest()


def style_entities_for_frame(owner, frame):
    # Missing language means no learned style. Explicit preferences still apply.
    if not ({'communicate', 'call', 'follow_up'} & set(frame.get('actions', [])) or 'email' in frame.get('channels', [])):
        return ['__no_style__']
    language = frame.get('language')
    if not language:
        return ['__no_style__']
    values = {}
    for single, plural in [('channel', 'channels'), ('audience', 'audiences'),
                           ('domain', 'domains'), ('recipient', 'recipients')]:
        labels = set(frame.get(plural, [])) - {'any'}
        # Remove known parent labels without choosing between unrelated audiences.
        if single == 'audience' and labels & {'friends', 'family', 'client'}:
            labels.discard('external')
        if len(labels) != 1 or 'unknown' in labels:
            return ['__no_style__']
        values[single] = next(iter(labels))
    try:
        scope = normalize_scope({**values, 'language': language})
        return list(dict.fromkeys([style_entity(owner, {**scope, 'recipient': 'any'}),
                                   style_entity(owner, scope)]))
    except ValueError:
        return ['__no_style__']


def sample_window(samples):
    """Validate before any write; retries and copied duplicates add no extra evidence."""
    deduped = {}
    for sample in samples:
        if set(sample) != {'event_id', 'text', 'timestamp', 'source', 'authority', 'origin'}:
            raise ValueError('Each sample requires exact provenance fields; do not infer authorship')
        if sample['authority'] != 'owner' or sample['origin'] not in {'authored', 'owner_edit'}:
            raise ValueError('Only verified owner-authored wording/edits may train style')
        for field, limit in [('event_id', 200), ('text', 2000), ('timestamp', 40), ('source', 200)]:
            if not isinstance(sample[field], str) or not sample[field].strip() or len(sample[field]) > limit:
                raise ValueError(f'Invalid sample {field}')
        moment = datetime.fromisoformat(sample['timestamp'].replace('Z', '+00:00'))
        if moment.tzinfo is None:
            raise ValueError('Sample timestamp must have an explicit timezone')
        if sample['event_id'] in deduped and sample != deduped[sample['event_id']]:
            raise ValueError('Conflicting content for the same source event ID')
        deduped[sample['event_id']] = dict(sample)
    recent, seen_text, used = [], set(), 0
    for sample in sorted(deduped.values(), key=lambda s: datetime.fromisoformat(s['timestamp'].replace('Z', '+00:00')), reverse=True):
        text_key = ' '.join(sample['text'].split()).casefold()
        if text_key in seen_text:
            continue
        size = len(json.dumps(sample, ensure_ascii=False))
        if len(recent) >= 48 or used + size > 30000:
            break
        seen_text.add(text_key); used += size; recent.append(sample)
    if not recent:
        raise ValueError('No original owner examples in this scope')
    return list(reversed(recent))
