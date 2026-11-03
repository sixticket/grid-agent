"""Pinned, single-use recovery of South Africa 2023 multi after HTTP 520."""
import hashlib
import json
from pathlib import Path
import shutil

JOB_ID = '2023/south_africa/multi'
KIND = 'sol_openai_sa_history_resume'
EVENTS = 9
RESPONSES = 6
COPY_NAMES = {'log.jsonl', 'api_usage.jsonl', 'api_responses.jsonl',
              'recovery_requests.jsonl', 'dispatch_checkpoint.nc', 'dispatch_checkpoint.json'}
SOURCE_CHANGES = {'worker.py', 'adapters/history_resume.py',
                  'adapters/sa_history_resume.py', 'adapters/request_retry.py'}
JOB_FIELDS = ('id', 'year', 'region', 'agent', 'pack', 'runtime', 'input_sha256',
              'physics_profile', 'climate_model', 'weather_source')
ROLES = ('architect', 'renewable', 'storage', 'nuclear_firm', 'orchestrator', 'evaluator')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def rows(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def files(root):
    root = Path(root)
    require(root.is_dir() and not root.is_symlink(), 'History source directory missing or linked')
    require(not any(path.is_symlink() for path in root.rglob('*')), 'History files must not be symlinks')
    return {str(path.relative_to(root)): sha(path) for path in sorted(root.rglob('*')) if path.is_file()}


def replay_names(source):
    source = Path(source)
    require(all((source / name).is_file() for name in COPY_NAMES), 'SA history evidence is incomplete')
    require(not (source / 'best_accepted.json').exists()
            and not (source / 'accepted_dispatch').exists(), 'SA history unexpectedly has an accepted incumbent')
    return COPY_NAMES


def evidence(source):
    source = Path(source)
    events = rows(source / 'log.jsonl')
    require([item['event'] for item in events] ==
            ['meta', 'localization', 'llm', 'llm', 'llm', 'llm', 'llm', 'simulation', 'llm'],
            'SA historical event order differs')
    llms = [item['payload'] for item in events if item['event'] == 'llm']
    require(tuple(item['agent'] for item in llms) == ROLES, 'SA historical role order differs')
    responses = [item['response'] for item in llms]
    ids = [item['id'] for item in responses]
    require(len(ids) == RESPONSES and len(set(ids)) == RESPONSES, 'SA historical response count differs')
    require(all(item['model'] == 'gpt-6.1-sol' and item.get('previous_response_id') is None
                and item.get('status') == 'completed' and item.get('store') is True
                for item in responses), 'SA role response identity differs')
    for name in ('api_usage.jsonl', 'api_responses.jsonl', 'recovery_requests.jsonl'):
        require([item['response_id'] for item in rows(source / name)] == ids,
                'SA historical API evidence identity differs: ' + name)
    require([item['response'] for item in rows(source / 'api_responses.jsonl')] == responses,
            'SA journal response bodies differ from the replay log')
    require(all(len(item.get('request_hash', '')) == 64
                for item in rows(source / 'recovery_requests.jsonl')), 'SA recovery request coverage differs')
    require(events[0]['payload'].get('baseline') == 'C_multi_agent'
            and events[0]['payload'].get('max_cycles') == 120
            and events[1]['payload'].get('cycle') == 0
            and events[7]['payload'].get('cycle') == 0
            and events[7]['payload']['results'].get('status') == 'optimal'
            and events[7]['payload']['distribution_check'].get('passes') is False,
            'SA historical cycle, budget, or simulation differs')
    metadata = read(source / 'dispatch_checkpoint.json')
    require(metadata.get('sha256') == sha(source / 'dispatch_checkpoint.nc'),
            'SA rolling dispatch checksum differs')
    expected_key = json.dumps({key: value for key, value in events[7]['payload']['design'].items()
                              if not key.startswith('dist_')}, sort_keys=True)
    require(metadata.get('key') == expected_key
            and str(metadata.get('scenario_digest', '')).startswith(events[0]['payload']['scenario_hash']),
            'SA rolling dispatch identity differs')
    return ids


def load_manifest(reference, record=None):
    require(isinstance(reference, dict) and set(reference) == {'manifest_path', 'manifest_sha256'},
            'Missing pinned SA history manifest')
    path = Path(reference['manifest_path'])
    require(path.is_absolute() and not path.is_symlink() and sha(path) == reference['manifest_sha256'],
            'SA history manifest changed')
    manifest = read(path)
    require(manifest.get('schema_version') == 1 and manifest.get('kind') == KIND
            and manifest.get('job_id') == JOB_ID, 'Wrong SA history manifest scope')
    source = Path(manifest['source_attempt'])
    require(source.is_absolute() and source.name == 'attempt_001'
            and source.parent.parts[-3:] == ('2023', 'south_africa', 'multi'), 'Wrong SA source attempt')
    require(files(source) == manifest['source_files_sha256'], 'Original SA history files changed')
    execution = (source / 'execution.log').read_text()
    require(not (source / 'final.json').exists() and 'Error code: 520' in execution
            and 'unknown_origin_error' in execution, 'SA history must be the failed HTTP 520 attempt')
    names = replay_names(source)
    require(set(manifest['replay_files_sha256']) == names, 'SA history copy scope differs')
    require(all(manifest['source_files_sha256'].get(name) == digest
                for name, digest in manifest['replay_files_sha256'].items()), 'SA history copy pins differ')
    evidence(source)
    old = read(source / 'job.json')
    require(old['job']['id'] == JOB_ID and old['config']['provider'] == 'openai'
            and old['job']['physics_profile'] == 'original' and old['job']['weather_source'] == 'ERA5 2023'
            and old['job']['climate_model'] is None and old['settings']['max_cycles'] == 120,
            'Wrong SA job, provider, physics, or budget')
    before, after = manifest['source_sha256'], manifest['current_source_sha256']
    require(old['source_sha256'] == before, 'SA historical source provenance differs')
    changes = {name: {'before': before.get(name), 'after': after.get(name)}
               for name in set(before) | set(after) if before.get(name) != after.get(name)}
    require(changes == manifest['source_changes'] and set(changes) <= SOURCE_CHANGES,
            'SA history changes unapproved scientific code or source pins')
    continuation = manifest.get('expected_continuation_response')
    require(isinstance(continuation, dict) and set(continuation) ==
            {'instructions_sha256', 'max_output_tokens', 'previous_response_id', 'request_hash'}
            and continuation['max_output_tokens'] == 12000 and continuation['previous_response_id'] is None
            and all(isinstance(continuation[key], str) and len(continuation[key]) == 64
                    and all(character in '0123456789abcdef' for character in continuation[key])
                    for key in ('instructions_sha256', 'request_hash')),
            'SA critic continuation request pin is missing or differs')
    if record is not None:
        require(record['source_sha256'] == after, 'Current SA history source pins differ')
        require(record['config'] == old['config'] and record['settings'] == old['settings'],
                'SA history changes model or search settings')
        require(all(record['job'].get(field) == old['job'].get(field) for field in JOB_FIELDS),
                'SA history changes job inputs, physics, or identity')
    return manifest


def resume_target(state):
    require(state['status'] == 'stopped_on_failure' and state['config']['provider'] == 'openai',
            'Only the stopped Sol HTTP 520 campaign may replay SA history')
    targets = [job for job in state['jobs'] if job['id'] == JOB_ID]
    require(len(targets) == 1, 'SA history target is missing or duplicated')
    job = targets[0]
    require(job['status'] == 'failed' and len(job.get('attempts', [])) == 1
            and job['attempts'][0]['status'] == 'failed' and not job.get('history_resume')
            and not job['attempts'][0].get('history_resume'), 'SA history recovery is single-use for this target')
    require(all(item is job or item['status'] in ('completed', 'pending') for item in state['jobs']),
            'Another unfinished failure needs a separate recovery decision')
    manifest = load_manifest(state.get('history_resume'), dict(job=job, config=state['config'],
                             settings=state['settings'], source_sha256=state['source_sha256']))
    require(Path(job['attempts'][0]['output']).resolve() == Path(manifest['source_attempt']).resolve()
            and Path(job['output']).resolve() == Path(manifest['source_attempt']).resolve(),
            'SA source attempt does not belong to this campaign job')
    return job, manifest


def copy_history(manifest, out):
    out = Path(out)
    require(manifest.get('kind') == KIND and out.name == 'attempt_002'
            and out.parent.resolve() == Path(manifest['source_attempt']).parent.resolve()
            and out.is_dir() and not out.is_symlink() and not any(out.iterdir()),
            'SA history needs an empty second attempt')
    require(set(manifest['replay_files_sha256']) == COPY_NAMES, 'SA history copy scope differs')
    for name, digest in manifest['replay_files_sha256'].items():
        relative = Path(name)
        require(not relative.is_absolute() and '..' not in relative.parts, 'Unsafe SA history copy path')
        target = out / relative
        shutil.copyfile(Path(manifest['source_attempt']) / relative, target)
        require(sha(target) == digest, 'SA history copy changed: ' + name)


def verify_copy(manifest, out):
    out = Path(out)
    require(manifest.get('kind') == KIND and out.name == 'attempt_002'
            and out.parent.resolve() == Path(manifest['source_attempt']).parent.resolve()
            and out.is_dir() and not out.is_symlink(), 'Wrong SA history destination')
    require(not any(path.is_symlink() for path in out.rglob('*')), 'SA history destination contains linked files')
    require(not (out / 'best_accepted.json').exists() and not (out / 'accepted_dispatch').exists(),
            'SA replay copy unexpectedly has an accepted incumbent')
    require(set(manifest['replay_files_sha256']) == COPY_NAMES, 'SA history copy scope differs')
    for name, digest in manifest['replay_files_sha256'].items():
        path = out / name
        require(not path.is_symlink() and sha(path) == digest, 'Copied SA history changed before continuation: ' + name)


def verify_audit(record, out):
    manifest = load_manifest(record['history_resume'], record)
    audit = read(Path(out) / 'replay_audit.json')
    require(audit.get('status') == 'passed' and audit.get('next_operation') == 'api:openai'
            and audit.get('replayed_events') == audit.get('total_events') == EVENTS
            and audit.get('api_calls') == audit.get('optimization_calls') == 0,
            'SA offline replay did not reach the pinned critic API frontier')
    require(audit.get('history_resume') == record['history_resume']
            and audit.get('source_sha256') == record['source_sha256']
            and audit.get('job_sha256') == sha(Path(out) / 'job.json'), 'SA history replay audit identity differs')
    require(audit.get('next_request') == manifest['expected_continuation_response'],
            'SA replay reconstructed a different critic request')
    return audit


def accounting_split(manifest, out):
    inherited_ids = evidence(manifest['source_attempt'])
    ledger = rows(Path(out) / 'api_usage.jsonl')
    journal = rows(Path(out) / 'api_responses.jsonl')
    ids = [item['response_id'] for item in ledger]
    require(ids[:RESPONSES] == inherited_ids and len(ids) == len(set(ids))
            and [item['response_id'] for item in journal] == ids, 'SA history accounting identity differs')
    return dict(inherited_successful_api_calls=RESPONSES, new_successful_api_calls=len(ids) - RESPONSES,
                total_successful_api_calls=len(ids), replayed_events=EVENTS,
                inherited_simulation_runs=1, inherited_verification_sims=0, inherited_completed_cycles=0,
                replayed_responses_billed_again=False, new_provider_calls_during_replay=0,
                context_billing='New continuation requests retain normal provider context-token billing.',
                usage_scope='successful responses in selected trajectory; other attempts remain separately accounted')
