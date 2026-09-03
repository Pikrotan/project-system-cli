from pathlib import Path
from collections import Counter
from .utils import load_yaml, ID_RE
from .frontmatter import read_object
from .schemas import validate_object_schema, validate_project_schema
from .graph import load_objects, extract_refs, dependency_cycles
from .objects import DIRS

BAD_DEP_STATUSES={'deprecated','removed','rejected','superseded','cancelled'}

def validate(root):
    issues=[]
    cfg=load_yaml(Path(root)/'project.yaml')
    for m in validate_project_schema(cfg): issues.append(('BLOCKING','project.yaml',m))
    objects=[]
    for p in Path(root).glob('knowledge/**/*.md'):
        try: data,_=read_object(p)
        except Exception as e:
            issues.append(('ERROR',str(p.relative_to(root)),str(e))); continue
        objects.append((p,data))
        expected_dir=DIRS.get(data.get('type'))
        if expected_dir and p.parent.name!=expected_dir:
            issues.append(('ERROR',str(p.relative_to(root)),f'object type {data.get("type")} must live under knowledge/{expected_dir}/'))
        for m in validate_object_schema(data): issues.append(('ERROR',str(p.relative_to(root)),m))
        if data.get('id') and p.stem!=data['id']: issues.append(('ERROR',str(p.relative_to(root)),'filename does not match object id'))
    counts=Counter(d.get('id') for _,d in objects if d.get('id'))
    for oid,n in counts.items():
        if n>1: issues.append(('ERROR',oid,'duplicate ID'))
    objmap=load_objects(root)
    for p,d in objects:
        for ref in extract_refs(d):
            if ref not in objmap: issues.append(('ERROR',d.get('id',str(p)) ,f'broken reference: {ref}'))
        for dep in d.get('depends_on',[]) or []:
            if dep in objmap and objmap[dep]['data'].get('status') in BAD_DEP_STATUSES and d.get('status') in {'active','approved','implemented','shipped','in_progress'}:
                issues.append(('WARNING',d.get('id','?'),f'current object depends on non-current {dep} ({objmap[dep]["data"].get("status")})'))
    for cyc in dependency_cycles(root): issues.append(('ERROR','dependency_graph','cycle: '+' -> '.join(cyc)))
    active_decisions=[d for _,d in objects if d.get('type')=='decision' and d.get('status')=='active']
    if active_decisions and cfg.get('validation',{}).get('human_approval_checks',True):
        mode=cfg.get('governance_mode','solo')
        if mode=='solo':
            msg='approval metadata is structurally valid only; solo HITL is procedural and cannot be proven by the local validator'
        elif mode=='small_team':
            msg='approval metadata is structurally valid only; enforce required human review in the hosting platform for governed changes'
        else:
            msg='local validator cannot prove hosting-platform human approval; enforce protected branch / required human review externally'
        issues.append(('INFO','governance',msg))
    return issues

def counts(issues):
    c=Counter(x[0] for x in issues)
    return {k:c.get(k,0) for k in ['BLOCKING','ERROR','WARNING','INFO']}
