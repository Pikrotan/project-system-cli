from pathlib import Path
import json, re
from .utils import load_yaml
from .graph import related_bfs
from .object_loader import load_object_layer
from .impact import impact
from .skills import skill_evidence

CURRENT_STATUSES={'decision':{'active'},'requirement':{'active'},'feature':{'idea','planned','in_progress','shipped'},'question':{'open','needs_data','ready_for_decision','blocked'},'risk':{'open','mitigated','accepted'},'experiment':{'planned','running','completed'},'screen':{'draft','design','approved','implemented'},'flow':{'draft','proposed','approved','implemented'},'entity':{'proposed','active'},'metric':{'proposed','active'},'design_change':{'new','review','approved'},'debt':{'open','acknowledged','in_progress'}}

def _safe_name(s): return re.sub(r'[^A-Za-z0-9_.-]+','-',s)[:80]

def _block(label,path,text=None):
    txt=path.read_text(encoding='utf-8') if text is None else text
    return f'\n\n---\n## {label}: `{path}`\n\n{txt}\n'

def _try_add(parts,label,path,maxchars,used,required=False,text=None):
    if not path.exists(): return used,False,'missing'
    block=_block(label,path,text)
    if used+len(block)>maxchars:
        if required:
            raise RuntimeError(f'essential context item exceeds budget: {path}')
        return used,False,'budget'
    parts.append(block)
    return used+len(block),True,None

def build_context(root,target='project',budget='medium',mode='review',allowed_write_set=None,kind='context',skill_names=None):
    root=Path(root); pol=load_yaml(root/'.project/policies/retrieval.yaml')
    tokens=int(pol.get('context_budgets',{}).get(budget,20000)); maxchars=tokens*4
    objs=load_object_layer(root).objects
    parts=[f'# {kind.title()} Pack\n\nTarget: `{target}`\n\nMode: `{mode}`\n\nBudget: `{budget}`\n']
    used=len(parts[0]); included_docs=[]; included_objs=[]; omitted_docs=[]; omitted_objs=[]
    special=target in {'project','onboarding','bootstrap'}
    canonical_scope=allowed_write_set or []
    evidence,selected_records=skill_evidence(root,skill_names or [],canonical_scope)

    for name,record in selected_records.items():
        used,ok,_=_try_add(
            parts,f'Project Skill {name}',record.path,maxchars,used,
            required=True,text=record.content,
        )
        if not ok:
            raise RuntimeError(f'cannot include required project Skill: {name}')

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
    derived_scope=[f'{outdir.relative_to(root).as_posix()}/**']
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
        'allowed_write_set':canonical_scope,
        'selected_skills':evidence['selected_skills'],
        'skills_registry_sha256':evidence['skills_registry_sha256'],
        'task_write_scope':{'canonical':canonical_scope,'derived':derived_scope},
        'effective_write_scope':evidence['effective_write_scope'],
        'skill_write_authorizations':evidence['skill_write_authorizations'],
        'excluded_historical':True,'canonical':False
    }
    (outdir/'manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    (outdir/'context.md').write_text(''.join(parts),encoding='utf-8')
    return outdir,manifest
