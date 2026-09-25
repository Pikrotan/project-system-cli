from hashlib import sha256

import project_system.context as context_module
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

def test_context_repeatable_skill_evidence(tmp_path):
    root=init_project('Demo',tmp_path/'demo')
    path,oid=create_object(root,'feature','Search','product','owner')
    relative=path.relative_to(root).as_posix()
    out,manifest=build_context(
        root,oid,'small','implement',allowed_write_set=[relative],
        skill_names=['requirements-management','implementation-plan'],
    )
    assert [item['name'] for item in manifest['selected_skills']]==[
        'requirements-management','implementation-plan'
    ]
    assert manifest['effective_write_scope']==[relative]
    assert '# Requirements Management' in (out/'context.md').read_text(encoding='utf-8')


def test_context_renders_same_skill_snapshot_bound_to_hash(tmp_path, monkeypatch):
    root=init_project('Demo',tmp_path/'snapshot')
    path,oid=create_object(root,'feature','Search','product','owner')
    relative=path.relative_to(root).as_posix()
    skill=root/'.agents/skills/requirements-management/SKILL.md'
    initial_bytes=skill.read_bytes()
    initial_text=initial_bytes.decode('utf-8')
    replacement=initial_text.replace(
        '# Requirements Management','# Replaced After Snapshot',1,
    )
    original=context_module.skill_evidence

    def replace_after_snapshot(*args,**kwargs):
        result=original(*args,**kwargs)
        skill.write_text(replacement,encoding='utf-8')
        return result

    monkeypatch.setattr(context_module,'skill_evidence',replace_after_snapshot)
    out,manifest=build_context(
        root,oid,'small','implement',allowed_write_set=[relative],
        skill_names=['requirements-management'],
    )
    rendered=(out/'context.md').read_text(encoding='utf-8')

    assert manifest['selected_skills'][0]['sha256']==sha256(initial_bytes).hexdigest()
    assert '# Requirements Management' in rendered
    assert '# Replaced After Snapshot' not in rendered
