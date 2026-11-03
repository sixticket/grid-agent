"""Pinned, single-use replay of the interrupted Sol Mexico 2035 trajectory."""
import hashlib
import json
from pathlib import Path
import shutil

JOB_ID = '2035/mexico/single'
KIND = 'sol_openai_history_resume'
EVENTS = 17
RESPONSES = 8
COPY_NAMES = {'log.jsonl', 'api_usage.jsonl', 'api_responses.jsonl',
              'recovery_requests.jsonl', 'dispatch_checkpoint.nc',
              'dispatch_checkpoint.json', 'best_accepted.json'}
SOURCE_CHANGES = {'run_campaign.py', 'worker.py', 'check_api.py', 'adapters/history_resume.py',
                  'adapters/request_retry.py',
                  'runtime_original/experiment/engine/providers.py',
                  'runtime_calendar/experiment/engine/providers.py'}
JOB_FIELDS = ('id', 'year', 'region', 'agent', 'pack', 'runtime', 'input_sha256',
              'physics_profile', 'climate_model', 'weather_source')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def rows(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def _reference_kind(reference):
    require(isinstance(reference, dict) and set(reference) == {'manifest_path', 'manifest_sha256'},
            'Missing pinned history manifest')
    path = Path(reference['manifest_path'])
    require(path.is_absolute() and not path.is_symlink() and sha(path) == reference['manifest_sha256'],
            'History manifest changed')
    return read(path).get('kind')


def expected_events(record_or_reference):
    record = record_or_reference if 'history_resume' in record_or_reference else None
    reference = record['history_resume'] if record is not None else record_or_reference
    manifest = load_manifest(reference, record)
    if manifest.get('kind') == 'sol_openai_sa_history_resume':
        from sa_history_resume import EVENTS as sa_events
        return sa_events
    return EVENTS


def files(root):
    root = Path(root)
    require(root.is_dir() and not root.is_symlink(), 'History source directory missing or linked')
    require(not any(path.is_symlink() for path in root.rglob('*')), 'History files must not be symlinks')
    return {str(path.relative_to(root)): sha(path) for path in sorted(root.rglob('*')) if path.is_file()}


def replay_names(source):
    source = Path(source)
    require(all((source / name).is_file() for name in COPY_NAMES), 'History evidence is incomplete')
    accepted = {str(path.relative_to(source)) for path in (source / 'accepted_dispatch').glob('*') if path.is_file()}
    require(accepted and all(Path(name).suffix in ('.nc', '.json') for name in accepted),
            'Accepted dispatch evidence is incomplete')
    for name in accepted:
        partner = str(Path(name).with_suffix('.json' if name.endswith('.nc') else '.nc'))
        require(partner in accepted, 'Accepted dispatch pair is incomplete')
    return COPY_NAMES | accepted


def evidence(source):
    source = Path(source)
    events = rows(source / 'log.jsonl')
    assistants = [item['payload'] for item in events if item['event'] == 'assistant']
    ids = [item['id'] for item in assistants]
    require(len(events) == EVENTS and len(ids) == RESPONSES and len(set(ids)) == RESPONSES
            and events[0]['event'] == 'meta' and events[-1]['event'] == 'tool'
            and events[-1]['payload']['name'] == 'update_strategy', 'History frontier differs')
    require(all(item['model'] == 'gpt-6.1-sol' and item.get('previous_response_id') ==
                (assistants[index - 1]['id'] if index else None)
                for index, item in enumerate(assistants)), 'Historical response chain differs')
    for name in ('api_usage.jsonl', 'api_responses.jsonl', 'recovery_requests.jsonl'):
        require([item['response_id'] for item in rows(source / name)] == ids,
                'Historical API evidence identity differs: ' + name)
    require(all(item['response'].get('store') is True for item in rows(source / 'api_responses.jsonl')),
            'Historical response storage was disabled')
    return ids


def load_manifest(reference, record=None):
    require(isinstance(reference, dict) and set(reference) == {'manifest_path', 'manifest_sha256'},
            'Missing pinned history manifest')
    path = Path(reference['manifest_path'])
    require(path.is_absolute() and not path.is_symlink() and sha(path) == reference['manifest_sha256'],
            'History manifest changed')
    manifest = read(path)
    if manifest.get('kind') == 'sol_openai_sa_history_resume':
        from sa_history_resume import load_manifest as load_sa
        return load_sa(reference, record)
    require(manifest.get('schema_version') == 1 and manifest.get('kind') == KIND
            and manifest.get('job_id') == JOB_ID, 'Wrong history manifest scope')
    source = Path(manifest['source_attempt'])
    require(source.is_absolute() and source.name == 'attempt_002'
            and source.parent.parts[-3:] == ('2035', 'mexico', 'single'), 'Wrong source attempt')
    require(files(source) == manifest['source_files_sha256'], 'Original history files changed')
    require(not (source / 'final.json').exists() and 'server_is_overloaded' in
            (source / 'execution.log').read_text(), 'History must be the failed overload attempt')
    names = replay_names(source)
    require(set(manifest['replay_files_sha256']) == names, 'History copy scope differs')
    require(all(manifest['source_files_sha256'].get(name) == digest
                for name, digest in manifest['replay_files_sha256'].items()), 'History copy pins differ')
    evidence(source)
    old = read(source / 'job.json')
    require(old['job']['id'] == JOB_ID and old['config']['provider'] == 'openai', 'Wrong history job/provider')
    before, after = manifest['source_sha256'], manifest['current_source_sha256']
    require(old['source_sha256'] == before, 'Historical source provenance differs')
    changes = {name: {'before': before.get(name), 'after': after.get(name)}
               for name in set(before) | set(after) if before.get(name) != after.get(name)}
    require(changes == manifest['source_changes'] and set(changes) <= SOURCE_CHANGES,
            'History changes unapproved scientific code or source pins')
    if record is not None:
        require(record['source_sha256'] == after, 'Current history source pins differ')
        require(record['config'] == old['config'] and record['settings'] == old['settings'],
                'History changes model or search settings')
        require(all(record['job'].get(field) == old['job'].get(field) for field in JOB_FIELDS),
                'History changes job inputs, physics, or identity')
    return manifest


def resume_target(state):
    if _reference_kind(state.get('history_resume')) == 'sol_openai_sa_history_resume':
        from sa_history_resume import resume_target as resume_sa
        return resume_sa(state)
    require(state['status'] in ('paused', 'stopped_on_failure') and state['config']['provider'] == 'openai',
            'Only a stopped Sol campaign may replay history')
    targets = [job for job in state['jobs'] if job['id'] == JOB_ID]
    require(len(targets) == 1, 'History target is missing or duplicated')
    job = targets[0]
    require(job['status'] == 'interrupted' and len(job.get('attempts', [])) == 3
            and job['attempts'][1]['status'] == 'failed'
            and job['attempts'][2]['status'] == 'interrupted', 'History recovery requires the known three attempts')
    require(all(not item.get('history_resume') for item in state['jobs']), 'History recovery is single-use')
    manifest = load_manifest(state.get('history_resume'), dict(job=job, config=state['config'],
                             settings=state['settings'], source_sha256=state['source_sha256']))
    require(Path(job['attempts'][1]['output']).resolve() == Path(manifest['source_attempt']).resolve(),
            'Source attempt does not belong to this campaign job')
    require(Path(job['output']).resolve() == Path(job['attempts'][2]['output']).resolve()
            and Path(job['output']).name == 'attempt_003', 'Interrupted attempt route differs')
    return job, manifest


def copy_history(manifest, out):
    if manifest.get('kind') == 'sol_openai_sa_history_resume':
        from sa_history_resume import copy_history as copy_sa
        return copy_sa(manifest, out)
    out = Path(out)
    require(out.name == 'attempt_004' and out.parent.resolve() == Path(manifest['source_attempt']).parent.resolve()
            and out.is_dir() and not out.is_symlink() and not any(out.iterdir()), 'History needs an empty fourth attempt')
    for name, digest in manifest['replay_files_sha256'].items():
        relative = Path(name)
        require(not relative.is_absolute() and '..' not in relative.parts, 'Unsafe history copy path')
        target = out / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(manifest['source_attempt']) / relative, target)
        require(sha(target) == digest, 'History copy changed: ' + name)


def verify_copy(manifest, out):
    if manifest.get('kind') == 'sol_openai_sa_history_resume':
        from sa_history_resume import verify_copy as verify_sa
        return verify_sa(manifest, out)
    out = Path(out)
    require(out.name == 'attempt_004' and out.parent.resolve() == Path(manifest['source_attempt']).parent.resolve()
            and not out.is_symlink(), 'Wrong history destination')
    require(not any(path.is_symlink() for path in out.rglob('*')), 'History destination contains linked files')
    for name, digest in manifest['replay_files_sha256'].items():
        path = out / name
        require(not path.is_symlink() and sha(path) == digest, 'Copied history changed before continuation: ' + name)


def verify_audit(record, out):
    if _reference_kind(record.get('history_resume')) == 'sol_openai_sa_history_resume':
        from sa_history_resume import verify_audit as verify_sa
        return verify_sa(record, out)
    audit = read(Path(out) / 'replay_audit.json')
    require(audit.get('status') == 'passed' and audit.get('next_operation') == 'api:openai'
            and audit.get('replayed_events') == audit.get('total_events') == EVENTS
            and audit.get('api_calls') == audit.get('optimization_calls') == 0,
            'Offline replay did not reach the pinned API frontier')
    require(audit.get('history_resume') == record['history_resume']
            and audit.get('source_sha256') == record['source_sha256']
            and audit.get('job_sha256') == sha(Path(out) / 'job.json'), 'History replay audit identity differs')
    return audit


def accounting_split(manifest, out):
    if manifest.get('kind') == 'sol_openai_sa_history_resume':
        from sa_history_resume import accounting_split as split_sa
        return split_sa(manifest, out)
    inherited_ids = evidence(manifest['source_attempt'])
    ledger = rows(Path(out) / 'api_usage.jsonl')
    journal = rows(Path(out) / 'api_responses.jsonl')
    ids = [item['response_id'] for item in ledger]
    require(ids[:RESPONSES] == inherited_ids and len(ids) == len(set(ids))
            and [item['response_id'] for item in journal] == ids, 'History accounting identity differs')
    return dict(inherited_successful_api_calls=RESPONSES, new_successful_api_calls=len(ids) - RESPONSES,
                total_successful_api_calls=len(ids), replayed_events=EVENTS,
                replayed_responses_billed_again=False, new_provider_calls_during_replay=0,
                context_billing="New continuation requests retain normal provider context-token billing.",
                usage_scope='successful responses in selected trajectory; other attempts remain separately accounted')
