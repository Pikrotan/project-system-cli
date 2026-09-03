from project_system.init_project import init_project
from project_system.validation import validate
from project_system.objects import create_object
from project_system import __version__
from project_system.utils import load_yaml

def test_init_valid(tmp_path):
    root=init_project('Demo',tmp_path/'demo','mobile_app','solo')
    assert not [x for x in validate(root) if x[0] in {'BLOCKING','ERROR'}]

def test_object_creation_valid(tmp_path):
    root=init_project('Demo',tmp_path/'demo','other','solo')
    create_object(root,'feature','Search','product','owner')
    assert not [x for x in validate(root) if x[0] in {'BLOCKING','ERROR'}]

def test_new_project_records_runtime_cli_version(tmp_path):
    root=init_project('Demo',tmp_path/'demo','other','solo')
    assert load_yaml(root/'project.yaml')['tooling']['project_cli']==__version__
