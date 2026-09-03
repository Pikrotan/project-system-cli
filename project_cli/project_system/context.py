from pathlib import Path
import json, re
from .utils import load_yaml
from .graph import related_bfs
from .object_loader import load_object_layer
from .impact import impact

CURRENT_STATUSES={'decision':{'active'},'requirement':{'active'},'feature':{'idea','planned','in_progress','shipped'},'question':{'open','needs_data','ready_for_decision','blocked'},'risk':{'open','mitigated','accepted'},'experiment':{'planned','running','completed'},'screen':{'draft','design','approved','implemented'},'flow':{'draft','proposed','approved','implemented'},'entity':{'proposed','active'},'metric':{'proposed','active'},'design_change':{'new','review','approved'},'debt':{'open','acknowledged','in_progress'}}

def _safe_name(s): return re.sub(r'[^A-Za-z0-9_.-]+','-',s)[:80]

def _block(label,path):
    txt=path.read_text(encoding='utf-8')
    return f'\n\n---\n## {label}: `{path}`\n\n{txt}\n'

def _try_add(parts,label,path,maxchars,used,required=False):
    if not path.exists(): return used,False,'missing'
    block=_block(label,path)
    if used+len(block)>maxchars:
        if required:
            raise RuntimeError(f'essential context item exceeds budget: {path}')
        return used,False,'budget'
    parts.append(block)
    return used+len(block),True,None

def build_context(root,target='project',budget='medium',mode='review',allowed_write_set=None,kind='context'):
    root=Path(root); pol=load_yaml(root/'.project/policies/retrieval.yaml')
    tokens=int(pol.get('context_budgets',{}).get(budget,20000)); maxchars=tokens*4
    objs=load_object_layer(root).objects
    parts=[f'# {kind.title()} Pack\n\nTarget: `{target}`\n\nMode: `{mode}`\n\nBudget: `{budget}`\n']
    used=len(parts[0]); included_docs=[]; included_objs=[]; omitted_docs=[]; omitted_objs=[]
    special=target in {'project','onboarding','bootstrap'}

    # For a targeted pack the target is the one item that must never be
    # silently displaced by generic core context.
    related=[]
    if not special:
        if target not in objs: raise KeyError(f'object not found: {target}')
        item=objs[target]
        used,ok,reason=_try_add(parts,'Target Knowledge Object',item['path'],maxchars,used,required=True)
        if ok: included_objs.append(target)
        related=related_bfs(root,target,2)

    for rel in pol.get('core_docs',[]):
        p=root/rel; used,ok,reason=_try_add(parts,'Core',p,maxchars,used)
        if ok: included_docs.append(rel)
        elif reason=='budget': omitted_docs.append(rel)

    if special:
        targets=[]
        for oid,item in objs.items():
            d=item['data']; t=d.get('type'); st=d.get('status')
            if st in CURRENT_STATUSES.get(t,set()): targets.append(oid)
        targets=sorted(targets)
    else:
        targets=related

    for oid in targets:
        item=objs[oid]; d=item['data']; t=d.get('type'); st=d.get('status')
        if not special and st not in CURRENT_STATUSES.get(t,set()):
            continue
        used,ok,reason=_try_add(parts,'Knowledge Object',item['path'],maxchars,used)
        if ok: included_objs.append(oid)
        elif reason=='budget': omitted_objs.append(oid)

    if not special:
        imp=impact(root,target)
        for rel in imp['check_docs']:
            p=root/rel; used,ok,reason=_try_add(parts,'Impact Check Doc',p,maxchars,used)
            if ok: included_docs.append(rel)
            elif reason=='budget': omitted_docs.append(rel)

    outdir=root/'.generated'/'context'/f'{kind.upper()}-{_safe_name(target)}-{budget}'; outdir.mkdir(parents=True,exist_ok=True)
    manifest={
        'target':target,'mode':mode,'budget':budget,
        'budget_tokens':tokens,  # compatibility: policy value is an estimate, not tokenizer-exact
        'char_budget':maxchars,'actual_chars':used,
        'estimated_tokens':(used+3)//4,
        'included_objects':list(dict.fromkeys(included_objs)),
        'included_docs':list(dict.fromkeys(included_docs)),
        'omitted_objects':list(dict.fromkeys(omitted_objs)),
        'omitted_docs':list(dict.fromkeys(omitted_docs)),
        'budget_exhausted':bool(omitted_objs or omitted_docs),
        'allowed_write_set':allowed_write_set or [],'excluded_historical':True,'canonical':False
    }
    (outdir/'manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    (outdir/'context.md').write_text(''.join(parts),encoding='utf-8')
    return outdir,manifest
