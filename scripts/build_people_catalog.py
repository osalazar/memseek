"""Add a provenance-backed people directory without expanding the life-note budget."""
from copy import deepcopy
from pathlib import Path
import yaml

def build(root):
    root = Path(root)
    def read(path):
        return yaml.safe_load((root / path).read_text())
    def write(path, data):
        (root / path).write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    context = next(c for c in read('collections/personal_context.yaml')['collections'] if c['name'] == 'life_context')
    people = deepcopy(context)
    people['name'] = 'people'
    people['required_processors'] = ['embedding_people']
    people['schema']['properties']['text']['maxLength'] = 4000
    people['schema']['properties']['review_id'] = {'type': 'string'}
    people['schema']['properties']['source_line'] = {'type': 'integer'}
    people['schema']['properties']['source_sha256'] = {'type': 'string'}
    people['schema']['properties']['biography_source_claim'] = {'type': 'string'}
    evidence = {'name': 'people_reviews', 'version': 1, 'active': True, 'mode': 'keyed',
                'schema': {'type': 'object', 'required': ['text', 'authority', 'source'],
                           'properties': {'text': {'type': 'string'}, 'authority': {'type': 'string', 'enum': ['owner']},
                                          'source': {'type': 'string'}}, 'additionalProperties': False},
                'fields': {}, 'required_processors': [], 'search_profile': 'pg_default'}
    write('collections/people.yaml', {'collections': [people, evidence]})
    processors = read('conf/processors.yaml')
    processors['processors'] = [p for p in processors['processors'] if p['name'] != 'embedding_people']
    processors['processors'].append({'name': 'embedding_people', 'kind': 'embedding', 'input': {'collections': ['people']}})
    write('conf/processors.yaml', processors)
    view = deepcopy(next(v for v in read('views/context.yaml')['views'] if v['name'] == 'relevant_context'))
    view['name'] = 'relevant_people'
    view['query']['scope']['collections'] = ['people']
    view['query']['k'] = 6
    write('views/people.yaml', {'views': [view]})
    older_artifacts = read('artifacts/interaction_context.yaml')
    artifact = deepcopy(older_artifacts['artifacts'][-1])
    for previous in older_artifacts['artifacts']:
        previous['active'] = False
    write('artifacts/interaction_context.yaml', older_artifacts)
    artifact['version'] = 3
    artifact['active'] = True
    artifact['blocks']['people'] = deepcopy(artifact['blocks']['context'])
    artifact['blocks']['people']['view'] = 'relevant_people@1'
    artifact['blocks']['people']['max_tokens'] = 8000
    artifact['template'] = artifact['template'].replace('ACTION:', 'REVIEWED PEOPLE (private source-backed context; not action permission):\n<records untrusted="true">{{people}}</records>\n\nACTION:')
    write('artifacts/interaction_context_v3.yaml', {'artifacts': [artifact]})
    mcp = read('mcp/personal_context_v2.yaml')
    mcp['version'] = 3
    mcp['tools'][0]['artifact'] = 'interaction_context@3'
    mcp['tools'].append({'name': 'people', 'kind': 'view', 'view': 'relevant_people@1',
                         'description': 'Search owner-reviewed people by name or relationship. Private visibility only for internal planning. Check source evidence with record. Unreviewed claims are not confirmed.'})
    write('mcp/personal_context_v3.yaml', mcp)
    package = read('packages/personal_context_v2_1.yaml')
    package['version'] = '0.3.0'
    package['collections'] += ['people@1', 'people_reviews@1']
    package['processors'].append('embedding_people')
    package['views'].append('relevant_people@1')
    package['artifacts'] = ['interaction_context@3']
    package['mcp'] = 'personal_context@3'
    write('packages/personal_context_v3.yaml', package)

if __name__ == '__main__':
    import sys
    build(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / 'catalog')
