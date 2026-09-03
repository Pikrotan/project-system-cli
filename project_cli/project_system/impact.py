from pathlib import Path
from collections import deque
from .graph import build_graph
from .utils import load_yaml

def impact(root,target,max_depth=3):
    objs,edges,reverse=build_graph(root)
    if target not in objs: raise KeyError(f'object not found: {target}')
    affected=[]; seen={target}; q=deque([(target,0)])
    while q:
        n,d=q.popleft()
        if d>=max_depth: continue
        for m in sorted(reverse.get(n,[])):
            if m not in seen: seen.add(m); affected.append(m); q.append((m,d+1))
    docs=[]; pol=load_yaml(Path(root)/'.project/policies/impact.yaml')
    data=objs[target]['data']
    for rule in pol.get('rules',[]):
        when=rule.get('when',{})
        if all(data.get(k)==v for k,v in when.items()): docs.extend(rule.get('check_docs',[]))
    # domain-specific narrative hints
    domain=data.get('domain','')
    if domain in {'product','ux'}: docs += ['docs/03_PRODUCT.md']
    if domain in {'architecture','technical'}: docs += ['docs/04_ARCHITECTURE.md','docs/technical/TECH_OVERVIEW.md']
    return {'target':target,'affected_objects':list(dict.fromkeys(affected)),'check_docs':list(dict.fromkeys(docs))}
