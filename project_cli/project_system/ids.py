from datetime import date
import secrets
from .utils import ID_SUFFIX_LENGTH
from .object_loader import load_object_layer

PREFIX={'decision':'DEC','requirement':'REQ','feature':'FEAT','question':'Q','risk':'RISK','experiment':'EXP','screen':'SCR','flow':'FLOW','entity':'ENT','metric':'MET','design_change':'DES','debt':'DEBT'}

def make_id(obj_type, root=None, today=None):
    prefix=PREFIX[obj_type]
    d=(today or date.today()).strftime('%Y%m%d')
    existing=set()
    if root:
        existing=set(load_object_layer(root).objects)
    for _ in range(100):
        suffix=secrets.token_hex(ID_SUFFIX_LENGTH // 2)
        value=f'{prefix}-{d}-{suffix}'
        if value not in existing:
            return value
    raise RuntimeError('could not generate unique ID')
