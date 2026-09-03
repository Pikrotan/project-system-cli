from project_system.init_project import init_project
from project_system.objects import create_object
from project_system.frontmatter import read_object, write_object
from project_system.validation import validate


def test_active_decision_requires_approval(tmp_path):
    root=init_project('Demo',tmp_path/'demo')
    p,_=create_object(root,'decision','D','product','owner')
    d,b=read_object(p); d['status']='active'; write_object(p,d,b)
    issues=validate(root)
    assert any(x[0]=='ERROR' and 'approved_by' in x[2] for x in issues)


def test_broken_reference_is_error(tmp_path):
    root=init_project('Demo',tmp_path/'demo')
    p,_=create_object(root,'feature','F','product','owner')
    d,b=read_object(p); d['depends_on']=['DEC-20260831-ABCDEFGH']; write_object(p,d,b)
    issues=validate(root)
    assert any(x[0]=='ERROR' and 'broken reference' in x[2] for x in issues)


def test_resolved_question_needs_resolution_reference(tmp_path):
    root=init_project('Demo',tmp_path/'demo')
    p,_=create_object(root,'question','Q','product','owner')
    d,b=read_object(p); d['status']='resolved'; write_object(p,d,b)
    issues=validate(root)
    assert any(x[0]=='ERROR' and 'resolved_by' in x[2] for x in issues)
