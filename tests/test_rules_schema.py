import json

from jsonschema import Draft202012Validator, FormatChecker

from project_system.utils import distribution_root


def validator(name):
    schema=json.loads((distribution_root()/'schemas'/name).read_text(encoding='utf-8'))
    return Draft202012Validator(schema,format_checker=FormatChecker())


def valid_rules():
    return {
        'schema_version':1,
        'profile':'project-system-rules-v1',
        'rules':{
            'ARCH-001':{
                'title':'Architecture boundary',
                'status':'active',
                'category':'architecture',
                'description':'Domain boundary must hold.',
                'verification':{'method':'deterministic','checker':'repository.required_path'},
                'enforcement':{'severity':'ERROR','checkpoints':['project_validate']},
                'exception_policy':'decision_required',
            }
        },
    }


def valid_exceptions():
    return {
        'schema_version':1,
        'profile':'project-system-rule-exceptions-v1',
        'exceptions':{
            'EXC-20260926-deadbeef':{
                'rule_id':'ARCH-001',
                'state':'active',
                'mode':'temporary',
                'reason':'Temporary migration exception.',
                'scope':{'paths':['lib/**']},
                'decision_id':'DEC-20260926-deadbeef',
                'approved_by':'owner',
                'approved_at':'2026-09-26T00:00:00Z',
                'expires_at':'2026-10-01T00:00:00Z',
            }
        },
    }


def test_rules_schema_accepts_valid_deterministic_rule():
    assert not list(validator('rules.schema.json').iter_errors(valid_rules()))


def test_rules_schema_rejects_deterministic_without_checker():
    data=valid_rules()
    del data['rules']['ARCH-001']['verification']['checker']
    assert list(validator('rules.schema.json').iter_errors(data))


def test_rules_schema_rejects_ai_with_checker():
    data=valid_rules()
    data['rules']['ARCH-001']['verification']={'method':'ai','checker':'repository.required_path'}
    assert list(validator('rules.schema.json').iter_errors(data))


def test_rules_schema_rejects_invalid_rule_id():
    data=valid_rules()
    data['rules']['bad-id']=data['rules'].pop('ARCH-001')
    assert list(validator('rules.schema.json').iter_errors(data))


def test_rule_exceptions_schema_requires_expiry_for_temporary():
    data=valid_exceptions()
    del data['exceptions']['EXC-20260926-deadbeef']['expires_at']
    assert list(validator('rule-exceptions.schema.json').iter_errors(data))


def test_rule_exceptions_schema_forbids_expiry_for_permanent():
    data=valid_exceptions()
    item=data['exceptions']['EXC-20260926-deadbeef']
    item['mode']='permanent'
    assert list(validator('rule-exceptions.schema.json').iter_errors(data))
