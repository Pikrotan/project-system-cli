from project_system.ids import make_id
import re

def test_id_format(tmp_path):
    v=make_id('decision',tmp_path)
    assert re.match(r'^DEC-\d{8}-[0-9a-f]{8}$',v)

def test_id_suffix_has_large_random_space(tmp_path):
    vals={make_id('decision',tmp_path) for _ in range(2000)}
    assert len(vals)==2000
