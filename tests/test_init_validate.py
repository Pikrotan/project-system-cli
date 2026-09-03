from project_system.init_project import init_project
from project_system.validation import validate
from project_system.objects import create_object

def test_init_valid(tmp_path):
    root=init_project('Demo',tmp_path/'demo','mobile_app','solo')
    assert not [x for x in validate(root) if x[0] in {'BLOCKING','ERROR'}]

def test_object_creation_valid(tmp_path):
    root=init_project('Demo',tmp_path/'demo','other','solo')
    create_object(root,'feature','Search','product','owner')
    assert not [x for x in validate(root) if x[0] in {'BLOCKING','ERROR'}]
