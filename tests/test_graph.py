from project_system.init_project import init_project
from project_system.objects import create_object
from project_system.frontmatter import read_object, write_object
from project_system.graph import build_graph

def test_graph_reference(tmp_path):
    root=init_project('Demo',tmp_path/'demo')
    p1,id1=create_object(root,'decision','D','product','owner')
    d,b=read_object(p1); d['status']='active'; d['approved_by']='owner'; d['approved_at']='2026-08-31'; write_object(p1,d,b)
    p2,id2=create_object(root,'requirement','R','product','owner')
    d,b=read_object(p2); d['depends_on']=[id1]; write_object(p2,d,b)
    objs,edges,rev=build_graph(root)
    assert any(e['from']==id2 and e['to']==id1 for e in edges)
