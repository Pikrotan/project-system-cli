from pathlib import Path
import os, tempfile, yaml
from .utils import distribution_root, load_yaml, atomic_write_text

def catalog():
    base=distribution_root()/'blueprints/modules'; out={}
    for p in base.iterdir():
        if p.is_dir() and (p/'blueprint.yaml').exists(): out[p.name]=load_yaml(p/'blueprint.yaml')
    return out

def _safe_relative(base, rel, label):
    base=Path(base).resolve()
    relp=Path(rel)
    if relp.is_absolute() or '..' in relp.parts:
        raise RuntimeError(f'unsafe {label} path: {rel}')
    candidate=base/relp
    # Reject any existing symlink in the lexical path. This is stricter than
    # merely resolving the final path and avoids writing through repo symlinks.
    cur=base
    for part in relp.parts:
        cur=cur/part
        if cur.is_symlink():
            raise RuntimeError(f'refusing {label} path containing symlink: {rel}')
    resolved=candidate.resolve(strict=False)
    if not resolved.is_relative_to(base):
        raise RuntimeError(f'{label} escapes allowed root: {rel}')
    return candidate

def _stage_bytes(destination, payload):
    destination=Path(destination)
    destination.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix=f'.{destination.name}.',suffix='.stage',dir=str(destination.parent))
    try:
        with os.fdopen(fd,'wb') as f:
            f.write(payload); f.flush(); os.fsync(f.fileno())
        return Path(tmp)
    except Exception:
        try: os.unlink(tmp)
        except FileNotFoundError: pass
        raise

def _commit_staged(staged, destination):
    destination=Path(destination)
    if destination.is_symlink():
        raise RuntimeError(f'refusing to overwrite symlink: {destination}')
    os.replace(staged,destination)

def enable(root,module):
    root=Path(root).resolve(); cats=catalog()
    if module not in cats: raise KeyError(f'unknown module: {module}')
    spec=cats[module]; cfg=load_yaml(root/'project.yaml'); mods=cfg.setdefault('modules',{})
    if mods.get(module,{}).get('enabled'):
        return []
    missing=[m for m in spec.get('requires',[]) if not mods.get(m,{}).get('enabled')]
    if missing: raise RuntimeError('missing required modules: '+', '.join(missing))
    enabled_names={m for m,v in mods.items() if isinstance(v,dict) and v.get('enabled')}
    direct={m for m in spec.get('conflicts_with',[]) if m in enabled_names}
    reverse={m for m in enabled_names if module in cats.get(m,{}).get('conflicts_with',[])}
    conflicts=sorted(direct | reverse)
    if conflicts: raise RuntimeError('conflicting enabled modules: '+', '.join(conflicts))

    module_root=distribution_root()/'blueprints/modules'/module
    src_root=(module_root/'files').resolve()
    plan=[]
    for item in spec.get('creates',[]):
        s=_safe_relative(src_root,item['template'],'blueprint template')
        if not s.is_file(): raise FileNotFoundError(f'blueprint template not found: {item["template"]}')
        d=_safe_relative(root,item['target'],'blueprint target')
        if d.is_symlink(): raise RuntimeError(f'refusing to overwrite symlink: {item["target"]}')
        if d.exists():
            continue
        # Read before mutating the project so a bad source cannot leave a
        # half-enabled module behind.
        plan.append((item['target'],d,s.read_bytes()))

    staged=[]; created=[]; created_dirs=[]
    try:
        for rel,d,payload in plan:
            # Revalidate immediately before staging/commit in case the working
            # tree changed after planning.
            _safe_relative(root,rel,'blueprint target')
            if d.exists() or d.is_symlink():
                raise RuntimeError(f'target appeared during module enable: {rel}')
            parent=d.parent
            missing=[]
            cur=parent
            while cur != root and not cur.exists():
                missing.append(cur); cur=cur.parent
            st=_stage_bytes(d,payload); staged.append(st)
            created_dirs.extend(x for x in reversed(missing) if x not in created_dirs)
            _commit_staged(st,d); staged.remove(st); created.append(d)
        mods[module]={'enabled':True,'blueprint_version':str(spec.get('version','1.0'))}
        atomic_write_text(root/'project.yaml',yaml.safe_dump(cfg,sort_keys=False,allow_unicode=True))
    except Exception:
        for st in staged:
            try: st.unlink()
            except FileNotFoundError: pass
        for d in reversed(created):
            try: d.unlink()
            except FileNotFoundError: pass
        for d in reversed(created_dirs):
            try: d.rmdir()
            except (FileNotFoundError, OSError): pass
        raise
    return [str(p.relative_to(root)) for p in created]

def disable(root,module):
    root=Path(root).resolve(); cats=catalog(); cfg=load_yaml(root/'project.yaml'); mods=cfg.setdefault('modules',{})
    dependents=[m for m,s in cats.items() if module in s.get('requires',[]) and mods.get(m,{}).get('enabled')]
    if dependents: raise RuntimeError('enabled modules depend on this module: '+', '.join(dependents))
    if module not in mods: mods[module]={}
    mods[module]['enabled']=False
    atomic_write_text(root/'project.yaml',yaml.safe_dump(cfg,sort_keys=False,allow_unicode=True))
    return ['Module disabled in config; previously materialized docs are intentionally not deleted automatically.']
