from .utils import find_root, load_yaml

def load_project(root=None):
    r=find_root(root)
    return r, load_yaml(r/'project.yaml')
