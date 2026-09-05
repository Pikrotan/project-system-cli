import argparse, sys, json
from pathlib import Path
from . import __version__
from .utils import find_root
from .init_project import init_project
from .objects import create_object, DIRS
from .validation import validate, counts
from .generation import generate, GenerationBlockedError
from .object_loader import load_object_layer
from .context import build_context
from .impact import impact
from .health import health
from .modules import catalog, enable, disable
from .tasking import task, bootstrap, prepare_pr
from .sync_planning import plan_sync, SyncPlanError
from .sync_verification import verify_sync, SyncVerifyError
from .sync_finalization import finalize_sync, SyncFinalizeError

TYPES=list(DIRS)

def print_issues(issues):
    if not issues: print('PASS: no validation issues'); return
    for sev,loc,msg in issues: print(f'{sev:8} {loc}: {msg}')
    c=counts(issues); print(f"\nBLOCKING {c['BLOCKING']} | ERROR {c['ERROR']} | WARNING {c['WARNING']} | INFO {c['INFO']}")

def print_object_counts(root):
    by_type=load_object_layer(root).counts_by_type()
    print(f"\nObjects: {sum(by_type.values())}")
    for object_type,count in sorted(by_type.items()):
        print(f'- {object_type}: {count}')

def main(argv=None):
    p=argparse.ArgumentParser(prog='project',description='Project Template v1.1 CLI')
    p.add_argument('--version',action='version',version=f'project-system-cli {__version__}')
    sp=p.add_subparsers(dest='cmd',required=True)
    q=sp.add_parser('init'); q.add_argument('name'); q.add_argument('--path',default=None); q.add_argument('--type',default='other',choices=['mobile_app','web_app','desktop_app','game','saas','platform','backend_service','library','prototype','other']); q.add_argument('--governance',default='solo',choices=['solo','small_team','strict_team']); q.add_argument('--full-docs',action='store_true')
    q=sp.add_parser('new'); q.add_argument('type',choices=TYPES); q.add_argument('--title',required=True); q.add_argument('--domain',default='general'); q.add_argument('--owner',default='owner')
    q=sp.add_parser('validate'); q.add_argument('--changed',action='store_true',help='Accepted for workflow compatibility; validates the whole knowledge graph.')
    sp.add_parser('generate')
    q=sp.add_parser('context'); q.add_argument('target'); q.add_argument('--budget',choices=['small','medium','large'],default='medium'); q.add_argument('--mode',default='review')
    q=sp.add_parser('impact'); q.add_argument('target')
    sp.add_parser('health')
    sp.add_parser('modules')
    q=sp.add_parser('enable'); q.add_argument('module')
    q=sp.add_parser('disable'); q.add_argument('module')
    q=sp.add_parser('task'); q.add_argument('target'); q.add_argument('--budget',choices=['small','medium','large'],default='medium'); q.add_argument('--mode',default='implement')
    q=sp.add_parser('sync'); q.add_argument('target',help='existing object ID, "plan", "verify", or "finalize"'); q.add_argument('pack',nargs='?',help='SYNC PACK path or pack_id'); q.add_argument('--budget',choices=['small','medium','large'],default='medium'); q.add_argument('--commit',action='store_true',help='explicitly commit the verified canonical state'); q.add_argument('--push',action='store_true',help='explicitly push an already verified SYNC commit'); q.add_argument('--message',help='custom commit message; valid only with --commit')
    q=sp.add_parser('bootstrap'); q.add_argument('--budget',choices=['small','medium','large'],default='medium')
    sp.add_parser('prepare-pr')
    args=p.parse_args(argv)
    if args.cmd=='init':
        path=args.path or ('./'+args.name); r=init_project(args.name,path,args.type,args.governance,args.full_docs); print(r); return
    root=find_root()
    if args.cmd=='new':
        path,oid=create_object(root,args.type,args.title,args.domain,args.owner); print(f'{oid}\n{path.relative_to(root)}')
    elif args.cmd=='validate':
        issues=validate(root); print_issues(issues); print_object_counts(root); sys.exit(2 if any(x[0] in {'BLOCKING','ERROR'} for x in issues) else 0)
    elif args.cmd=='generate':
        try: print(generate(root))
        except GenerationBlockedError as exc:
            print(str(exc),file=sys.stderr); sys.exit(2)
    elif args.cmd=='context':
        out,_=build_context(root,args.target,args.budget,args.mode); print(out)
    elif args.cmd=='impact': print(json.dumps(impact(root,args.target),indent=2,ensure_ascii=False))
    elif args.cmd=='health':
        c,by,issues=health(root); print(f"BLOCKING {c['BLOCKING']}\nERROR {c['ERROR']}\nWARNING {c['WARNING']}\nINFO {c['INFO']}"); print('\nObjects:'); [print(f'- {k}: {v}') for k,v in sorted(by.items())]
    elif args.cmd=='modules':
        from .utils import load_yaml; cfg=load_yaml(root/'project.yaml'); enabled=cfg.get('modules',{}); cats=catalog()
        for name,spec in sorted(cats.items()): print(('✓' if enabled.get(name,{}).get('enabled') else '○'),name,f"[{spec.get('category')}]",('requires '+','.join(spec.get('requires',[])) if spec.get('requires') else ''))
    elif args.cmd=='enable':
        created=enable(root,args.module); print('Enabled',args.module); [print('+',x) for x in created]
    elif args.cmd=='disable':
        notes=disable(root,args.module); print('Disabled',args.module); [print(x) for x in notes]
    elif args.cmd=='task':
        out,_=task(root,args.target,args.mode,args.budget,False); print(out)
    elif args.cmd=='sync':
        if args.target=='plan':
            if not args.pack: p.error('project sync plan requires <pack>')
            if args.commit or args.push or args.message is not None: p.error('sync plan does not accept finalize options')
            try: out,_=plan_sync(root,args.pack); print(out)
            except SyncPlanError as exc:
                print(f'sync plan failed: {exc}',file=sys.stderr); sys.exit(2)
        elif args.target=='verify':
            if not args.pack: p.error('project sync verify requires <pack-or-pack-id>')
            if args.commit or args.push or args.message is not None: p.error('sync verify does not accept finalize options')
            try: out,_=verify_sync(root,args.pack); print(out)
            except SyncVerifyError as exc:
                print(f'sync verify failed [{exc.category}]: {exc}',file=sys.stderr)
                sys.exit(exc.exit_code)
        elif args.target=='finalize':
            if not args.pack: p.error('project sync finalize requires <pack-or-pack-id>')
            try:
                out,_=finalize_sync(
                    root,
                    args.pack,
                    commit=args.commit,
                    push=args.push,
                    message=args.message,
                )
                print(out)
            except SyncFinalizeError as exc:
                print(f'sync finalize failed [{exc.category}]: {exc}',file=sys.stderr)
                sys.exit(exc.exit_code)
        else:
            if args.pack: p.error('legacy project sync accepts one object ID')
            if args.commit or args.push or args.message is not None: p.error('legacy project sync does not accept finalize options')
            out,_=task(root,args.target,'sync',args.budget,True); print(out)
    elif args.cmd=='bootstrap':
        out,_=bootstrap(root,args.budget); print(out)
    elif args.cmd=='prepare-pr': print(prepare_pr(root))
