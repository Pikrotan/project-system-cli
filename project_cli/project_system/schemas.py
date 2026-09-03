from pathlib import Path
import json
from jsonschema import Draft202012Validator
from .utils import distribution_root

def schema_path_for_type(obj_type):
    return distribution_root()/'schemas'/f'{obj_type.replace("_","-")}.schema.json'

def load_schema(obj_type):
    return json.loads(schema_path_for_type(obj_type).read_text(encoding='utf-8'))

def validate_object_schema(data):
    t=data.get('type')
    if not t: return ['missing type']
    p=schema_path_for_type(t)
    if not p.exists(): return [f'unknown object type: {t}']
    v=Draft202012Validator(load_schema(t))
    return [((('.'.join(str(x) for x in e.path))+': ') if e.path else '') + e.message for e in sorted(v.iter_errors(data),key=lambda e:list(e.path))]

def validate_project_schema(data):
    schema=json.loads((distribution_root()/'schemas'/'project.schema.json').read_text(encoding='utf-8'))
    v=Draft202012Validator(schema)
    return [((('.'.join(str(x) for x in e.path))+': ') if e.path else '') + e.message for e in sorted(v.iter_errors(data),key=lambda e:list(e.path))]
