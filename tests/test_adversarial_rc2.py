from pathlib import Path
import yaml, pytest

from project_system.init_project import init_project
from project_system.frontmatter import read_object, MAX_OBJECT_FILE_BYTES
from project_system.objects import create_object
from project_system.context import build_context
from project_system.validation import validate
from project_system.frontmatter import write_object
import project_system.modules as modules


def _fake_dist(tmp_path, specs):
    dist=tmp_path/'fake-dist'
    for module_name, spec in specs.items():
        bp=dist/'blueprints/modules'/module_name
        (bp/'files').mkdir(parents=True)
        creates=spec.get('creates',[])
        for item in creates:
            p=bp/'files'/item['template']; p.parent.mkdir(parents=True,exist_ok=True)
            p.write_text(f'payload:{module_name}:{item["template"]}',encoding='utf-8')
        data={
            'id':module_name,'name':module_name,'version':'1.0','category':'capability',
            'requires':spec.get('requires',[]),'conflicts_with':spec.get('conflicts_with',[]),
            'creates':creates,
        }
        (bp/'blueprint.yaml').write_text(yaml.safe_dump(data),encoding='utf-8')
    return dist


def test_conflicts_are_checked_symmetrically(tmp_path, monkeypatch):
    root=init_project('Demo',tmp_path/'demo')
    dist=_fake_dist(tmp_path,{
        'a':{'conflicts_with':['b'],'creates':[{'template':'a.md','target':'docs/a.md'}]},
        'b':{'conflicts_with':[],'creates':[{'template':'b.md','target':'docs/b.md'}]},
    })
    monkeypatch.setattr(modules,'distribution_root',lambda:dist)
    modules.enable(root,'a')
    with pytest.raises(RuntimeError,match='conflicting enabled modules: a'):
        modules.enable(root,'b')


def test_conflicts_are_not_transitive(tmp_path, monkeypatch):
    root=init_project('Demo',tmp_path/'demo')
    dist=_fake_dist(tmp_path,{
        'a':{'conflicts_with':['b'],'creates':[{'template':'a.md','target':'docs/a.md'}]},
        'b':{'conflicts_with':['c'],'creates':[{'template':'b.md','target':'docs/b.md'}]},
        'c':{'conflicts_with':[],'creates':[{'template':'c.md','target':'docs/c.md'}]},
    })
    monkeypatch.setattr(modules,'distribution_root',lambda:dist)
    modules.enable(root,'a')
    # A and C are not explicitly incompatible. B cannot be enabled with either.
    modules.enable(root,'c')
    with pytest.raises(RuntimeError,match='conflicting enabled modules'):
        modules.enable(root,'b')


def test_frontmatter_rejects_yaml_aliases(tmp_path):
    p=tmp_path/'alias.md'
    p.write_text('---\na: &a [x]\nb: [*a,*a]\n---\nbody\n',encoding='utf-8')
    with pytest.raises(ValueError,match='anchors/aliases'):
        read_object(p)


def test_frontmatter_rejects_duplicate_keys(tmp_path):
    p=tmp_path/'dup.md'
    p.write_text('---\ntype: feature\ntype: decision\n---\nbody\n',encoding='utf-8')
    with pytest.raises(ValueError,match='duplicate YAML key'):
        read_object(p)


def test_frontmatter_rejects_oversized_object(tmp_path):
    p=tmp_path/'huge.md'
    with p.open('wb') as f:
        f.write(b'---\na: b\n---\n')
        f.seek(MAX_OBJECT_FILE_BYTES)
        f.write(b'X')
    with pytest.raises(ValueError,match='exceeds size limit'):
        read_object(p)


def test_context_manifest_proves_character_ceiling(tmp_path):
    root=init_project('Demo',tmp_path/'demo')
    _,oid=create_object(root,'feature','Feature','product','owner')
    out,manifest=build_context(root,oid,'small','review')
    text=(out/'context.md').read_text(encoding='utf-8')
    assert manifest['actual_chars']==len(text)
    assert len(text) <= manifest['char_budget']


def test_dependency_cycle_is_error(tmp_path):
    root=init_project('Demo',tmp_path/'demo')
    p1,id1=create_object(root,'feature','A','product','owner')
    p2,id2=create_object(root,'feature','B','product','owner')
    d,b=read_object(p1); d['depends_on']=[id2]; write_object(p1,d,b)
    d,b=read_object(p2); d['depends_on']=[id1]; write_object(p2,d,b)
    issues=validate(root)
    assert any(sev=='ERROR' and loc=='dependency_graph' and 'cycle:' in msg for sev,loc,msg in issues)


def test_failed_enable_removes_new_empty_directories(tmp_path, monkeypatch):
    root=init_project('Demo',tmp_path/'demo')
    dist=_fake_dist(tmp_path,{
        'fragile':{'creates':[
            {'template':'one.md','target':'docs/new/deep/one.md'},
            {'template':'two.md','target':'docs/new/deep/two.md'},
        ]},
    })
    monkeypatch.setattr(modules,'distribution_root',lambda:dist)
    real=modules._commit_staged; calls={'n':0}
    def fail_second(staged,destination):
        calls['n']+=1
        if calls['n']==2: raise OSError('boom')
        return real(staged,destination)
    monkeypatch.setattr(modules,'_commit_staged',fail_second)
    with pytest.raises(OSError,match='boom'):
        modules.enable(root,'fragile')
    assert not (root/'docs/new').exists()
