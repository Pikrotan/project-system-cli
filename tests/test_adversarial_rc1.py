from pathlib import Path
import shutil, yaml, pytest

from project_system.init_project import init_project
from project_system.frontmatter import read_object
from project_system.objects import create_object
from project_system.context import build_context
import project_system.modules as modules


def _fake_dist(tmp_path, module_name, creates, requires=None, conflicts=None):
    dist=tmp_path/'fake-dist'; bp=dist/'blueprints/modules'/module_name
    (bp/'files').mkdir(parents=True)
    for item in creates:
        t=item['template']
        if '..' not in Path(t).parts and not Path(t).is_absolute():
            p=bp/'files'/t; p.parent.mkdir(parents=True,exist_ok=True); p.write_text(f'payload:{t}',encoding='utf-8')
    (bp/'blueprint.yaml').write_text(yaml.safe_dump({
        'id':module_name,'name':module_name,'version':'1.0','requires':requires or [],
        'conflicts_with':conflicts or [],'creates':creates
    }),encoding='utf-8')
    return dist


def test_frontmatter_rejects_python_object_yaml(tmp_path):
    p=tmp_path/'bad.md'
    p.write_text('---\na: !!python/object/apply:os.system ["echo PWNED"]\n---\nbody\n',encoding='utf-8')
    with pytest.raises(yaml.constructor.ConstructorError):
        read_object(p)


def test_module_rejects_target_path_traversal(tmp_path,monkeypatch):
    root=init_project('Demo',tmp_path/'demo')
    dist=_fake_dist(tmp_path,'evil',[{'template':'payload.txt','target':'../escaped.txt'}])
    monkeypatch.setattr(modules,'distribution_root',lambda:dist)
    with pytest.raises(RuntimeError,match='unsafe blueprint target path'):
        modules.enable(root,'evil')
    assert not (tmp_path/'escaped.txt').exists()


def test_module_rejects_symlink_target(tmp_path,monkeypatch):
    root=init_project('Demo',tmp_path/'demo')
    dist=_fake_dist(tmp_path,'evil',[{'template':'payload.txt','target':'docs/evil.md'}])
    monkeypatch.setattr(modules,'distribution_root',lambda:dist)
    outside=tmp_path/'outside.txt'; link=root/'docs/evil.md'
    try:
        link.symlink_to(outside)
    except OSError as exc:
        if getattr(exc,'winerror',None)==1314:
            pytest.skip('Windows symlink privilege is not available')
        raise
    with pytest.raises(RuntimeError,match='symlink'):
        modules.enable(root,'evil')
    assert not outside.exists()


def test_module_enable_rolls_back_on_mid_commit_failure(tmp_path,monkeypatch):
    root=init_project('Demo',tmp_path/'demo')
    creates=[{'template':'one.md','target':'docs/one.md'},{'template':'two.md','target':'docs/two.md'}]
    dist=_fake_dist(tmp_path,'fragile',creates)
    monkeypatch.setattr(modules,'distribution_root',lambda:dist)
    real=modules._commit_staged; calls={'n':0}
    def fail_second(staged,destination):
        calls['n']+=1
        if calls['n']==2: raise OSError('simulated disk failure')
        return real(staged,destination)
    monkeypatch.setattr(modules,'_commit_staged',fail_second)
    with pytest.raises(OSError,match='simulated'):
        modules.enable(root,'fragile')
    assert not (root/'docs/one.md').exists()
    assert not (root/'docs/two.md').exists()
    cfg=yaml.safe_load((root/'project.yaml').read_text())
    assert not cfg.get('modules',{}).get('fragile',{}).get('enabled',False)


def test_target_object_is_never_silently_displaced_by_core_context(tmp_path):
    root=init_project('Demo',tmp_path/'demo')
    p,oid=create_object(root,'feature','Target','product','owner')
    pol=root/'.project/policies/retrieval.yaml'; data=yaml.safe_load(pol.read_text())
    data['context_budgets']['small']=500
    pol.write_text(yaml.safe_dump(data,sort_keys=False),encoding='utf-8')
    out,manifest=build_context(root,oid,'small','implement')
    assert oid in manifest['included_objects']
    assert manifest['budget_exhausted'] is True
    text=(out/'context.md').read_text()
    assert f'id: {oid}' in text
    assert text.count('---') % 2 == 0


def test_essential_target_larger_than_budget_fails_explicitly(tmp_path):
    root=init_project('Demo',tmp_path/'demo')
    p,oid=create_object(root,'feature','Huge','product','owner')
    p.write_text(p.read_text()+('\nX'*10000),encoding='utf-8')
    pol=root/'.project/policies/retrieval.yaml'; data=yaml.safe_load(pol.read_text())
    data['context_budgets']['small']=100
    pol.write_text(yaml.safe_dump(data,sort_keys=False),encoding='utf-8')
    with pytest.raises(RuntimeError,match='essential context item exceeds budget'):
        build_context(root,oid,'small','implement')


def test_unknown_schema_version_is_rejected(tmp_path):
    from project_system.frontmatter import write_object
    from project_system.validation import validate
    root=init_project('Demo',tmp_path/'demo')
    p,_=create_object(root,'feature','Future','product','owner')
    d,b=read_object(p); d['schema_version']=999; write_object(p,d,b)
    assert any(x[0]=='ERROR' and 'schema_version' in x[2] for x in validate(root))


def test_object_type_must_match_knowledge_directory(tmp_path):
    from project_system.validation import validate
    root=init_project('Demo',tmp_path/'demo')
    p,_=create_object(root,'feature','Wrong place','product','owner')
    q=root/'knowledge/requirements'/p.name; p.replace(q)
    assert any(x[0]=='ERROR' and 'must live under knowledge/features' in x[2] for x in validate(root))
