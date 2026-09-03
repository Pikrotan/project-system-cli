from .validation import validate, counts
from .graph import load_objects

def health(root):
    issues=validate(root); c=counts(issues); objs=load_objects(root)
    by={}
    for i in objs.values(): by[i['data'].get('type','unknown')]=by.get(i['data'].get('type','unknown'),0)+1
    return c,by,issues
