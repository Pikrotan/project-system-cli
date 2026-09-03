from project_system.init_project import init_project
from project_system.modules import enable

def test_enable_dependency(tmp_path):
    root=init_project('Demo',tmp_path/'demo')
    enable(root,'backend')
    created=enable(root,'payments')
    assert any('PAYMENTS_AND_BILLING' in x for x in created)
