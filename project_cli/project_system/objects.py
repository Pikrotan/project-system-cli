from pathlib import Path
from datetime import date
from .ids import make_id
from .utils import distribution_root

DIRS={'decision':'decisions','requirement':'requirements','feature':'features','question':'questions','risk':'risks','experiment':'experiments','screen':'screens','flow':'flows','entity':'entities','metric':'metrics','design_change':'design_changes','debt':'debts'}

def create_object(root,obj_type,title,domain='general',owner='owner'):
    tpl=(distribution_root()/'object_templates'/f'{obj_type}.md').read_text(encoding='utf-8')
    target_dir=Path(root)/'knowledge'/DIRS[obj_type]
    target_dir.mkdir(parents=True,exist_ok=True)
    # Exclusive creation closes the same-working-tree TOCTOU race. Cross-branch
    # collisions are made negligible by the 8-character cryptographic suffix.
    for _ in range(100):
        oid=make_id(obj_type,root)
        text=tpl.replace('{{ID}}',oid).replace('{{TITLE}}',title).replace('{{DOMAIN}}',domain).replace('{{OWNER}}',owner).replace('{{DATE}}',date.today().isoformat())
        p=target_dir/f'{oid}.md'
        try:
            with p.open('x',encoding='utf-8') as f:
                f.write(text)
            return p,oid
        except FileExistsError:
            continue
    raise RuntimeError('could not create object after repeated ID collisions')
