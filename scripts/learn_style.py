"""Learn a scoped style from prepared WhatsApp JSONL; facts/preferences use import_memory.py."""
import argparse
import asyncio
import json
from pathlib import Path
from personal_context import PersonalContext, connect
from personal_context.style import sample_window


def samples_from_whatsapp(rows, language, source=None):
    # Attribution was established by prepare_whatsapp's confirmed owner aliases.
    samples = []
    for row in rows:
        if row['authority'] != 'owner' or (source and row['source'] != source):
            continue
        data = json.loads(row['text'])
        if data.get('channel') != 'WhatsApp':
            raise ValueError('This adapter requires prepared WhatsApp rows')
        # A host must select a homogeneous language window; no model guesses here.
        message = data['message'].strip()
        if (len(message) > 2000 or message.startswith(('https://', 'http://', 'POLL:'))
                or 'IBAN' in message or 'BIC / SWIFT' in message
                or message.lower().endswith(('image omitted', 'video omitted', 'audio omitted'))):
            continue
        samples.append({'event_id': row['event_id'], 'text': message,
            'timestamp': data['timestamp'], 'source': row['source'],
            'authority': 'owner', 'origin': 'authored'})
    return sample_window(samples)


async def main(args):
    rows = [json.loads(line) for line in Path(args.path).read_text().splitlines() if line.strip()]
    samples = samples_from_whatsapp(rows, args.language, args.source)
    scope = dict(channel='chat', audience=args.audience, domain=args.domain,
                 recipient=args.recipient, language=args.language)
    async with connect() as mem:
        result = await PersonalContext(mem, args.owner).learn_style(scope=scope, samples=samples, replace=args.replace)
        profile = result.get('profile', {}).get('content', {})
        print(json.dumps({'entity': result['entity'], 'samples': result['samples'],
            'unchanged': result.get('unchanged', False), 'profile': profile}, indent=2))

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path'); parser.add_argument('--owner', required=True)
    parser.add_argument('--audience', required=True); parser.add_argument('--domain', required=True)
    parser.add_argument('--language', required=True, help='Confirmed language of the selected examples, e.g. en')
    parser.add_argument('--recipient', required=True, help='Canonical contact/group ID; any only for a deliberate audience-level profile')
    parser.add_argument('--source', help='Limit to one source/chat before learning')
    parser.add_argument('--replace', action='store_true')
    asyncio.run(main(parser.parse_args()))
