from project_system.init_project import init_project
from project_system.objects import create_object
from project_system.context import build_context

def test_context_pack(tmp_path):
    root=init_project('Demo',tmp_path/'demo')
    _,oid=create_object(root,'feature','Search','product','owner')
    out,manifest=build_context(root,oid,'small','implement')
    assert (out/'manifest.json').exists()
    assert (out/'context.md').exists()
    assert manifest['target']==oid
