from pathlib import Path
import os, re, tempfile, yaml

ID_SUFFIX_LENGTH = 8
ID_RE = re.compile(r"^(?:DEC|REQ|FEAT|Q|RISK|EXP|SCR|FLOW|ENT|MET|DES|DEBT)-\d{8}-[0-9a-f]{8}$")

def find_root(start=None):
    p=Path(start or Path.cwd()).resolve()
    for c in [p,*p.parents]:
        if (c/'project.yaml').exists(): return c
    raise FileNotFoundError('project.yaml not found; run inside a project repository')

def distribution_root():
    # Machine assets are packaged alongside the CLI so a normal wheel install
    # works without relying on the source checkout layout.
    import project_system_assets
    return Path(project_system_assets.__file__).resolve().parent

def load_yaml(path):
    with open(path,'r',encoding='utf-8') as f:
        return yaml.safe_load(f) or {}

def dump_yaml(data):
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)

def atomic_write_text(path, text):
    """Atomically replace a regular file with UTF-8 text.

    The temporary file is created in the same directory so os.replace stays on
    the same filesystem. Existing symlink destinations are rejected.
    """
    p=Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.is_symlink():
        raise RuntimeError(f'refusing to write through symlink: {p}')
    fd,tmp=tempfile.mkstemp(prefix=f'.{p.name}.', suffix='.tmp', dir=str(p.parent))
    try:
        with os.fdopen(fd,'w',encoding='utf-8',newline='') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp,p)
    except Exception:
        try: os.unlink(tmp)
        except FileNotFoundError: pass
        raise

def slugify(s):
    s=re.sub(r'[^a-zA-Z0-9_-]+','-',s.strip().lower()).strip('-')
    return s or 'project'
