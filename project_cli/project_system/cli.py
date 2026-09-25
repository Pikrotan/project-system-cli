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
from .sync_intake import intake_sync, SyncIntakeError
from .sync_pull import pull_sync, SyncPullError
from .sync_migration import migrate_bindings, SyncMigrationError
from .sync_pickup import BLOCKED_STATUSES, pickup_once
from .sync_watcher import (
    DEFAULT_INTERVAL_SECONDS, SyncWatcherError, run_watcher,
)
from .sync_auto import (
    SyncAutoError, install_auto, remove_auto, run_auto, status_auto,
)
from .google_credentials import GoogleCredentialManager, GoogleError
from .google_workspace import (
    initialize_workspace, rebind_workspace, sync_workspace, workspace_status,
)
from .skills import SkillError, inspect_skill_layer, install_skills

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
    q=sp.add_parser('context'); q.add_argument('target'); q.add_argument('--budget',choices=['small','medium','large'],default='medium'); q.add_argument('--mode',default='review'); q.add_argument('--skill',action='append',default=[])
    q=sp.add_parser('impact'); q.add_argument('target')
    sp.add_parser('health')
    sp.add_parser('modules')
    q=sp.add_parser('enable'); q.add_argument('module')
    q=sp.add_parser('disable'); q.add_argument('module')
    q=sp.add_parser('task'); q.add_argument('target'); q.add_argument('--budget',choices=['small','medium','large'],default='medium'); q.add_argument('--mode',default='implement'); q.add_argument('--skill',action='append',default=[])
    q=sp.add_parser('sync'); q.add_argument('target',help='existing object ID, "auto", "watch", "pull", "intake", "plan", "verify", "finalize", or "migrate-bindings"'); q.add_argument('pack',nargs='?',help='SYNC action, REQUEST/PACK path, or pack_id'); q.add_argument('--budget',choices=['small','medium','large'],default='medium'); q.add_argument('--commit',action='store_true',help='explicitly commit the verified canonical state'); q.add_argument('--push',action='store_true',help='explicitly push an already verified SYNC commit'); q.add_argument('--message',help='custom commit message; valid only with --commit'); q.add_argument('--complete',action='store_true',help='explicitly complete a non-commit GitHub transport outcome'); q.add_argument('--outcome',choices=['reviewed-no-change','rejected','abandoned'],help='terminal outcome; requires --complete'); q.add_argument('--reason',help='required human reason for --complete'); q.add_argument('--apply',action='store_true',help='apply a deterministic sync binding migration audit'); q.add_argument('--plan',action='store_true',help='plan the bound pack after intake/pull'); q.add_argument('--issue',type=int,help='select one GitHub transport Issue; valid only with sync pull'); q.add_argument('--once',action='store_true',help='run exactly one bounded automatic pickup cycle; valid only with sync watch'); q.add_argument('--interval',type=int,help='watcher/scheduler interval in seconds (60..3600, default 120)'); q.add_argument('--replace',action='store_true',help='replace this project automatic SYNC registration after ownership proof'); q.add_argument('--json',dest='json_output',action='store_true',help='emit machine-readable automatic SYNC status'); q.add_argument('--registration',help='validated automatic SYNC registration ID; internal run command only')
    q=sp.add_parser('google',help='Google Workspace / Designer Bridge')
    google_commands=q.add_subparsers(dest='google_command',required=True)
    connect=google_commands.add_parser('connect',help='authorize with Google OAuth Desktop App')
    connect.add_argument('--credentials',help='path to Google OAuth Desktop App client JSON')
    google_commands.add_parser('status',help='show protected local authorization status')
    google_commands.add_parser('disconnect',help='remove protected local Google credentials')
    workspace=google_commands.add_parser('workspace',help='manage this project Google Workspace')
    workspace_commands=workspace.add_subparsers(dest='workspace_command',required=True)
    workspace_commands.add_parser('init',help='create and bind project Google resources')
    workspace_commands.add_parser('status',help='verify resource binding and projection drift')
    workspace_commands.add_parser('rebind',help='recover an unambiguous metadata-bound workspace')
    workspace_commands.add_parser('sync',help='run one deterministic projection/import cycle')
    q=sp.add_parser('bootstrap',help='prepare a knowledge-bootstrap context pack'); q.add_argument('--budget',choices=['small','medium','large'],default='medium'); q.add_argument('--skill',action='append',default=[])
    q=sp.add_parser('skills',help='inspect, validate, or install project Skills')
    skills_commands=q.add_subparsers(dest='skills_command',required=True)
    skills_commands.add_parser(
        'list',
        help='list installed project Skills',
        description='List the registered project Skills and their portable trigger descriptions without executing them.',
    )
    skills_commands.add_parser(
        'validate',
        help='validate the project Skills layer',
        description='Validate the Skills registry, entrypoints, paths, capabilities, write ceilings, and required project profile.',
    )
    install=skills_commands.add_parser(
        'install',
        help='plan or apply Skills installation for this project',
        description='Plan project Skills installation without writing by default; use --apply to perform the validated migration.',
    )
    install.add_argument('--apply',action='store_true',help='apply the planned Skills installation (default: dry-run only)')
    sp.add_parser('prepare-pr')
    args=p.parse_args(argv)
    if args.cmd=='init':
        path=args.path or ('./'+args.name); r=init_project(args.name,path,args.type,args.governance,args.full_docs); print(r); return
    if args.cmd=='google' and args.google_command in {'connect','status','disconnect'}:
        try:
            manager=GoogleCredentialManager()
            if args.google_command=='connect': report=manager.connect(args.credentials)
            elif args.google_command=='status': report=manager.status()
            else: report=manager.disconnect()
            print(json.dumps(report,sort_keys=True,ensure_ascii=False))
        except GoogleError as exc:
            print(f'google {args.google_command} failed [{exc.category}]: {exc}',file=sys.stderr)
            sys.exit(exc.exit_code)
        return
    if args.cmd=='sync' and args.target=='auto' and args.pack=='run':
        if not args.registration: p.error('project sync auto run requires --registration REG-ID')
        if args.interval is not None or args.replace or args.json_output or args.once or args.plan or args.issue is not None or args.apply or args.commit or args.push or args.message is not None or args.complete or args.outcome or args.reason: p.error('sync auto run accepts only --registration REG-ID')
        try:
            report=run_auto(args.registration)
            print(json.dumps(report,sort_keys=True,ensure_ascii=False))
        except SyncAutoError as exc:
            print(f'sync auto run failed: {exc}',file=sys.stderr); sys.exit(exc.exit_code)
        return
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
        try: out,_=build_context(root,args.target,args.budget,args.mode,skill_names=args.skill); print(out)
        except SkillError as exc: print(f'context failed: {exc}',file=sys.stderr); sys.exit(2)
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
        try: out,_=task(root,args.target,args.mode,args.budget,False,args.skill); print(out)
        except SkillError as exc: print(f'task failed: {exc}',file=sys.stderr); sys.exit(2)
    elif args.cmd=='google':
        action=args.workspace_command
        try:
            if action=='init': report=initialize_workspace(root)
            elif action=='status': report=workspace_status(root)
            elif action=='rebind': report=rebind_workspace(root)
            else: report=sync_workspace(root)
            print(json.dumps(report,sort_keys=True,ensure_ascii=False))
        except GoogleError as exc:
            print(f'google workspace {action} failed [{exc.category}]: {exc}',file=sys.stderr)
            sys.exit(exc.exit_code)
    elif args.cmd=='sync':
        if args.plan and args.target not in {'intake','pull'}: p.error('--plan is valid only with sync intake/pull')
        if args.issue is not None and args.target != 'pull': p.error('--issue is valid only with sync pull')
        if args.apply and args.target != 'migrate-bindings': p.error('--apply is valid only with sync migrate-bindings')
        if args.once and args.target != 'watch': p.error('--once is valid only with sync watch')
        if args.interval is not None and args.target not in {'watch','auto'}: p.error('--interval is valid only with sync watch or sync auto install')
        if (args.replace or args.json_output or args.registration is not None) and args.target != 'auto': p.error('--replace/--json/--registration are valid only with sync auto')
        if (args.complete or args.outcome is not None or args.reason is not None) and args.target != 'finalize': p.error('--complete/--outcome/--reason are valid only with sync finalize')
        if args.target=='auto':
            action=args.pack
            if action not in {'install','status','remove'}: p.error('project sync auto requires install, status, remove, or run')
            common_invalid=args.commit or args.push or args.message is not None or args.complete or args.outcome or args.reason or args.apply or args.plan or args.issue is not None or args.once or args.registration is not None
            if common_invalid: p.error('sync auto install/status/remove do not accept other sync workflow options')
            if action!='install' and (args.interval is not None or args.replace): p.error('--interval/--replace are valid only with sync auto install')
            if action!='status' and args.json_output: p.error('--json is valid only with sync auto status')
            try:
                if action=='install':
                    interval=args.interval if args.interval is not None else DEFAULT_INTERVAL_SECONDS
                    report=install_auto(root,interval=interval,replace=args.replace)
                    registration=report['registration']
                    print(f'Registration: {registration["registration_id"]}\nProject: {registration["project_id"]}\nRepository: {registration["repository"]}\nTask: {registration["task_name"]}\nInterval: {registration["interval_seconds"]}\nStatus: {report["status"]}')
                elif action=='status':
                    report=status_auto(root)
                    if args.json_output:
                        print(json.dumps(report,sort_keys=True,ensure_ascii=False))
                    else:
                        print(f'Registration: {report["registration_id"]}\nProject: {report["project_id"]}\nRepository: {report["repository"]}\nTask: {report["task_name"]}\nInstalled: {"yes" if report["installed"] else "no"}\nTask state: {report["task_state"]}\nInterval: {report["interval_seconds"]}\nLast cycle: {report["last_cycle_at"] or "never"}\nLast status: {report["last_cycle_status"] or "none"}\nLast issue: {report["last_issue"] or "none"}\nLast pack: {report["last_pack"] or "none"}\nLast error: {report["last_error_category"] or "none"}\nNext run: {report["next_scheduled_run"] or "unknown"}')
                        for warning in report['warnings']: print(f'Warning: {warning}')
                else:
                    report=remove_auto(root)
                    print(f'Registration: {report["registration_id"]}\nTask: {report["task_name"]}\nStatus: {report["status"]}')
            except SyncAutoError as exc:
                print(f'sync auto {action} failed: {exc}',file=sys.stderr); sys.exit(exc.exit_code)
        elif args.target=='watch':
            if args.pack or args.commit or args.push or args.message is not None or args.complete or args.outcome or args.reason or args.apply or args.plan or args.issue is not None: p.error('sync watch accepts only --once or --interval SECONDS')
            if args.once:
                if args.interval is not None: p.error('sync watch --once does not accept --interval')
                report=pickup_once(root)
                print(f'Repository: {report.get("repository") or "unknown"}\nStatus: {report["status"]}')
                if report.get('reason'): print(f'Reason: {report["reason"]}')
                if report.get('issue_number') is not None: print(f'Issue: {report["issue_number"]}')
                if report.get('pack_id'): print(f'Pack: {report["pack_id"]}')
                if report.get('plan'): print(f'Plan: {report["plan"]}')
                if report['status'] in BLOCKED_STATUSES: sys.exit(3)
            else:
                interval=args.interval if args.interval is not None else DEFAULT_INTERVAL_SECONDS
                last_display=[None]
                def show_cycle(report,state):
                    signature=(report['status'],report.get('issue_number'),report.get('pack_id'))
                    if signature==last_display[0]: return
                    last_display[0]=signature
                    print(f'Cycle: {state["last_cycle_at"]} | Status: {report["status"]}',flush=True)
                    if report.get('issue_number') is not None: print(f'Issue: {report["issue_number"]}',flush=True)
                    if report.get('pack_id'): print(f'Pack: {report["pack_id"]}',flush=True)
                    if report['status'] in BLOCKED_STATUSES and report.get('reason'): print(f'Reason: {report["reason"]}',flush=True)
                    print(f'Next delay: {state["current_delay_seconds"]} seconds',flush=True)
                try:
                    print(f'Persistent foreground watcher starting (interval {interval} seconds)',flush=True)
                    report=run_watcher(root,interval,on_cycle=show_cycle)
                    print(f'Watcher stopped after {report["cycles"]} cycle(s)',flush=True)
                    if report['interrupted']: sys.exit(130)
                except SyncWatcherError as exc:
                    print(f'sync watch failed: {exc}',file=sys.stderr); sys.exit(exc.exit_code)
        elif args.target=='pull':
            if args.pack or args.commit or args.push or args.message is not None or args.complete or args.apply or args.once or args.interval is not None: p.error('sync pull accepts only --plan and --issue NUMBER')
            try:
                report=pull_sync(root,plan=args.plan,issue_number=args.issue)
                print(f'Repository: {report["repository"]}\nStatus: {report["status"]}')
                for warning in report.get('warnings', []): print(f'Warning: {warning}')
                if report['status']!='no_pending':
                    print(f'Issue: {report["transport"]["issue_number"]}\nPack: {report["pack_id"]}\nPath: {report["pack_path"]}\nBase: {report["base_commit"]}\nAcknowledgement: local_only')
                    if args.plan: print(f'Plan: {report["plan_result"]}')
            except SyncPullError as exc:
                print(f'sync pull failed: {exc}',file=sys.stderr); sys.exit(exc.exit_code)
        elif args.target=='intake':
            if not args.pack: p.error('project sync intake requires <request-path|->')
            if args.commit or args.push or args.message is not None or args.complete or args.apply or args.once or args.interval is not None: p.error('sync intake does not accept finalize options')
            try:
                _,report=intake_sync(root,args.pack,plan=args.plan)
                print(f'Pack: {report["pack_id"]}\nPath: {report["pack_path"]}\nBase: {report["base_commit"]}\nChanges: {report["change_count"]}\nStatus: {report["intake_result"]}')
                if args.plan:
                    print(f'Plan: {report["plan_result"]}\nAllowed writes: {report["allowed_write_count"]}')
            except SyncIntakeError as exc:
                print(f'sync intake failed: {exc}',file=sys.stderr); sys.exit(exc.exit_code)
        elif args.target=='plan':
            if not args.pack: p.error('project sync plan requires <pack>')
            if args.commit or args.push or args.message is not None or args.complete or args.apply or args.once or args.interval is not None: p.error('sync plan does not accept finalize options')
            try: out,_=plan_sync(root,args.pack); print(out)
            except SyncPlanError as exc:
                print(f'sync plan failed: {exc}',file=sys.stderr); sys.exit(2)
        elif args.target=='verify':
            if not args.pack: p.error('project sync verify requires <pack-or-pack-id>')
            if args.commit or args.push or args.message is not None or args.complete or args.apply or args.once or args.interval is not None: p.error('sync verify does not accept finalize options')
            try: out,_=verify_sync(root,args.pack); print(out)
            except SyncVerifyError as exc:
                print(f'sync verify failed [{exc.category}]: {exc}',file=sys.stderr)
                sys.exit(exc.exit_code)
        elif args.target=='finalize':
            if not args.pack: p.error('project sync finalize requires <pack-or-pack-id>')
            if args.interval is not None: p.error('sync finalize does not accept --interval')
            try:
                out,report=finalize_sync(
                    root,
                    args.pack,
                    commit=args.commit,
                    push=args.push,
                    message=args.message,
                    complete=args.complete,
                    outcome=args.outcome,
                    reason=args.reason,
                )
                print(out)
                if report.get('transport_state') == 'awaiting_push':
                    print(f'Status: committed / awaiting_push\nPack: {report["pack_id"]}')
                elif report.get('state') == 'completed':
                    print(f'Status: completed\nOutcome: {report.get("terminal_outcome") or report.get("outcome")}\nPack: {report["pack_id"]}')
            except SyncFinalizeError as exc:
                print(f'sync finalize failed [{exc.category}]: {exc}',file=sys.stderr)
                sys.exit(exc.exit_code)
        elif args.target=='migrate-bindings':
            if args.pack: p.error('project sync migrate-bindings does not accept a pack selector')
            if args.once or args.interval is not None: p.error('sync migrate-bindings does not accept --once/--interval')
            if args.commit or args.push or args.message is not None or args.complete or args.outcome or args.reason: p.error('sync migrate-bindings accepts only --apply')
            try:
                results=migrate_bindings(root,apply=args.apply)
                print('Mode:', 'apply' if args.apply else 'audit')
                if not results: print('No SYNC bindings found')
                for item in results:
                    print(f'{item["pack_id"]}: {item["status"]}' + (f' — {item["detail"]}' if item.get('detail') else ''))
                if any(item['status']=='conflict' for item in results): sys.exit(3)
            except SyncMigrationError as exc:
                print(f'sync migrate-bindings failed: {exc}',file=sys.stderr); sys.exit(exc.exit_code)
        else:
            if args.pack: p.error('legacy project sync accepts one object ID')
            if args.commit or args.push or args.message is not None or args.complete or args.outcome or args.reason or args.apply or args.once or args.interval is not None: p.error('legacy project sync does not accept finalize options')
            try: out,_=task(root,args.target,'sync',args.budget,True); print(out)
            except SkillError as exc: print(f'sync failed: {exc}',file=sys.stderr); sys.exit(2)
    elif args.cmd=='bootstrap':
        try: out,_=bootstrap(root,args.budget,args.skill); print(out)
        except SkillError as exc: print(f'bootstrap failed: {exc}',file=sys.stderr); sys.exit(2)
    elif args.cmd=='skills':
        if args.skills_command=='install':
            try:
                report=install_skills(root,apply=args.apply)
                print(json.dumps(report,indent=2,sort_keys=True,ensure_ascii=False))
            except SkillError as exc:
                print(f'skills install failed: {exc}',file=sys.stderr); sys.exit(2)
        else:
            layer=inspect_skill_layer(root)
            if args.skills_command=='list':
                print(f'Profile: {layer.registry.get("profile") if layer.registry else "legacy"}')
                for name,record in sorted(layer.records.items()):
                    print(f'- {name}: {record.description}')
            else:
                print_issues(layer.issues)
                sys.exit(2 if any(x[0] in {'BLOCKING','ERROR'} for x in layer.issues) else 0)
    elif args.cmd=='prepare-pr': print(prepare_pr(root))
