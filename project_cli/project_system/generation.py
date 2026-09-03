from pathlib import Path
import json
from .graph import build_graph, load_objects
from .validation import validate, counts

TYPE_DIRS=['decisions','requirements','features','questions','risks','experiments','screens','flows','entities','metrics','design_changes','debts']

def generate(root):
    root=Path(root); base=root/'.generated'; (base/'indexes').mkdir(parents=True,exist_ok=True); (base/'graphs').mkdir(parents=True,exist_ok=True); (base/'reports').mkdir(parents=True,exist_ok=True)
    objs,edges,reverse=build_graph(root)
    bytype={}
    for oid,item in objs.items(): bytype.setdefault(item['data'].get('type','unknown'),[]).append(item['data'])
    for t,items in bytype.items():
        rows=['| ID | Title | Status | Domain | Owner |','|---|---|---|---|---|']
        for d in sorted(items,key=lambda x:x['id']): rows.append(f"| {d['id']} | {str(d.get('title','')).replace('|','/')} | {d.get('status','')} | {d.get('domain','')} | {d.get('owner','')} |")
        (base/'indexes'/f'{t.upper()}S.md').write_text(f'# {t.replace("_"," ").title()} Index\n\n> Generated. Do not edit manually.\n\n'+'\n'.join(rows)+'\n',encoding='utf-8')
    project_rows=['| Path | Kind | ID/Title | Status |','|---|---|---|---|']
    for oid,item in sorted(objs.items()): project_rows.append(f"| {item['path'].relative_to(root)} | {item['data'].get('type')} | {oid} | {item['data'].get('status','')} |")
    for p in sorted((root/'docs').rglob('*.md')) if (root/'docs').exists() else []:
        title=next((line[2:].strip() for line in p.read_text(encoding='utf-8').splitlines() if line.startswith('# ')),p.stem)
        project_rows.append(f'| {p.relative_to(root)} | narrative | {title} | active |')
    (base/'indexes'/'PROJECT_MAP.md').write_text('# Project Map\n\n> Generated. Do not edit manually.\n\n'+'\n'.join(project_rows)+'\n',encoding='utf-8')
    graph={'nodes':[{'id':oid,'type':v['data'].get('type'),'status':v['data'].get('status')} for oid,v in sorted(objs.items())],'edges':edges,'reverse':dict(reverse)}
    (base/'graphs'/'DEPENDENCY_GRAPH.json').write_text(json.dumps(graph,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    (base/'graphs'/'TRACEABILITY.json').write_text(json.dumps({'edges':edges},indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    issues=validate(root); cc=counts(issues)
    lines=['# Project Health','', '> Generated. No scalar score by default.','',*(f'- {k}: {cc[k]}' for k in ['BLOCKING','ERROR','WARNING','INFO']),'','## Object Counts','']
    for t,items in sorted(bytype.items()): lines.append(f'- {t}: {len(items)}')
    (base/'reports'/'PROJECT_HEALTH.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    vr=['# Validation Report','', '> Generated. Do not edit manually.','']+[f'- **{sev}** `{loc}` — {msg}' for sev,loc,msg in issues]
    if not issues: vr.append('- No issues.')
    (base/'reports'/'VALIDATION_REPORT.md').write_text('\n'.join(vr)+'\n',encoding='utf-8')
    return base
