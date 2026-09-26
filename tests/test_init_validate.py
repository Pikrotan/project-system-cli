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

def test_new_project_records_current_cli_target(tmp_path):
    root=init_project('Demo',tmp_path/'demo','other','solo')
    assert load_yaml(root/'project.yaml')['tooling']['project_cli']==__version__
    assert load_yaml(root/'project.yaml')['tooling']['skills_schema_version']==1
    assert (root/'.project/skills.yaml').is_file()
    assert (root/'.agents/skills/knowledge-sync/SKILL.md').is_file()

def test_new_project_materializes_rules_v1(tmp_path):
    root=init_project("Demo",tmp_path/"demo","other","solo")
    cfg=load_yaml(root/"project.yaml")
    assert cfg["tooling"]["rules_schema_version"]==1
    assert load_yaml(root/".project/policies/rules.yaml")=={
        "schema_version":1,
        "profile":"project-system-rules-v1",
        "rules":{},
    }
    assert load_yaml(root/".project/policies/rule_exceptions.yaml")=={
        "schema_version":1,
        "profile":"project-system-rule-exceptions-v1",
        "exceptions":{},
    }
