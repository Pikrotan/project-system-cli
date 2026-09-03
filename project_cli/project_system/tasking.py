from pathlib import Path
from .context import build_context
from .impact import impact
from .git_helpers import changed_files

def task(root,target,mode='implement',budget='medium',sync=False):
    imp=impact(root,target)
    allowed=[str(Path('knowledge')/Path(target))] if False else []
    # canonical object file + deterministic impact docs; executor may request scope expansion rather than editing outside this set
    from .graph import load_objects
    objs=load_objects(root)
    if target in objs: allowed.append(str(objs[target]['path'].relative_to(root)))
    allowed += [x for x in imp['check_docs'] if (Path(root)/x).exists()]
    kind='sync' if sync else 'task'
    return build_context(root,target,budget,mode,allowed_write_set=list(dict.fromkeys(allowed)),kind=kind)

def bootstrap(root,budget='medium'):
    return build_context(root,'bootstrap',budget,'sync',allowed_write_set=['docs/**','knowledge/**','inbox/**'],kind='bootstrap')

def prepare_pr(root):
    from .validation import validate, counts
    issues=validate(root); c=counts(issues); changed=changed_files(root)
    p=Path(root)/'.generated/reports/PR_DESCRIPTION.md'; p.parent.mkdir(parents=True,exist_ok=True)
    txt='# Pull Request Draft\n\n## What changed\n\n'+('\n'.join(f'- `{x}`' for x in changed) if changed else '_No Git diff detected._')+'\n\n## Why\n\n_TODO._\n\n## Change class\n\n_TODO: A / B / C / D\n\n## Knowledge objects\n\n_TODO._\n\n## Impact\n\n_TODO._\n\n## Tests / validation\n\n'+f"- BLOCKING: {c['BLOCKING']}\n- ERROR: {c['ERROR']}\n- WARNING: {c['WARNING']}\n\n## Human approval required\n\n_TODO._\n\n## Risks / drift\n\n_TODO._\n"
    p.write_text(txt,encoding='utf-8'); return p
