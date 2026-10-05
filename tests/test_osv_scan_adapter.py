import copy
from dataclasses import replace
import hashlib
import importlib
import json
import os
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace

import pytest

from project_system import verification_adapters as adapters


LOCKFILES = (
    'pubspec.lock', 'package-lock.json', 'pnpm-lock.yaml', 'yarn.lock',
    'bun.lock', 'uv.lock', 'poetry.lock', 'Pipfile.lock', 'pdm.lock', 'pylock.toml',
)
EXCLUDED = (
    '.git', '.generated', '.dart_tool', '.pub-cache', 'build', 'node_modules',
    '.venv', 'venv', '__pycache__', 'vendor', 'dist', '.pytest_cache',
)


def module():
    return importlib.import_module('project_system.osv_scan_adapter')


def write(root, relative='pubspec.lock'):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('fixture lockfile\n', encoding='utf-8')
    return path


def package(name='example', version='1.2.3', ecosystem='Pub', ids=()):
    value = {'package': {'ecosystem': ecosystem, 'name': name, 'version': version}}
    if ids:
        value['vulnerabilities'] = [
            {'id': item, 'summary': 'advisory prose', 'modified': '2026-01-01'}
            for item in ids
        ]
        value['groups'] = [{'ids': [item], 'aliases': [item],
                            'max_severity': 'arbitrary presentation'} for item in ids]
    return value


def output(relative='pubspec.lock', packages=None):
    return {'results': [{'source': {'type': 'lockfile', 'path': relative},
                         'packages': [package()] if packages is None else packages}]}


@pytest.fixture
def root(tmp_path):
    path = tmp_path / 'project'
    path.mkdir()
    return path


def fake_process(monkeypatch, root, payload, *, exit_code=0,
                 version='osv-scanner version: 2.3.3\ncommit: test\nbuilt at: now\n',
                 version_exit=0, stderr=''):
    adapter = module()
    tool = write(root.parent, 'tools/osv-scanner.exe')
    monkeypatch.setattr(adapter.shutil, 'which', lambda name: str(tool))
    calls = []

    def run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        if arguments[1:] == ['--version']:
            return subprocess.CompletedProcess(arguments, version_exit, version, '')
        config = Path(arguments[arguments.index('--config') + 1])
        assert config.is_file() and config.read_bytes() == b''
        assert not config.resolve().is_relative_to(root.resolve())
        assert Path(kwargs['cwd']) == config.parent
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.CompletedProcess(arguments, exit_code, text, stderr)

    monkeypatch.setattr(adapter, 'run_process', run)
    return calls, tool


def evaluate(root, paths=None):
    return adapters.evaluate_verification_adapter('osv.scan', root, evaluation_paths=paths)


def outcome(root, paths=None):
    from project_system.rule_checkers import evaluate_checker
    from project_system.rule_engine import RuleEvaluationContext

    return evaluate_checker(
        'code.verification',
        RuleEvaluationContext(root, 'sync_verify', {}, True, evaluation_paths=paths),
        {'adapter': 'osv.scan'},
    )


def test_osv_registry_contract_and_legacy_defaults():
    spec = adapters.VERIFICATION_ADAPTER_REGISTRY['osv.scan']
    assert (spec.adapter_id, spec.version) == ('osv.scan', '1')
    assert spec.implementation is module().run_osv_scan
    assert spec.executes_project_code is False
    assert spec.uses_semantic_hash is True
    assert spec.uses_network is True
    assert spec.global_input_patterns == ('**',)
    for name in ('dart.analyze', 'dart.test'):
        assert adapters.VERIFICATION_ADAPTER_REGISTRY[name].uses_network is False
    assert adapters.VerificationAdapterSpec('fake', '1', lambda *args: None).uses_network is False


@pytest.mark.parametrize('value', [None, 0, 1, 'true', [], {}])
def test_malformed_network_metadata_fails_closed(monkeypatch, value):
    spec = adapters.VERIFICATION_ADAPTER_REGISTRY['dart.analyze']
    monkeypatch.setattr(adapters, 'VERIFICATION_ADAPTER_REGISTRY', {
        'dart.analyze': replace(spec, uses_network=value),
    })
    with pytest.raises(adapters.VerificationAdapterError, match='uses_network'):
        adapters.resolve_verification_adapter_evaluation('dart.analyze', None)


def test_discovery_cross_ecosystem_sorted_and_exact(root):
    expected = ['pubspec.lock', 'web/package-lock.json', 'backend/uv.lock']
    for path in reversed(expected):
        write(root, path)
    for path in ('requirements.txt', 'pubspec.yaml', 'pyproject.toml', 'package.json', 'pom.xml'):
        write(root, path)
    assert module().discover_lockfiles(root) == tuple(sorted(expected))


@pytest.mark.parametrize('name', LOCKFILES)
def test_each_supported_resolved_lockfile_is_discovered(root, name):
    write(root, 'nested/' + name)
    assert module().discover_lockfiles(root) == ('nested/' + name,)


@pytest.mark.parametrize('directory', EXCLUDED)
def test_generated_cache_vendor_directories_are_excluded(root, directory):
    write(root, directory + '/nested/package-lock.json')
    assert module().discover_lockfiles(root) == ()


def test_unsupported_only_project_is_not_applicable_without_execution(root, monkeypatch):
    write(root, 'requirements.txt')
    write(root, 'package.json')
    monkeypatch.setattr(module(), 'run_process', lambda *a, **k: pytest.fail('unexpected execution'))
    result = evaluate(root)
    assert result.verification_status == 'NOT_APPLICABLE'
    assert result.tool_version == 'not_executed'


def test_empty_bounded_set_does_not_discover_or_execute(root, monkeypatch):
    monkeypatch.setattr(module(), 'discover_lockfiles', lambda *a: pytest.fail('unexpected discovery'))
    monkeypatch.setattr(module(), 'run_process', lambda *a, **k: pytest.fail('unexpected execution'))
    result = evaluate(root, ())
    assert result.verification_status == 'NOT_APPLICABLE'
    assert result.evaluation_mode == 'bounded'


@pytest.mark.parametrize('paths', [None, ('README.md',), ('assets/nested/golden.png',), ('pubspec.lock',)])
def test_safe_applicability_and_fixed_read_only_invocation(root, monkeypatch, paths):
    lock = write(root)
    write(root, 'osv-scanner.toml').write_text('[[IgnoredVulns]]\nid="OSV-1"\n', encoding='utf-8')
    calls, tool = fake_process(monkeypatch, root, output())
    result = evaluate(root, paths)
    assert result.verification_status == 'PASS'
    assert result.evaluation_mode == ('project_wide' if paths is None else 'project_wide_invalidation')
    assert len(calls) == 2
    version_argv, version_policy = calls[0]
    assert version_argv == [str(tool), '--version']
    argv, policy = calls[1]
    config = Path(argv[argv.index('--config') + 1])
    assert argv == [str(tool), 'scan', 'source', '--format=json', '--all-packages',
                    '--no-resolve', '--no-call-analysis=all', '--all-vulns',
                    '--config', str(config), '-L', str(lock)]
    assert not config.exists() and not config.parent.exists()
    for current in (policy, version_policy):
        assert current['shell'] is False and current['check'] is False
        assert current['encoding'] == 'utf-8' and current['capture_output'] is True
        assert 0 < current['timeout'] <= 300
        assert 0 < current['max_capture_bytes'] <= 16 * 1024 * 1024
    assert '-r' not in argv and '--recursive' not in argv
    assert not (root / '.generated').exists()


def test_discovery_rejects_symlink_escape(root):
    target = write(root.parent, 'outside/package-lock.json')
    try:
        (root / 'package-lock.json').symlink_to(target)
    except OSError:
        pytest.skip('symlink creation unavailable')
    with pytest.raises(adapters.VerificationAdapterError):
        module().discover_lockfiles(root)


def test_discovery_rejects_windows_reparse_attribute(root, monkeypatch):
    candidate = write(root)
    adapter = module()
    original = adapter.os.lstat

    def lstat(path, *args, **kwargs):
        value = original(path, *args, **kwargs)
        if Path(path) == candidate:
            return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=0x400)
        return value

    monkeypatch.setattr(adapter.os, 'lstat', lstat)
    with pytest.raises(adapters.VerificationAdapterError):
        adapter.discover_lockfiles(root)


def test_discovery_rejects_nonregular_named_lockfile(root):
    (root / 'pubspec.lock').mkdir()
    with pytest.raises(adapters.VerificationAdapterError):
        module().discover_lockfiles(root)


def test_discovery_rejects_ambiguous_lockfile_argument_separator(root):
    write(root, 'comma,directory/pubspec.lock')
    with pytest.raises(adapters.VerificationAdapterError):
        module().discover_lockfiles(root)


def test_project_root_comma_is_not_forwarded_as_a_lockfile_list(root, monkeypatch):
    ambiguous = root / 'comma,root'
    ambiguous.mkdir()
    write(ambiguous)
    monkeypatch.setattr(module(), 'run_process', lambda *a, **k: pytest.fail('unexpected execution'))
    assert outcome(ambiguous).raw_status == 'ERROR'


def test_pass_result_consumer_boundary(root, monkeypatch):
    write(root)
    fake_process(monkeypatch, root, output(packages=[package(), package('other')]))
    result = evaluate(root)
    assert result.verification_status == 'PASS' and result.findings == ()
    assert len(result.semantic_sha256) == 64
    assert adapters.validate_verification_adapter_result(result, expected_adapter_id='osv.scan') == result


def test_vulnerabilities_are_concrete_sanitized_failures(root, monkeypatch):
    write(root)
    payload = output(packages=[package(ids=('OSV-2', 'OSV-1')), package('another', ids=('OSV-3',))])
    fake_process(monkeypatch, root, payload, exit_code=1, stderr='secret absolute-path advisory prose')
    result = evaluate(root)
    assert result.verification_status == 'FAIL'
    assert len(result.findings) == 3
    for finding in result.findings:
        assert finding.path == 'pubspec.lock'
        assert finding.severity == 'ERROR' and finding.code == 'osv.vulnerability'
        assert '1.2.3' in finding.message and 'OSV-' in finding.message
        assert len(finding.message) < 1024
    evidence = outcome(root).details
    text = json.dumps(evidence)
    assert 'advisory prose' not in text and 'secret' not in text and str(root) not in text
    assert 'stdout_sha256' not in evidence and 'stderr_sha256' not in evidence


def test_semantic_result_and_rule_evidence_ignore_order_prose_and_timestamps(root, monkeypatch):
    write(root)
    write(root, 'web/package-lock.json')
    first = output(packages=[package(ids=('OSV-2', 'OSV-1')), package('other')])
    first['results'].append(output('web/package-lock.json', [package('js', ecosystem='npm')])['results'][0])
    calls, _ = fake_process(monkeypatch, root, first, exit_code=1)
    a = evaluate(root)
    a_outcome = outcome(root)
    second = copy.deepcopy(first)
    second['results'].reverse()
    for item in second['results']:
        item['source']['path'] = str(root / item['source']['path'])
        item['packages'].reverse()
        for pkg in item['packages']:
            for vuln in pkg.get('vulnerabilities', []):
                vuln.update(summary='changed', details='different prose', modified='tomorrow', references=[])
            pkg.get('vulnerabilities', []).reverse()
            for group in pkg.get('groups', []):
                group['ids'].reverse()
                group['aliases'].reverse()
                group['max_severity'] = 'changed'
    # Reverse object key order too; stderr is unrelated execution provenance.
    text = json.dumps(second, sort_keys=True, indent=2)
    fake_process(monkeypatch, root, text, exit_code=1, stderr='volatile output')
    b = evaluate(root)
    b_outcome = outcome(root)
    assert a.stdout_sha256 != b.stdout_sha256 and a.stderr_sha256 != b.stderr_sha256
    assert a.semantic_sha256 == b.semantic_sha256 and a.result_sha256 == b.result_sha256
    assert a_outcome == b_outcome
    assert len(calls) == 4


@pytest.mark.parametrize('mutation', ['add', 'remove', 'version', 'vulnerability', 'source', 'multiplicity'])
def test_semantic_hash_binds_meaningful_inventory(root, monkeypatch, mutation):
    write(root)
    first = output(packages=[package(), package('second')])
    fake_process(monkeypatch, root, first)
    baseline = evaluate(root)
    changed = copy.deepcopy(first)
    packages = changed['results'][0]['packages']
    exit_code = 0
    if mutation == 'add':
        packages.append(package('third'))
    elif mutation == 'remove':
        packages.pop()
    elif mutation == 'version':
        packages[0]['package']['version'] = '2.0.0'
    elif mutation == 'vulnerability':
        packages[0] = package(ids=('OSV-1',))
        exit_code = 1
    elif mutation == 'multiplicity':
        packages.append(copy.deepcopy(packages[0]))
    else:
        write(root, 'nested/pubspec.lock')
        changed['results'].append(output('nested/pubspec.lock')['results'][0])
    fake_process(monkeypatch, root, changed, exit_code=exit_code)
    actual = evaluate(root)
    assert baseline.semantic_sha256 != actual.semantic_sha256
    assert baseline.result_sha256 != actual.result_sha256


@pytest.mark.parametrize('case', [
    'json', 'duplicate_key', 'nan', 'source_type', 'source_mapping', 'outside', 'traversal',
    'unknown_source', 'missing_source', 'empty_packages', 'missing_name', 'missing_version',
    'bad_ecosystem', 'bad_id', 'group_mapping', 'empty_group', 'group_unknown_id',
    'missing_groups', 'group_without_vuln', 'overlap_groups', 'unknown_envelope',
    'generic_findings', 'license_violation', 'commit_package', 'source_duplicate',
])
def test_untrusted_output_fails_as_error_not_waivable_fail(root, monkeypatch, case):
    write(root)
    payload = output(packages=[package(ids=('OSV-1',))])
    pkg = payload['results'][0]['packages'][0]
    source = payload['results'][0]['source']
    if case == 'json':
        payload = 'secret malformed JSON'
    elif case == 'duplicate_key':
        payload = '{"results":[],"results":[]}'
    elif case == 'nan':
        payload = '{"results":[],"metadata":NaN}'
    elif case == 'source_type':
        source['type'] = 'sbom'
    elif case == 'source_mapping':
        payload['results'][0]['source'] = []
    elif case == 'outside':
        source['path'] = str(root.parent / 'outside.lock')
    elif case == 'traversal':
        source['path'] = 'nested/../pubspec.lock'
    elif case == 'unknown_source':
        write(root, 'not-passed.txt')
        source['path'] = 'not-passed.txt'
    elif case == 'missing_source':
        write(root, 'nested/uv.lock')
    elif case == 'empty_packages':
        payload['results'][0]['packages'] = []
    elif case in {'missing_name', 'missing_version'}:
        pkg['package'].pop(case.removeprefix('missing_'))
    elif case == 'bad_ecosystem':
        pkg['package']['ecosystem'] = []
    elif case == 'bad_id':
        pkg['vulnerabilities'][0]['id'] = 'OSV-1\nsecret'
    elif case == 'group_mapping':
        pkg['groups'] = [None]
    elif case == 'empty_group':
        pkg['groups'][0]['ids'] = []
    elif case == 'group_unknown_id':
        pkg['groups'][0]['ids'] = ['OSV-unknown']
    elif case == 'missing_groups':
        pkg.pop('groups')
    elif case == 'group_without_vuln':
        pkg.pop('vulnerabilities')
    elif case == 'overlap_groups':
        pkg['groups'].append({'ids': ['OSV-1'], 'aliases': ['OSV-another']})
    elif case == 'unknown_envelope':
        payload['unexpected'] = {'partial': True}
    elif case == 'generic_findings':
        payload['experimental_generic_findings'] = [{}]
    elif case == 'license_violation':
        pkg['license_violations'] = ['MIT']
    elif case == 'commit_package':
        pkg['package']['commit'] = 'abc'
    elif case == 'source_duplicate':
        payload['results'].append(copy.deepcopy(payload['results'][0]))
    fake_process(monkeypatch, root, payload, exit_code=1, stderr='secret /unsafe/absolute/path')
    with pytest.raises(adapters.VerificationAdapterError) as caught:
        evaluate(root)
    assert 'secret' not in str(caught.value) and str(root) not in str(caught.value)
    actual = outcome(root)
    assert actual.raw_status == 'ERROR'
    assert 'secret' not in actual.failure_reason


@pytest.mark.parametrize('exit_code, vulnerable', [(0, True), (1, False), (2, True), (127, False),
                                                   (128, False), (129, False), (130, False), (-1, False)])
def test_scanner_exit_consistency_is_fail_closed(root, monkeypatch, exit_code, vulnerable):
    write(root)
    fake_process(monkeypatch, root, output(packages=[package(ids=('OSV-1',) if vulnerable else ())]),
                 exit_code=exit_code)
    assert outcome(root).raw_status == 'ERROR'


@pytest.mark.parametrize('version', [
    'osv-scanner version: 1.9.2', 'osv-scanner version: 3.0.0',
    'osv-scanner version: 2.bad.0', 'osv-scanner version: 2.3.3-dev',
    'osv-scanner version: 2.3.3\nosv-scanner version: 2.3.3', '',
])
def test_only_strict_supported_osv_version_can_scan(root, monkeypatch, version):
    write(root)
    calls, _ = fake_process(monkeypatch, root, output(), version=version)
    assert outcome(root).raw_status == 'ERROR'
    assert len(calls) == 1


def test_version_probe_error_is_not_a_scan_result(root, monkeypatch):
    write(root)
    calls, _ = fake_process(monkeypatch, root, output(), version_exit=127)
    assert outcome(root).raw_status == 'ERROR'
    assert len(calls) == 1


@pytest.mark.parametrize('version', [
    '2.0.0', '2.2.4', '2.3.0', '2.3.1', '2.3.2', '2.3.3-dev', '3.0.0',
])
def test_version_floor_rejects_unsupported_before_scan(root, monkeypatch, version):
    write(root)
    calls, _ = fake_process(monkeypatch, root, output(),
                            version='osv-scanner version: ' + version)
    with pytest.raises(adapters.VerificationAdapterError):
        evaluate(root)
    assert len(calls) == 1
    assert outcome(root).raw_status == 'ERROR'
    assert len(calls) == 2  # Both evaluations stop at the version probe.


@pytest.mark.parametrize('version', ['2.3.3', '2.3.8', '2.4.0', '2.5.0', '2.99.123'])
def test_version_floor_accepts_newer_stable_v2(root, monkeypatch, version):
    write(root)
    calls, _ = fake_process(monkeypatch, root, output(),
                            version='osv-scanner version: ' + version)
    result = evaluate(root)
    assert result.verification_status == 'PASS' and result.tool_version == version
    assert len(calls) == 2


def alias_package():
    value = package(ids=('GHSA-example', 'CVE-2026-1234'))
    value['vulnerabilities'][0]['aliases'] = ['CVE-2026-1234', 'OSV-alias']
    value['vulnerabilities'][1]['aliases'] = ['GHSA-example', 'OSV-alias']
    value['groups'] = [{'ids': ['GHSA-example', 'CVE-2026-1234'],
                       'aliases': ['OSV-alias', 'GHSA-example', 'CVE-2026-1234']}]
    return value


@pytest.mark.parametrize('case', [
    'missing_advisory_alias', 'missing_own_id', 'unknown_extra_alias', 'shared_alias',
    'conflicting_duplicate', 'missing_aliases',
])
def test_alias_group_integrity_errors_are_not_vulnerability_fail(root, monkeypatch, case):
    write(root)
    pkg = package(ids=('OSV-1',))
    pkg['vulnerabilities'][0]['aliases'] = ['CVE-2026-1234']
    pkg['groups'][0]['aliases'] = ['OSV-1', 'CVE-2026-1234']
    if case == 'missing_advisory_alias':
        pkg['groups'][0]['aliases'] = ['OSV-1']
    elif case == 'missing_own_id':
        pkg['groups'][0]['aliases'] = ['CVE-2026-1234']
    elif case == 'unknown_extra_alias':
        pkg['groups'][0]['aliases'].append('CVE-unknown')
    elif case == 'shared_alias':
        pkg['vulnerabilities'].append({'id': 'OSV-2', 'aliases': ['CVE-2026-1234']})
        pkg['groups'].append({'ids': ['OSV-2'], 'aliases': ['OSV-2', 'CVE-2026-1234']})
    elif case == 'conflicting_duplicate':
        pkg['vulnerabilities'].append({'id': 'OSV-1', 'aliases': ['CVE-different']})
    else:
        pkg['groups'][0].pop('aliases')
    fake_process(monkeypatch, root, output(packages=[pkg]), exit_code=1)
    with pytest.raises(adapters.VerificationAdapterError):
        evaluate(root)
    assert outcome(root).raw_status == 'ERROR'


def test_alias_group_valid_upstream_shape_is_a_vulnerability_fail(root, monkeypatch):
    write(root)
    pkg = alias_package()
    pkg['package'].update(commit='', os_package_name='', deprecated=False,
                          image_origin_details=None)
    pkg['groups'][0]['max_severity'] = '9.8'
    payload = output(packages=[pkg])
    payload['experimental_config'] = {'licenses': {'summary': False, 'allowlist': None}}
    payload['results'][0]['experimental_pes'] = []
    fake_process(monkeypatch, root, payload, exit_code=1)
    result = evaluate(root)
    assert result.verification_status == 'FAIL' and len(result.findings) == 2
    assert outcome(root).raw_status == 'FAIL'


def test_alias_group_permutations_and_equivalent_duplicates_have_same_hash(root, monkeypatch):
    write(root)
    pkg = alias_package()
    fake_process(monkeypatch, root, output(packages=[pkg]), exit_code=1)
    first = evaluate(root)
    first_evidence = outcome(root)
    permuted = copy.deepcopy(pkg)
    permuted['vulnerabilities'].reverse()
    for advisory in permuted['vulnerabilities']:
        advisory['aliases'].reverse()
        advisory['aliases'].append(advisory['id'])  # Same semantic identity.
    permuted['vulnerabilities'].append(copy.deepcopy(pkg['vulnerabilities'][0]))
    permuted['groups'][0]['ids'].reverse()
    permuted['groups'][0]['aliases'].reverse()
    permuted['groups'][0]['aliases'].append('OSV-alias')
    permuted['groups'].append(copy.deepcopy(permuted['groups'][0]))
    fake_process(monkeypatch, root, output(packages=[permuted]), exit_code=1)
    second = evaluate(root)
    assert first.verification_status == second.verification_status == 'FAIL'
    assert first.semantic_sha256 == second.semantic_sha256
    assert first.result_sha256 == second.result_sha256
    assert first_evidence == outcome(root)


def test_alias_group_semantic_hash_binds_changed_alias_identity(root, monkeypatch):
    write(root)
    pkg = alias_package()
    fake_process(monkeypatch, root, output(packages=[pkg]), exit_code=1)
    first = evaluate(root)
    pkg['vulnerabilities'][0]['aliases'].append('OSV-new-alias')
    pkg['groups'][0]['aliases'].append('OSV-new-alias')
    fake_process(monkeypatch, root, output(packages=[pkg]), exit_code=1)
    second = evaluate(root)
    assert first.semantic_sha256 != second.semantic_sha256
    assert first.result_sha256 != second.result_sha256


@pytest.mark.parametrize('value', [[], [{}]])
def test_upstream_schema_rejects_bogus_annotations_even_when_empty(root, monkeypatch, value):
    write(root)
    payload = output()
    payload['results'][0]['experimental_annotations'] = value
    fake_process(monkeypatch, root, payload)
    with pytest.raises(adapters.VerificationAdapterError):
        evaluate(root)
    assert outcome(root).raw_status == 'ERROR'


@pytest.mark.parametrize('field,value', [
    ('image_origin', ''), ('image_origin', {'layer': 'untrusted'}),
    ('image_origin_details', {'layer': 'untrusted'}), ('os_package_name', 'libssl'),
    ('deprecated', True), ('deprecated', 'false'),
])
def test_upstream_schema_rejects_invented_or_unsupported_package_fields(root, monkeypatch, field, value):
    write(root)
    payload = output()
    payload['results'][0]['packages'][0]['package'][field] = value
    fake_process(monkeypatch, root, payload)
    with pytest.raises(adapters.VerificationAdapterError):
        evaluate(root)
    assert outcome(root).raw_status == 'ERROR'


def test_upstream_schema_nonempty_pes_is_unsupported_capability(root, monkeypatch):
    write(root)
    payload = output()
    payload['results'][0]['experimental_pes'] = [{'justification': 'not requested'}]
    fake_process(monkeypatch, root, payload)
    with pytest.raises(adapters.VerificationAdapterError):
        evaluate(root)
    assert outcome(root).raw_status == 'ERROR'


def test_actual_v2_envelope_with_disabled_license_config_is_supported(root, monkeypatch):
    write(root)
    payload = output()
    payload['experimental_config'] = {'licenses': {'summary': False, 'allowlist': None}}
    fake_process(monkeypatch, root, payload, version='osv-scanner version: 2.5.0\nosv-scalibr version: v0.4.0\n')
    assert evaluate(root).verification_status == 'PASS'


def test_discovered_inputs_cannot_change_during_scan(root, monkeypatch):
    lock = write(root)
    fake_process(monkeypatch, root, output())
    adapter = module()
    original = adapter.run_process

    def run(argv, **kwargs):
        result = original(argv, **kwargs)
        if argv[1:] != ['--version']:
            lock.write_text('changed lockfile content and size\n', encoding='utf-8')
        return result

    monkeypatch.setattr(adapter, 'run_process', run)
    assert outcome(root).raw_status == 'ERROR'


def test_new_lockfile_during_scan_cannot_silently_be_omitted(root, monkeypatch):
    write(root)
    fake_process(monkeypatch, root, output())
    adapter = module()
    original = adapter.run_process

    def run(argv, **kwargs):
        result = original(argv, **kwargs)
        if argv[1:] != ['--version']:
            write(root, 'new/uv.lock')
        return result

    monkeypatch.setattr(adapter, 'run_process', run)
    assert outcome(root).raw_status == 'ERROR'


def test_project_controlled_temp_location_does_not_create_project_artifacts(root, monkeypatch):
    write(root)
    fake_process(monkeypatch, root, output())
    monkeypatch.setattr(module().tempfile, 'gettempdir', lambda: str(root))
    before = sorted(path.relative_to(root) for path in root.rglob('*'))
    assert outcome(root).raw_status == 'ERROR'
    assert sorted(path.relative_to(root) for path in root.rglob('*')) == before


@pytest.mark.parametrize('failure', [
    FileNotFoundError('secret path'), subprocess.TimeoutExpired(['secret'], 1, stderr='secret'),
    ConnectionError('secret connectivity'), RuntimeError('secret transport'),
])
def test_execution_failure_is_sanitized_and_temp_config_is_cleaned(root, monkeypatch, failure):
    write(root)
    calls, _ = fake_process(monkeypatch, root, output())
    adapter = module()
    original = adapter.run_process
    configs = []

    def run(argv, **kwargs):
        if argv[1:] == ['--version']:
            return original(argv, **kwargs)
        configs.append(Path(argv[argv.index('--config') + 1]))
        raise failure

    monkeypatch.setattr(adapter, 'run_process', run)
    actual = outcome(root)
    assert actual.raw_status == 'ERROR' and 'secret' not in actual.failure_reason
    assert len(calls) == 1 and len(configs) == 1
    assert not configs[0].exists() and not configs[0].parent.exists()


def test_missing_executable_fails_closed_before_scan(root, monkeypatch):
    write(root)
    monkeypatch.setattr(module().shutil, 'which', lambda *args: None)
    monkeypatch.setattr(module(), 'run_process', lambda *a, **k: pytest.fail('unexpected execution'))
    assert outcome(root).raw_status == 'ERROR'


def test_project_owned_executable_is_rejected(root, monkeypatch):
    tool = write(root, 'bin/osv-scanner.exe')
    write(root)
    monkeypatch.setattr(module().shutil, 'which', lambda *args: str(tool))
    monkeypatch.setattr(module(), 'run_process', lambda *a, **k: pytest.fail('unexpected execution'))
    assert outcome(root).raw_status == 'ERROR'


@pytest.mark.parametrize('mutation', [
    {'semantic_sha256': None}, {'semantic_sha256': []}, {'semantic_sha256': 'bad'},
    {'stdout_sha256': 'bad'}, {'stderr_sha256': 'bad'}, {'result_sha256': '0' * 64},
    {'adapter_id': 'dart.test'}, {'adapter_version': '2'},
])
def test_result_contract_cannot_be_tampered(root, monkeypatch, mutation):
    write(root)
    fake_process(monkeypatch, root, output())
    result = evaluate(root)
    with pytest.raises(adapters.VerificationAdapterError):
        adapters.validate_verification_adapter_result(replace(result, **mutation), expected_adapter_id='osv.scan')


def test_semantic_evidence_rejects_provenance_injection_and_digest_tampering(root, monkeypatch):
    from project_system.rule_checkers import checker_outcome_contract_messages

    write(root)
    fake_process(monkeypatch, root, output())
    details = outcome(root).details
    for changed in ({**details, 'stdout_sha256': 'b' * 64},
                    {**details, 'semantic_sha256': 'c' * 64},
                    {**details, 'uses_network': False}):
        assert checker_outcome_contract_messages('code.verification', 'PASS', changed, None, {'adapter': 'osv.scan'})


@pytest.mark.parametrize('adapter_id', ['osv.scan', 'dart.analyze', 'dart.test'])
def test_project_cannot_override_packaged_network_or_execution_policy(root, adapter_id):
    from project_system.rule_checkers import checker_contract_messages

    assert checker_contract_messages('code.verification', {'adapter': adapter_id, 'uses_network': False})
    assert checker_contract_messages('code.verification', {'adapter': adapter_id, 'flags': ['--config=local']})


def mutate_file(path, mutation):
    before = path.stat()
    if mutation == 'remove':
        path.unlink()
        return
    if mutation == 'replace':
        replacement = path.with_name(path.name + '.replacement')
        replacement.write_bytes(path.read_bytes())
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        replacement.replace(path)
        assert path.stat().st_ino != before.st_ino
        return
    changed = b'X' * before.st_size
    if mutation == 'different_size':
        changed += b'longer'
    path.write_bytes(changed)
    if mutation == 'restored_mtime':
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert path.stat().st_mtime_ns == before.st_mtime_ns
        assert path.stat().st_size == before.st_size


@pytest.mark.parametrize('checkpoint', ['version', 'scan'])
@pytest.mark.parametrize('mutation', ['same_size', 'restored_mtime', 'replace', 'remove', 'add'])
def test_9c2_lockfile_checkpoint_integrity(root, monkeypatch, checkpoint, mutation):
    lock = write(root)
    calls, _ = fake_process(monkeypatch, root, output())
    adapter = module()
    original = adapter.run_process
    mutations = []

    def run(argv, **kwargs):
        result = original(argv, **kwargs)
        if (argv[1:] == ['--version']) == (checkpoint == 'version'):
            if mutation == 'add':
                write(root, 'new/uv.lock')
            else:
                mutate_file(lock, mutation)
            mutations.append(mutation)
        return result

    monkeypatch.setattr(adapter, 'run_process', run)
    assert outcome(root).raw_status == 'ERROR'
    assert mutations == [mutation]
    assert len(calls) == (1 if checkpoint == 'version' else 2)
    assert not Path(calls[0][1]['cwd']).exists()


@pytest.mark.parametrize('checkpoint', ['version', 'scan'])
@pytest.mark.parametrize('mutation', ['different_size', 'same_size', 'restored_mtime', 'replace', 'remove'])
def test_9c2_executable_checkpoint_integrity(root, monkeypatch, checkpoint, mutation):
    write(root)
    calls, tool = fake_process(monkeypatch, root, output())
    adapter = module()
    original = adapter.run_process
    mutations = []

    def run(argv, **kwargs):
        result = original(argv, **kwargs)
        if (argv[1:] == ['--version']) == (checkpoint == 'version'):
            mutate_file(tool, mutation)
            mutations.append(mutation)
        return result

    monkeypatch.setattr(adapter, 'run_process', run)
    assert outcome(root).raw_status == 'ERROR'
    assert mutations == [mutation]
    assert len(calls) == (1 if checkpoint == 'version' else 2)
    assert not Path(calls[0][1]['cwd']).exists()


@pytest.mark.parametrize('checkpoint', ['version', 'scan'])
@pytest.mark.parametrize('mutation', ['content', 'replace', 'remove'])
def test_9c2_temp_config_checkpoint_integrity_and_cleanup(root, monkeypatch, checkpoint, mutation):
    write(root)
    calls, _ = fake_process(monkeypatch, root, output())
    adapter = module()
    original = adapter.run_process
    mutations = []

    def run(argv, **kwargs):
        # Do not let the test transport itself reject a changed config: the
        # production integrity boundary, not a fake-process assertion, must fail.
        if argv[1:] == ['--version']:
            result = original(argv, **kwargs)
        else:
            calls.append((argv, kwargs))
            result = subprocess.CompletedProcess(argv, 0, json.dumps(output()), '')
        if (argv[1:] == ['--version']) == (checkpoint == 'version'):
            config = Path(kwargs['cwd']) / 'osv-scanner.toml'
            if mutation == 'content':
                config.write_bytes(b'[[IgnoredVulns]]\nid="OSV-hidden"\n')
            else:
                mutate_file(config, mutation)
            mutations.append(mutation)
        return result

    monkeypatch.setattr(adapter, 'run_process', run)
    assert outcome(root).raw_status == 'ERROR'
    assert mutations == [mutation]
    assert len(calls) == (1 if checkpoint == 'version' else 2)
    directory = Path(calls[0][1]['cwd'])
    assert not directory.exists() and not (directory / 'osv-scanner.toml').exists()


def stat_with(info, **changes):
    values = {key: getattr(info, key) for key in (
        'st_mode', 'st_dev', 'st_ino', 'st_size', 'st_mtime_ns',
    )}
    values['st_file_attributes'] = getattr(info, 'st_file_attributes', 0)
    return SimpleNamespace(**{**values, **changes})


@pytest.mark.parametrize('target', ['lockfile', 'executable', 'config', 'directory'])
@pytest.mark.parametrize('checkpoint', ['version', 'scan'])
def test_9c2_checkpoint_reparse_points_fail_closed(root, monkeypatch, target, checkpoint):
    lock = write(root)
    calls, tool = fake_process(monkeypatch, root, output())
    adapter = module()
    original_run, original_lstat = adapter.run_process, adapter._lstat
    unsafe = None

    def run(argv, **kwargs):
        nonlocal unsafe
        result = original_run(argv, **kwargs)
        if (argv[1:] == ['--version']) == (checkpoint == 'version'):
            directory = Path(kwargs['cwd'])
            unsafe = {'lockfile': lock, 'executable': tool,
                      'config': directory / 'osv-scanner.toml', 'directory': directory}[target]
        return result

    def lstat(path):
        nonlocal unsafe
        if Path(path) == unsafe:
            unsafe = None  # Simulated checkpoint contradiction, not a real link.
            adapter._error('OSV integrity path is a Windows reparse point')
        return original_lstat(path)

    monkeypatch.setattr(adapter, 'run_process', run)
    # Model the established reparse-rejecting boundary, not global os.lstat:
    # cleanup must re-inspect the actual owned directory, not fictitious data.
    monkeypatch.setattr(adapter, '_lstat', lstat)
    assert outcome(root).raw_status == 'ERROR'
    assert len(calls) == (1 if checkpoint == 'version' else 2)
    assert not Path(calls[0][1]['cwd']).exists()


@pytest.mark.parametrize('target', ['lockfile', 'executable', 'config'])
def test_9c2_real_symlink_replacement_is_rejected(root, monkeypatch, target):
    lock = write(root)
    calls, tool = fake_process(monkeypatch, root, output())
    outside = write(root.parent, 'outside/target')
    probe = root.parent / 'symlink-probe'
    try:
        probe.symlink_to(outside)
    except OSError:
        pytest.skip('symlink creation unavailable')
    probe.unlink()
    adapter = module()
    original = adapter.run_process

    def run(argv, **kwargs):
        if argv[1:] == ['--version']:
            result = original(argv, **kwargs)
            path = {'lockfile': lock, 'executable': tool,
                    'config': Path(kwargs['cwd']) / 'osv-scanner.toml'}[target]
            path.unlink()
            path.symlink_to(outside)
            return result
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, json.dumps(output()), '')

    monkeypatch.setattr(adapter, 'run_process', run)
    assert outcome(root).raw_status == 'ERROR'
    assert len(calls) == 1
    assert not Path(calls[0][1]['cwd']).exists()
    assert outside.read_text(encoding='utf-8') == 'fixture lockfile\n'


def test_9c2_file_state_binds_content_even_with_restored_metadata(root):
    lock = write(root)
    first = module()._file_state(root, ('pubspec.lock',))
    original = lock.read_bytes()
    assert first[0][0] == 'pubspec.lock'
    assert hashlib.sha256(original).hexdigest() in first[0]
    mutate_file(lock, 'restored_mtime')
    second = module()._file_state(root, ('pubspec.lock',))
    assert first != second
    assert first[0][1:5] == second[0][1:5]


def test_9c2_hashing_is_streamed_in_bounded_chunks(root, monkeypatch):
    lock = write(root)
    content = b'x' * (3 * 1024 * 1024 + 17)
    lock.write_bytes(content)
    adapter = module()
    original_fdopen = adapter.os.fdopen
    reads, streams = [], []

    class Reader:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def fileno(self):
            return self.stream.fileno()
        def read(self, size):
            assert 0 < size <= 1024 * 1024
            reads.append(size)
            return self.stream.read(size)

    def fdopen(*args, **kwargs):
        stream = original_fdopen(*args, **kwargs)
        streams.append(stream)
        return Reader(stream)

    monkeypatch.setattr(adapter.os, 'fdopen', fdopen)
    monkeypatch.setattr(Path, 'read_bytes', lambda *a: pytest.fail('unbounded file read'))
    state = adapter._file_state(root, ('pubspec.lock',))
    assert hashlib.sha256(content).hexdigest() in state[0]
    assert len(reads) >= 5 and all(stream.closed for stream in streams)


@pytest.mark.parametrize('contradiction', ['opened_identity', 'opened_nonregular',
                                         'opened_reparse', 'after_read_metadata'])
def test_9c2_open_handle_stat_contradictions_fail_closed(root, monkeypatch, contradiction):
    write(root)
    adapter = module()
    original = adapter.os.fstat
    calls = []

    def fstat(fd):
        info = original(fd)
        calls.append(fd)
        if contradiction == 'opened_identity':
            return stat_with(info, st_ino=info.st_ino + 1)
        if contradiction == 'opened_nonregular':
            return stat_with(info, st_mode=stat.S_IFIFO)
        if contradiction == 'opened_reparse':
            return stat_with(info, st_file_attributes=0x400)
        if len(calls) == 2:
            return stat_with(info, st_mtime_ns=info.st_mtime_ns + 1)
        return info

    monkeypatch.setattr(adapter.os, 'fstat', fstat)
    with pytest.raises(adapters.VerificationAdapterError):
        adapter._file_state(root, ('pubspec.lock',))
    assert calls


@pytest.mark.parametrize('mutation', ['replace_before_fstat', 'replace_during_read',
                                     'size_during_read', 'read_error'])
def test_9c2_hashing_race_or_read_error_closes_handle(root, monkeypatch, mutation):
    lock = write(root)
    adapter = module()
    original_fdopen = adapter.os.fdopen
    streams = []
    mutations = []

    def mutate(selected):
        try:
            mutate_file(lock, selected)
        except PermissionError:
            if selected != 'replace':
                raise
            pytest.skip('filesystem forbids replacement while the read handle is open')
        mutations.append(mutation)

    class Reader:
        def __init__(self, stream):
            self.stream = stream
            self.changed = False
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def fileno(self):
            if mutation == 'replace_before_fstat' and not self.changed:
                self.changed = True
                mutate('replace')
            return self.stream.fileno()
        def read(self, size):
            if mutation == 'read_error':
                raise OSError('secret read failure')
            result = self.stream.read(size)
            if not self.changed:
                self.changed = True
                mutate('replace' if mutation == 'replace_during_read' else 'different_size')
            return result

    def fdopen(*args, **kwargs):
        stream = original_fdopen(*args, **kwargs)
        streams.append(stream)
        return Reader(stream)

    monkeypatch.setattr(adapter.os, 'fdopen', fdopen)
    with pytest.raises(adapters.VerificationAdapterError) as caught:
        adapter._file_state(root, ('pubspec.lock',))
    assert 'secret' not in str(caught.value)
    assert mutations == ([] if mutation == 'read_error' else [mutation])
    assert streams and all(stream.closed for stream in streams)


def component_package():
    pkg = package(ids=('OSV-A', 'OSV-B', 'OSV-C', 'OSV-D'))
    pkg['vulnerabilities'] = [
        {'id': 'OSV-A', 'aliases': ['CVE-AB']},
        {'id': 'OSV-B', 'aliases': ['CVE-AB', 'CVE-BC']},
        {'id': 'OSV-C', 'aliases': ['CVE-BC']},
        {'id': 'OSV-D'},
    ]
    pkg['groups'] = [
        {'ids': ['OSV-A', 'OSV-B', 'OSV-C'],
         'aliases': ['OSV-A', 'OSV-B', 'OSV-C', 'CVE-AB', 'CVE-BC']},
        {'ids': ['OSV-D'], 'aliases': ['OSV-D']},
    ]
    return pkg


@pytest.mark.parametrize('contradiction', ['merged_disconnected', 'split_connected'])
def test_9c2_grouping_requires_exact_connected_components(root, monkeypatch, contradiction):
    write(root)
    pkg = component_package()
    if contradiction == 'merged_disconnected':
        pkg['groups'] = [{'ids': ['OSV-A', 'OSV-B', 'OSV-C', 'OSV-D'],
                          'aliases': ['OSV-A', 'OSV-B', 'OSV-C', 'OSV-D', 'CVE-AB', 'CVE-BC']}]
    else:
        pkg['groups'] = [
            {'ids': ['OSV-A'], 'aliases': ['OSV-A', 'CVE-AB']},
            {'ids': ['OSV-B', 'OSV-C'], 'aliases': ['OSV-B', 'OSV-C', 'CVE-AB', 'CVE-BC']},
            {'ids': ['OSV-D'], 'aliases': ['OSV-D']},
        ]
    fake_process(monkeypatch, root, output(packages=[pkg]), exit_code=1)
    with pytest.raises(adapters.VerificationAdapterError):
        evaluate(root)
    assert outcome(root).raw_status == 'ERROR'  # Not an exception-eligible FAIL.


def test_9c2_transitive_components_duplicates_and_permutations_preserve_hash(root, monkeypatch):
    write(root)
    pkg = component_package()
    fake_process(monkeypatch, root, output(packages=[pkg]), exit_code=1)
    first = evaluate(root)
    first_outcome = outcome(root)
    reordered = copy.deepcopy(pkg)
    reordered['vulnerabilities'].reverse()
    for advisory in reordered['vulnerabilities']:
        advisory.setdefault('aliases', []).reverse()
        advisory.update(summary='changed prose', modified='changed timestamp')
    reordered['vulnerabilities'].append(copy.deepcopy(reordered['vulnerabilities'][0]))
    reordered['groups'].reverse()
    for group in reordered['groups']:
        group['ids'].reverse()
        group['aliases'].reverse()
    reordered['groups'].append(copy.deepcopy(reordered['groups'][0]))
    fake_process(monkeypatch, root, json.dumps(output(packages=[reordered]), indent=2),
                 exit_code=1, stderr='volatile raw stderr')
    second = evaluate(root)
    assert first.verification_status == second.verification_status == 'FAIL'
    assert first.semantic_sha256 == second.semantic_sha256
    assert first.result_sha256 == second.result_sha256
    assert first_outcome == outcome(root)


def test_9c2_runtime_integrity_state_is_not_semantic_evidence(root, monkeypatch):
    lock = write(root)
    _, tool = fake_process(monkeypatch, root, output())
    first, first_outcome = evaluate(root), outcome(root)
    lock.write_bytes(b'different dependency bytes between separate stable runs')
    tool.write_bytes(b'different binary bytes between separate stable runs')
    second = evaluate(root)
    assert first.verification_status == second.verification_status == 'PASS'
    assert first.semantic_sha256 == second.semantic_sha256
    assert first.result_sha256 == second.result_sha256
    assert first_outcome == outcome(root)
    assert hashlib.sha256(tool.read_bytes()).hexdigest() not in json.dumps(first_outcome.details)


def test_9c2_executable_e2_rechecks_after_input_hashing(root, monkeypatch):
    write(root)
    calls, tool = fake_process(monkeypatch, root, output())
    adapter = module()
    original = adapter._file_state
    checkpoints = []

    def file_state(*args):
        state = original(*args)
        checkpoints.append(state)
        if len(checkpoints) == 2:  # T1: E1 has passed; E2 must reject the drift.
            mutate_file(tool, 'restored_mtime')
        return state

    monkeypatch.setattr(adapter, '_file_state', file_state)
    assert outcome(root).raw_status == 'ERROR'
    assert len(calls) == 1 and len(checkpoints) == 2
    assert not Path(calls[0][1]['cwd']).exists()


def test_9c2_config_creation_is_exclusive_and_cleans_up(root, monkeypatch):
    write(root)
    calls, _ = fake_process(monkeypatch, root, output())
    adapter = module()
    original = adapter.tempfile.mkdtemp
    directories = []

    def directory(*args, **kwargs):
        name = original(*args, **kwargs)
        directories.append(Path(name))
        (Path(name) / 'osv-scanner.toml').write_bytes(b'pre-existing data')
        return name

    monkeypatch.setattr(adapter.tempfile, 'mkdtemp', directory)
    assert outcome(root).raw_status == 'ERROR'
    assert not calls and directories and all(not path.exists() for path in directories)


@pytest.mark.parametrize('failure', ['parser', 'version', 'timeout', 'process', 'integrity'])
def test_9c2_temp_cleanup_on_every_failure_boundary(root, monkeypatch, failure):
    lock = write(root)
    calls, _ = fake_process(monkeypatch, root, output())
    adapter = module()
    original = adapter.run_process

    def run(argv, **kwargs):
        result = original(argv, **kwargs)
        if argv[1:] == ['--version']:
            if failure == 'version':
                return subprocess.CompletedProcess(argv, 0, 'osv-scanner version: 2.3.2', '')
            return result
        if failure == 'parser':
            return subprocess.CompletedProcess(argv, 0, 'secret malformed JSON', '')
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(argv, 300)
        if failure == 'process':
            raise OSError('secret process failure')
        mutate_file(lock, 'restored_mtime')
        return result

    monkeypatch.setattr(adapter, 'run_process', run)
    actual = outcome(root)
    assert actual.raw_status == 'ERROR' and 'secret' not in actual.failure_reason
    assert calls and not Path(calls[0][1]['cwd']).exists()


def test_9c2_hash_open_is_noninheritable_readonly_and_no_follow(root, monkeypatch):
    lock = write(root)
    adapter = module()
    with adapter._open_binary(lock) as stream:
        assert stream.read() == lock.read_bytes()
        assert not os.get_inheritable(stream.fileno())
        with pytest.raises(OSError):
            os.write(stream.fileno(), b'forbidden')
    if os.name == 'nt':
        import ctypes
        import msvcrt

        calls = []
        class Function:
            def __call__(self, *args):
                calls.append(args)
                return 123
        kernel = SimpleNamespace(CreateFileW=Function(), CloseHandle=Function())
        monkeypatch.setattr(ctypes, 'WinDLL', lambda *a, **k: kernel)
        monkeypatch.setattr(msvcrt, 'open_osfhandle', lambda handle, flags: (handle, flags))
        handle, flags = adapter._windows_read_fd(lock)
        assert handle == 123 and flags & os.O_NOINHERIT and flags & os.O_BINARY
        assert calls[0][1:6] == (0x80000000, 7, None, 3, 0x00200000)
    else:
        original = adapter.os.open
        flags = []
        def open_file(path, selected):
            flags.append(selected)
            return original(path, selected)
        monkeypatch.setattr(adapter.os, 'open', open_file)
        with adapter._open_binary(lock):
            pass
        assert flags[0] & os.O_NOFOLLOW and flags[0] & os.O_NONBLOCK


def test_9c2_no_unsafe_fallback_without_no_follow_primitive(root, monkeypatch):
    lock = write(root)
    monkeypatch.setattr(module(), 'os', SimpleNamespace(name='posix'))
    with pytest.raises(adapters.VerificationAdapterError):
        module()._open_binary(lock)


def test_9c2_observable_path_identity_change_during_hashing_is_error(root, monkeypatch):
    write(root)
    adapter = module()
    original = adapter._safe_file
    checks = []

    def safe_file(*args):
        path, info = original(*args)
        checks.append(path)
        if len(checks) == 2:
            info = stat_with(info, st_ino=info.st_ino + 1)
        return path, info

    monkeypatch.setattr(adapter, '_safe_file', safe_file)
    with pytest.raises(adapters.VerificationAdapterError):
        adapter._file_state(root, ('pubspec.lock',))
    assert len(checks) == 2


def test_9c2_directory_state_rechecked_after_config_hashing(root, monkeypatch):
    write(root)
    calls, _ = fake_process(monkeypatch, root, output())
    adapter = module()
    original = adapter._directory_state
    checks = []

    def directory_state(path):
        state = original(path)
        checks.append(state)
        return (*state[:2], state[2] + 1) if len(checks) == 3 else state

    monkeypatch.setattr(adapter, '_directory_state', directory_state)
    assert outcome(root).raw_status == 'ERROR'
    assert not calls and len(checks) == 4  # Creation, C0 before/after, safe cleanup.


@pytest.mark.parametrize('vulnerable', [False, True])
def test_9c2_preserves_exact_semantic_schema_v1(root, monkeypatch, vulnerable):
    write(root)
    pkg = component_package() if vulnerable else package()
    fake_process(monkeypatch, root, output(packages=[pkg]), exit_code=int(vulnerable))
    identity = {'source': 'pubspec.lock', **pkg['package']}
    expected = {
        'schema_version': 1,
        'lockfiles': ['pubspec.lock'],
        'packages': [identity],
        'vulnerabilities': [{
            **identity,
            'ids': sorted(advisory['id'] for advisory in pkg.get('vulnerabilities', [])),
            'groups': sorted([{'ids': sorted(group['ids']), 'aliases': sorted(group['aliases'])}
                              for group in pkg.get('groups', [])], key=lambda group: group['ids']),
        }],
    }
    digest = hashlib.sha256(json.dumps(expected, sort_keys=True, separators=(',', ':'),
                                      ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()
    result = evaluate(root)
    assert result.semantic_sha256 == digest
    assert result.verification_status == ('FAIL' if vulnerable else 'PASS')


def test_9c2_cleanup_does_not_delete_replaced_temp_directory(root, monkeypatch):
    write(root)
    calls, _ = fake_process(monkeypatch, root, output())
    adapter = module()
    # All directories in this scenario remain in the test-owned fixture tree.
    monkeypatch.setattr(adapter.tempfile, 'gettempdir', lambda: str(root.parent))
    original = adapter.run_process
    displaced, replacement = [], []

    def run(argv, **kwargs):
        result = original(argv, **kwargs)
        if argv[1:] == ['--version']:
            directory = Path(kwargs['cwd'])
            moved = directory.with_name(directory.name + '.displaced')
            directory.rename(moved)
            displaced.append(moved)
            directory.mkdir()
            sentinel = directory / 'not-owned-by-adapter.txt'
            sentinel.write_bytes(b'protected unrelated contents')
            replacement.append(sentinel)
        return result

    monkeypatch.setattr(adapter, 'run_process', run)
    assert outcome(root).raw_status == 'ERROR'
    assert len(calls) == 1 and displaced and replacement
    assert replacement[0].read_bytes() == b'protected unrelated contents'
    assert (displaced[0] / 'osv-scanner.toml').read_bytes() == b''
