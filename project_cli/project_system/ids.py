from datetime import date
from pathlib import Path
import secrets
from .utils import ID_SUFFIX_LENGTH

PREFIX={'decision':'DEC','requirement':'REQ','feature':'FEAT','question':'Q','risk':'RISK','experiment':'EXP','screen':'SCR','flow':'FLOW','entity':'ENT','metric':'MET','design_change':'DES','debt':'DEBT'}
ALPHABET='ABCDEFGHJKLMNPQRSTUVWXYZ23456789'

def make_id(obj_type, root=None, today=None):
    prefix=PREFIX[obj_type]
    d=(today or date.today()).strftime('%Y%m%d')
    existing=set()
    if root:
        for p in Path(root).glob('knowledge/**/*.md'):
            existing.add(p.stem)
    for _ in range(100):
        suffix=''.join(secrets.choice(ALPHABET) for _ in range(ID_SUFFIX_LENGTH))
        value=f'{prefix}-{d}-{suffix}'
        if value not in existing:
            return value
    raise RuntimeError('could not generate unique ID')
