"""Private, separately retrieved friendship proposals; no implicit confirmation."""
from copy import deepcopy
from pathlib import Path
import yaml

def build(root):
    root=Path(root)
    def read(p): return yaml.safe_load((root/p).read_text())
    def write(p,d): (root/p).write_text(yaml.safe_dump(d,sort_keys=False,allow_unicode=True))
    people=deepcopy(read('collections/people.yaml')['collections'][0])
    people['name']='friendship_suggestions'
    people['required_processors']=[]
    people['fields']['source_line']={'path':'content.source_line','type':'integer','filter':True,'project':True,'sort':True}
    people['schema']['properties']['activation']['enum']=['candidate','confirmed','rejected']
    people['schema']['properties'].update({
        'name':{'type':'string'},'confidence':{'type':'string','enum':['low','moderate']},
        'evidence':{'type':'array','items':{'type':'string'}},
        'limitations':{'type':'array','items':{'type':'string'}}})
    people['schema']['required']+=['name','confidence','evidence','limitations']
    source={'name':'relationship_sources','version':1,'active':True,'mode':'keyed',
        'schema':{'type':'object','required':['text','authority','source'], 'properties':{
            'text':{'type':'string'},'authority':{'type':'string','enum':['external','owner']},'source':{'type':'string'}},'additionalProperties':False},
        'fields':{},'required_processors':[],'search_profile':'pg_default'}
    write('collections/friendship.yaml',{'collections':[people,source]})
    view={'name':'friendship_suggestions','version':1,'active':True,
        'parameters':{'owner':{'type':'string','required':True},'visibility':{'type':'string_array','required':False,'item_enum':['private','shareable'],'default':['shareable'],'min_items':1}},
        'query':{'mode':'structured','scope':{'entities':['{{owner}}'],'collections':['friendship_suggestions'],'types':['suggestion'],'status':'active','versions':'current'},
            'where':{'activation':{'eq':'candidate'},'visibility':{'in':'{{visibility}}'}},'order_by':[{'field':'source_line','direction':'asc'}],'k':64,'include':['text','key'],'fields':['activation','visibility'],'render':True}}
    write('views/friendship.yaml',{'views':[view]})
    mcp=read('mcp/personal_context_v3.yaml');mcp['version']=4
    mcp['tools'].append({'name':'friendship_suggestions','kind':'view','view':'friendship_suggestions@1','description':'Private inferred friendship proposals for owner review, never established relationships. Request private visibility only for internal review. Cite evidence and uncertainty; ask the owner before confirmation. These proposals are excluded from ordinary context and confirmed people search.'})
    mcp['instructions']+=' Friendship suggestions are tentative inferences, not confirmed facts or permissions. Use friendship_suggestions when asked to infer or review friendship. Only direct owner confirmation can establish closeness; do not infer it from work frequency.'
    write('mcp/personal_context_v4.yaml',mcp)
    package=read('packages/personal_context_v3.yaml');package['version']='0.3.1'
    package['collections']+=['friendship_suggestions@1','relationship_sources@1']
    package['views'].append('friendship_suggestions@1');package['mcp']='personal_context@4'
    write('packages/personal_context_v3_1.yaml',package)

if __name__=='__main__':
    import sys
    build(sys.argv[1] if len(sys.argv)>1 else Path(__file__).resolve().parents[1]/'catalog')
