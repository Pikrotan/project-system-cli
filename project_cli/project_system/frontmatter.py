from pathlib import Path
import yaml

MAX_OBJECT_FILE_BYTES = 2 * 1024 * 1024
MAX_FRONTMATTER_BYTES = 256 * 1024

class StrictSafeLoader(yaml.SafeLoader):
    pass

def _construct_mapping_no_duplicates(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError(f'duplicate YAML key: {key!r}')
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping

StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping_no_duplicates,
)

def _reject_aliases(text):
    # Frontmatter is intentionally simple metadata; anchors/aliases add no
    # legitimate value here and can create pathological recursive/shared
    # structures. Reject them before object construction.
    for token in yaml.scan(text, Loader=yaml.SafeLoader):
        if isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken)):
            raise ValueError('YAML anchors/aliases are not allowed in knowledge frontmatter')

def read_object(path):
    path=Path(path)
    size=path.stat().st_size
    if size > MAX_OBJECT_FILE_BYTES:
        raise ValueError(f'knowledge object exceeds size limit ({MAX_OBJECT_FILE_BYTES} bytes)')
    text=path.read_text(encoding='utf-8')
    if not text.startswith('---\n'):
        raise ValueError('missing YAML frontmatter')
    parts=text.split('---\n',2)
    if len(parts)<3:
        raise ValueError('unterminated YAML frontmatter')
    fm_text=parts[1]
    if len(fm_text.encode('utf-8')) > MAX_FRONTMATTER_BYTES:
        raise ValueError(f'YAML frontmatter exceeds size limit ({MAX_FRONTMATTER_BYTES} bytes)')
    _reject_aliases(fm_text)
    data=yaml.load(fm_text, Loader=StrictSafeLoader) or {}
    if not isinstance(data, dict):
        raise ValueError('YAML frontmatter must be a mapping/object')
    return data, parts[2].lstrip('\n')

def write_object(path,data,body):
    Path(path).write_text('---\n'+yaml.safe_dump(data,sort_keys=False,allow_unicode=True)+'---\n\n'+body.lstrip(),encoding='utf-8')
