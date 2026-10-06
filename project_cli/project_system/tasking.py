from pathlib import Path
from .context import build_context
from .impact import impact
from .git_helpers import changed_files
from .object_loader import load_object_layer
from .skills import default_skill_names_for_target, skills_enabled
from .utils import load_yaml
from .task_specification import (
    build_task_specification, normalize_task_mode, snapshot_task_target,
    task_git_head, task_project_id, write_task_specification, serialize_task_specification,
    load_task_specification, MAX_SPEC_BYTES,
)
from .task_obligations import (
    build_task_obligations, write_task_obligations, serialize_task_obligations,
    load_task_obligations, MAX_OBLIGATIONS_BYTES,
)
from .task_baseline import (
    TaskBaselineError, build_task_baseline, capture_task_state, load_task_baseline,
    read_bounded, serialize_task_baseline, task_directory, write_task_artifact,
)

def task(root,target,mode='implement',budget='medium',sync=False,skills=None):
    config=load_yaml(Path(root)/'project.yaml')
    if not sync:
        mode=normalize_task_mode(mode)
        task_project_id(config)
        head=task_git_head(root)
        expected_output=task_directory(root,target,budget)
        initial_state=capture_task_state(root,head)
        previous=None
        if (expected_output/'task-baseline.json').exists():
            previous=load_task_baseline(expected_output/'task-baseline.json')
            load_task_specification(expected_output/'task-spec.json')
            previous_obligations=load_task_obligations(expected_output/'task-obligations.json')
            previous_spec_raw=read_bounded(expected_output/'task-spec.json',MAX_SPEC_BYTES,'Task Specification')
            previous_obligations_raw=read_bounded(expected_output/'task-obligations.json',MAX_OBLIGATIONS_BYTES,'Task Obligations')
            candidate=build_task_baseline(head,initial_state,previous_spec_raw,previous_obligations_raw)
            if previous!=candidate or previous_obligations['task_spec_sha256']!=candidate['task_spec_sha256']:
                raise TaskBaselineError('existing task baseline differs; implicit lifecycle reset is forbidden')
    layer=load_object_layer(root)
    target_binding=snapshot_task_target(root,layer,target) if not sync else None
    imp=impact(root,target)
    allowed=[str(Path('knowledge')/Path(target))] if False else []
    # canonical object file + deterministic impact docs; executor may request scope expansion rather than editing outside this set
    objs=layer.objects
    if target in objs: allowed.append(objs[target]['path'].relative_to(root).as_posix())
    allowed += [x for x in imp['check_docs'] if (Path(root)/x).exists()]
    kind='sync' if sync else 'task'
    selected=list(skills or [])
    if skills_enabled(config):
        selected += default_skill_names_for_target(
            objs[target]['data'],mode=mode,sync=sync,config=config,
        )
    output,manifest=build_context(root,target,budget,mode,allowed_write_set=list(dict.fromkeys(allowed)),kind=kind,skill_names=list(dict.fromkeys(selected)))
    if not sync:
        specification=build_task_specification(config,head,target_binding,mode,manifest)
        if previous is not None:
            if serialize_task_specification(specification).encode('utf-8')!=previous_spec_raw:
                raise TaskBaselineError('existing Task Specification differs; implicit lifecycle reset is forbidden')
            obligations=build_task_obligations(root,Path(output)/'task-spec.json',target)
            if serialize_task_obligations(obligations).encode('utf-8')!=previous_obligations_raw:
                raise TaskBaselineError('existing Task Obligations differ; implicit lifecycle reset is forbidden')
        else:
            task_directory(root,target,budget)
            write_task_specification(output,specification)
            write_task_obligations(output,build_task_obligations(root,Path(output)/'task-spec.json',target))
        if task_git_head(root)!=head or capture_task_state(root,head)!=initial_state:
            raise TaskBaselineError('project state changed during task creation')
        task_directory(root,target,budget)
        spec_raw=read_bounded(Path(output)/'task-spec.json',MAX_SPEC_BYTES,'Task Specification')
        obligations_raw=read_bounded(Path(output)/'task-obligations.json',MAX_OBLIGATIONS_BYTES,'Task Obligations')
        if (spec_raw!=serialize_task_specification(specification).encode('utf-8')
                or obligations_raw!=serialize_task_obligations(
                    build_task_obligations(root,Path(output)/'task-spec.json',target)).encode('utf-8')):
            raise TaskBaselineError('task artifacts changed during creation')
        baseline=build_task_baseline(
            head,initial_state,spec_raw,obligations_raw,
        )
        if previous is not None and (baseline!=previous or load_task_baseline(Path(output)/'task-baseline.json')!=previous):
            raise TaskBaselineError('existing task lifecycle changed during creation')
        write_task_artifact(root,Path(output)/'task-baseline.json',serialize_task_baseline(baseline))
    return output,manifest

def bootstrap(root,budget='medium',skills=None):
    selected=list(skills or [])
    if skills_enabled(load_yaml(Path(root)/'project.yaml')):
        selected.append('knowledge-sync')
    return build_context(root,'bootstrap',budget,'sync',allowed_write_set=['docs/**','knowledge/**','inbox/**'],kind='bootstrap',skill_names=list(dict.fromkeys(selected)))

def prepare_pr(root):
    from .validation import validate, counts
    issues=validate(root); c=counts(issues); changed=changed_files(root)
    p=Path(root)/'.generated/reports/PR_DESCRIPTION.md'; p.parent.mkdir(parents=True,exist_ok=True)
    txt='# Pull Request Draft\n\n## What changed\n\n'+('\n'.join(f'- `{x}`' for x in changed) if changed else '_No Git diff detected._')+'\n\n## Why\n\n_TODO._\n\n## Change class\n\n_TODO: A / B / C / D\n\n## Knowledge objects\n\n_TODO._\n\n## Impact\n\n_TODO._\n\n## Tests / validation\n\n'+f"- BLOCKING: {c['BLOCKING']}\n- ERROR: {c['ERROR']}\n- WARNING: {c['WARNING']}\n\n## Human approval required\n\n_TODO._\n\n## Risks / drift\n\n_TODO._\n"
    p.write_text(txt,encoding='utf-8'); return p
