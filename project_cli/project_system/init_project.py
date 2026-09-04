from pathlib import Path
import shutil, yaml
from . import __version__
from .utils import distribution_root, slugify

CORE_DOCS=[
'00_CURRENT_STATE.md','01_VISION.md','02_SCOPE.md','03_PRODUCT.md','04_ARCHITECTURE.md','05_ROADMAP.md','06_GLOSSARY.md','07_CONTEXT_MAP.md','08_WORKFLOW.md','09_DOCUMENTATION_PROCESS.md','10_DECISION_PROCESS.md',
'technical/TECH_OVERVIEW.md','technical/SYSTEM_ARCHITECTURE.md','design/DESIGN_OVERVIEW.md','business/BUSINESS_MODEL.md','research/MARKET_AND_COMPETITORS.md','operations/DEVELOPMENT_PROCESS.md','operations/TEAM_AND_RESPONSIBILITIES.md']

def init_project(name,path,project_type='other',governance='solo',full_docs=False):
    root=Path(path).resolve(); root.mkdir(parents=True,exist_ok=True); dist=distribution_root()
    for srcname,dstname in [('templates/AGENTS.md','AGENTS.md'),('templates/PROJECT_RULES.md','PROJECT_RULES.md')]: shutil.copy2(dist/srcname,root/dstname)
    readme=(dist/'templates/README.project.md').read_text(encoding='utf-8').replace('{{PROJECT_NAME}}',name); (root/'README.md').write_text(readme,encoding='utf-8')
    cfg={'project':{'id':slugify(name),'name':name,'template_version':'1.1','type':project_type},'governance_mode':governance,'tooling':{'project_cli':__version__,'schema_version':1},'modules':{},'external_systems':{'github':{'enabled':False,'mode':'sync'},'figma':{'enabled':False,'mode':'reference'},'designer_docs':{'enabled':False,'provider':'google_docs','mode':'projection'},'design_changes':{'enabled':False,'provider':'google_sheets','mode':'input_output'}},'ai':{'default_context_budget':'medium','allow_history_context':False,'allow_blueprint_context':False},'validation':{'strict_ids':True,'dependency_graph':True,'human_approval_checks':True},'drift_policy':{'blocking_allowed':0,'errors_allowed':0}}
    (root/'project.yaml').write_text(yaml.safe_dump(cfg,sort_keys=False,allow_unicode=True),encoding='utf-8')
    for fname in ['impact.yaml','retrieval.yaml','governance.yaml']:
        p=root/'.project/policies'/fname; p.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(dist/'policy_templates'/fname,p)
    (root/'.project/schema_overrides').mkdir(parents=True,exist_ok=True); (root/'.project/schema_overrides/.gitkeep').write_text('',encoding='utf-8')
    docs=list((dist/'narrative_templates').rglob('*.md')) if full_docs else [dist/'narrative_templates'/x for x in CORE_DOCS]
    for s in docs:
        rel=s.relative_to(dist/'narrative_templates'); d=root/'docs'/rel; d.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(s,d)
    for d in ['decisions','requirements','features','questions','risks','experiments','screens','flows','entities','metrics','design_changes','debts']:
        p=root/'knowledge'/d; p.mkdir(parents=True,exist_ok=True); (p/'.gitkeep').write_text('',encoding='utf-8')
    for d in ['general','design','research','feedback','sync']:
        p=root/'inbox'/d; p.mkdir(parents=True,exist_ok=True); (p/'.gitkeep').write_text('',encoding='utf-8')
    for d in ['retrospectives','imported','external_research','migrations','legacy']:
        p=root/'history'/d; p.mkdir(parents=True,exist_ok=True); (p/'.gitkeep').write_text('',encoding='utf-8')
    (root/'.generated').mkdir(exist_ok=True); (root/'.generated/.gitkeep').write_text('',encoding='utf-8')
    gh=root/'.github'; (gh/'workflows').mkdir(parents=True,exist_ok=True); shutil.copy2(dist/'github_templates/PULL_REQUEST_TEMPLATE.md',gh/'PULL_REQUEST_TEMPLATE.md'); shutil.copy2(dist/'github_templates/CODEOWNERS',gh/'CODEOWNERS'); shutil.copy2(dist/'github_templates/workflows/project-validate.yml',gh/'workflows/project-validate.yml')
    (root/'.gitignore').write_text('.generated/*\n!.generated/.gitkeep\n.env\n.env.*\n__pycache__/\n.pytest_cache/\n',encoding='utf-8')
    (root/'.llmignore').write_text('history/**\n.generated/**\nbuild/**\ndist/**\nnode_modules/**\ncoverage/**\n',encoding='utf-8')
    return root
