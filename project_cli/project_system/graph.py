from pathlib import Path
from collections import defaultdict, deque
from .frontmatter import read_object
from .utils import ID_RE

RELATION_KEYS={'depends_on','supersedes','requirements','flows','screens','blocks','affects','related','relationships','caused_by'}
SINGLE_RELATION_KEYS={'introduced_by','deprecated_by','resolved_by','feature','metric'}

def load_objects(root):
    out={}
    for p in Path(root).glob('knowledge/**/*.md'):
        try: data,_=read_object(p)
        except Exception: continue
        if data.get('id'): out[data['id']]={'data':data,'path':p}
    return out

def extract_refs(data):
    refs=[]
    for k,v in data.items():
        if k in RELATION_KEYS and isinstance(v,list): refs.extend(x for x in v if isinstance(x,str) and ID_RE.match(x))
        elif k in SINGLE_RELATION_KEYS and isinstance(v,str) and ID_RE.match(v): refs.append(v)
        elif k=='source' and isinstance(v,dict):
            r=v.get('ref')
            if isinstance(r,str) and ID_RE.match(r): refs.append(r)
    return list(dict.fromkeys(refs))

def build_graph(root):
    objs=load_objects(root); edges=[]; reverse=defaultdict(list)
    for oid,item in objs.items():
        for ref in extract_refs(item['data']):
            edges.append({'from':oid,'to':ref,'relation':'reference'})
            reverse[ref].append(oid)
    return objs,edges,reverse

def dependency_cycles(root):
    objs=load_objects(root); g={oid:[x for x in item['data'].get('depends_on',[]) if x in objs] for oid,item in objs.items()}
    visiting=set(); visited=set(); cycles=[]
    def dfs(n,stack):
        if n in visiting:
            i=stack.index(n); cycles.append(stack[i:]+[n]); return
        if n in visited: return
        visiting.add(n); stack.append(n)
        for m in g.get(n,[]): dfs(m,stack)
        stack.pop(); visiting.remove(n); visited.add(n)
    for n in g: dfs(n,[])
    return cycles

def related_bfs(root,start,depth=2):
    objs,edges,reverse=build_graph(root)
    adj=defaultdict(set)
    for e in edges: adj[e['from']].add(e['to']); adj[e['to']].add(e['from'])
    seen={start}; q=deque([(start,0)]); order=[]
    while q:
        n,d=q.popleft()
        if n!=start: order.append(n)
        if d>=depth: continue
        for m in sorted(adj.get(n,[])):
            if m not in seen: seen.add(m); q.append((m,d+1))
    return [x for x in order if x in objs]
