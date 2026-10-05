import copy
from dataclasses import replace
import importlib
import json
from pathlib import Path
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
        value['groups'] = [{'ids': list(ids), 'aliases': list(ids),
                            'max_severity': 'arbitrary presentation'}]
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
    value['groups'][0]['aliases'] = ['OSV-alias', 'GHSA-example', 'CVE-2026-1234']
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
