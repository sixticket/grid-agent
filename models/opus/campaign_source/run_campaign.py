#!/usr/bin/env python3
"""Sequential, isolated paper campaigns. Default invocation only shows a plan."""

# Preserved campaign source reference.
if __name__ == '__main__':
    raise SystemExit('Reference only; use tools/run_campaign.py. Historical replay requires the original pinned archive.')

import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import uuid

BASE = Path(__file__).resolve().parent
PROJECT = BASE.parents[1]
CLIMATE = PROJECT / "nasa_climate"
CLIMATE_MODEL = "MPI-ESM1-2-HR"
STUDY = json.loads((BASE.parent / "study_config.json").read_text())
CAMPAIGN_YEARS = tuple(STUDY["campaigns"][BASE.name]["years"])
EXPECTED_CONFIG = STUDY["campaigns"][BASE.name]["model"]
REGIONS = ('miso', 'ercot', 'nyiso', 'australia', 'new_zealand', 'mexico', 'brazil', 'south_africa')


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def save(path, value):
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n')
    tmp.replace(path)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pack_path(region, year):
    if year == 2023:
        return PROJECT / 'experiment' / region
    return BASE.parent / 'inputs' / str(year) / region


def runtime_path(region, year):
    name = 'runtime_calendar' if (region, year) == ('south_africa', 2030) else 'runtime_original'
    return BASE / name / 'experiment'


def assert_plan_scope(state):
    expected = {f'{y}/{r}/{a}' for y in CAMPAIGN_YEARS for r in REGIONS for a in ('single', 'multi')}
    if len(state['jobs']) != len(CAMPAIGN_YEARS) * 16 or {j['id'] for j in state['jobs']} != expected:
        raise ValueError(f'This campaign requires exactly {len(CAMPAIGN_YEARS) * 16} configured experiments.')
    config = state['config']
    if config != json.loads((BASE / 'model.json').read_text()) or config != EXPECTED_CONFIG:
        raise ValueError('Model configuration differs from the frozen weather-prediction campaign.')
    expected_settings = STUDY['campaigns'][BASE.name]['settings']
    if state['settings'] != expected_settings or config.get('effort') != EXPECTED_CONFIG['effort'] or config.get('service_tier') != EXPECTED_CONFIG['service_tier']:
        raise ValueError('Frozen experiment settings changed.')
    for j in state['jobs']:
        if j['id'] != f"{j['year']}/{j['region']}/{j['agent']}" or j['agent'] not in ('single', 'multi'):
            raise ValueError('Job identity and fields do not match.')
        if j['climate_model'] != (CLIMATE_MODEL if j['year'] != 2023 else None):
            raise ValueError('Climate model changed.')
        expected_physics = 'corrected_sa2030' if (j['region'], j['year']) == ('south_africa', 2030) else 'original'
        expected_weather = 'NASA daily / ERA5 intraday synthetic' if j['year'] != 2023 else 'ERA5 2023'
        if j.get('physics_profile') != expected_physics or j.get('weather_source') != expected_weather:
            raise ValueError('Incorrect physics/weather provenance label.')
        if Path(j['pack']) != pack_path(j['region'], j['year']) or Path(j['runtime']) != runtime_path(j['region'], j['year']):
            raise ValueError('Unapproved input/runtime route.')


def assert_worker_authorized(record):
    if os.getppid() != record['controller_pid'] or os.environ.get('GRID_CAMPAIGN_WORKER_TOKEN') != record['controller_token']:
        raise ValueError('Worker must be started by its campaign controller.')
    state = json.loads(Path(record['controller_campaign']).read_text())
    assert_plan_scope(state)
    if any(record.get(k) != state.get(k) for k in ('config', 'settings', 'source_sha256')):
        raise ValueError('Worker configuration differs from its controller.')
    jobs = [j for j in state['jobs'] if j['id'] == record['job']['id']]
    if state['pid'] != os.getppid() or state['status'] != 'running' or len(jobs) != 1 or any(jobs[0].get(k) != record['job'].get(k) for k in ('id', 'pack', 'runtime', 'output', 'input_sha256')):
        raise ValueError('Worker ownership does not match running campaign.')


def input_hashes(pack):
    network = json.loads((pack / 'network.json').read_text())
    names = {'config.json', 'network.json', 'inventory.json', 'assumptions.json'}
    names.update(n['profiles_csv'] for n in network['nodes'])
    if (pack / 'prediction_manifest.json').exists():
        manifest = json.loads((pack / 'prediction_manifest.json').read_text())
        for name, digest in manifest['files'].items():
            if sha(pack / name) != digest:
                raise ValueError(f'Forecast input changed: {pack / name}')
        for name, digest in manifest['fixed_inputs'].items():
            if sha(PROJECT / 'experiment' / name) != digest:
                raise ValueError(f'Forecast reference changed: {name}')
        names.update(manifest['files'])
        names.add('prediction_manifest.json')
    return {name: sha(pack / name) for name in sorted(names)}


def source_hashes():
    files = [BASE / n for n in ('run_campaign.py', 'worker.py', 'model.json', 'climate_adapter.py', 'runtime_origin.json', 'check_api.py', 'preflight.py')]
    for name in ('runtime_original', 'runtime_calendar'):
        runtime = BASE / name / 'experiment'
        files += sorted(runtime.rglob('*.py'))
        files += sorted((runtime / 'shared').glob('*.json'))
        files += [runtime / 'requirements.lock.txt']
    result = {str(p.relative_to(BASE)): sha(p) for p in files}
    result['../../nasa_climate/scripts/build_packs.py'] = sha(CLIMATE / 'scripts/build_packs.py')
    result['../study_config.json'] = sha(BASE.parent / 'study_config.json')
    result['../credentials.py'] = sha(BASE.parent / 'credentials.py')
    result['../run_all.py'] = sha(BASE.parent / 'run_all.py')
    for extra in sorted((BASE / 'adapters').rglob('*.py')) if (BASE / 'adapters').exists() else []:
        result[str(extra.relative_to(BASE))] = sha(extra)
    for filename in ('model_serving_config.json', 'serve_qwen.py'):
        if (BASE / filename).exists():
            result[filename] = sha(BASE / filename)
    if (BASE / 'opus_protocol.py').exists():
        result['opus_protocol.py'] = sha(BASE / 'opus_protocol.py')
    return result


def make_plan(args):
    config = json.loads((BASE / 'model.json').read_text())
    regions = args.regions or list(REGIONS)
    if args.include_south_africa and 'south_africa' not in regions:
        regions.append('south_africa')
    # Expensive South Africa cases run only after every other selected case.
    jobs = []
    for region_group in ([r for r in regions if r != 'south_africa'], [r for r in regions if r == 'south_africa']):
        for year in args.years:
            for region in region_group:
                pack = pack_path(region, year)
                hashes = input_hashes(pack)
                for agent in args.agents:
                    jobs.append(dict(id=f'{year}/{region}/{agent}', year=year, region=region,
                                     agent=agent, pack=str(pack), input_sha256=hashes,
                                     runtime=str(runtime_path(region, year)),
                                     physics_profile='corrected_sa2030' if (region, year) == ('south_africa', 2030) else 'original',
                                     climate_model=CLIMATE_MODEL if year != 2023 else None,
                                     weather_source='NASA daily / ERA5 intraday synthetic' if year != 2023 else 'ERA5 2023',
                                     status='pending', attempts=[]))
    return dict(schema_version=1, created_at=now(), status='planned', config=config,
                climate_model=CLIMATE_MODEL, emissions_scenario='ssp245', baseline_weather='ERA5 2023',
                interpretation='Supply-weather sensitivity with fixed demand; single GCM, not an ensemble or deterministic weather forecast.',
                settings=dict(STUDY['campaigns'][BASE.name]['settings']),
                source_sha256=source_hashes(), jobs=jobs)


def assert_unchanged(state):
    assert_plan_scope(state)
    if source_hashes() != state['source_sha256']:
        raise ValueError('Code/config changed since campaign creation; start a new campaign.')
    for job in state['jobs']:
        if input_hashes(Path(job['pack'])) != job['input_sha256']:
            raise ValueError(f"Inputs changed for {job['id']}; start a new campaign.")


def terminate_group(child):
    if child is None:
        return
    try:
        os.killpg(child.pid, signal.SIGTERM)
        child.wait(timeout=8)
    except subprocess.TimeoutExpired:
        pass
    except ProcessLookupError:
        pass
    # Parent exit does not imply that all descendants exited.
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    child.wait()


def execute(state, campaign, retry_failed=False):
    # Both model launchers share this lock: solver jobs never compete for 18 cores.
    lock_path = PROJECT / 'gpt_sol' / '.paper_campaign.lock'
    with lock_path.open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another Sol/Opus campaign is running; run models sequentially.')
        assert_unchanged(state)
        from preflight import verify_receipt
        verify_receipt(state)
        from check_api import check_access
        access = check_access()
        if access['status'] != 'passed':
            state.update(status='blocked_api_access', api_access_check=access)
            save(campaign / 'campaign.json', state)
            raise ValueError('API preflight failed: ' + access.get('error_code', access.get('error_type', 'unknown')))
        state['api_access_check'] = access
        config = state['config']
        if not os.environ.get(config['api_key_env']):
            raise ValueError(f"Set {config['api_key_env']} before --execute. Keys are never copied into results.")
        env = os.environ.copy()
        for key in ('GRID_RESUME', 'GRID_RECOVERY_AUDIT', 'GRID_RESUME_MAX_CYCLES', 'GRID_SOLVER_EXTRA'):
            env.pop(key, None)
        env.update(GRID_REASONING_EFFORT=config['effort'], GRID_ANTHROPIC_CACHE_TTL='5m',
                   PYTHONUNBUFFERED='1', PYTHONDONTWRITEBYTECODE='1')
        state.update(status='running', pid=os.getpid())
        save(campaign / 'campaign.json', state)
        child = None
        current = None

        def stop(signum, frame):
            raise KeyboardInterrupt

        old_handlers = {s: signal.signal(s, stop) for s in (signal.SIGINT, signal.SIGTERM)}
        try:
            for job in state['jobs']:
                if job['status'] == 'completed':
                    continue
                if job['status'] != 'pending' and not retry_failed:
                    raise ValueError(f"{job['id']} is {job['status']}. --retry-failed starts a fresh attempt and preserves old results.")
                current = job
                assert_unchanged(state)
                out = campaign / job['id'] / f"attempt_{len(job['attempts']) + 1:03d}"
                out.mkdir(parents=True, exist_ok=False)
                attempt = dict(output=str(out), started_at=now(), status='running')
                job['attempts'].append(attempt)
                job.update(status='running', output=str(out))
                token = uuid.uuid4().hex
                env['GRID_CAMPAIGN_WORKER_TOKEN'] = token
                save(out / 'job.json', dict(job=job, config=config, settings=state['settings'],
                                          source_sha256=state['source_sha256'], controller_pid=os.getpid(),
                                          controller_token=token, controller_campaign=str(campaign / 'campaign.json')))
                save(campaign / 'campaign.json', state)
                command = [sys.executable, str(BASE / 'worker.py'), '--job', str(out / 'job.json')]
                print(now(), job['id'], 'START', flush=True)
                with (out / 'execution.log').open('w') as log:
                    child = subprocess.Popen(command, cwd=out, env=env, stdout=log,
                                             stderr=subprocess.STDOUT, start_new_session=True)
                    attempt['pid'] = child.pid
                    save(campaign / 'campaign.json', state)
                    code = child.wait()
                child = None
                final_path = out / 'final.json'
                final = json.loads(final_path.read_text()) if final_path.exists() else None
                good = code == 0 and final is not None
                status = 'completed' if good else 'failed'
                attempt.update(status=status, exit_code=code, finished_at=now())
                job.update(status=status, accepted=(final or {}).get('acceptance', {}).get('passes'))
                if final:
                    attempt['metrics'] = final.get('metrics')
                    attempt['api_usage_summary'] = final.get('api_usage_summary')
                save(campaign / 'campaign.json', state)
                print(now(), job['id'], status, 'accepted=', job.get('accepted'), flush=True)
                if not good:
                    state.update(status='stopped_on_failure', finished_at=now())
                    save(campaign / 'campaign.json', state)
                    return 2
            state.update(status='completed', finished_at=now())
            save(campaign / 'campaign.json', state)
            return 0
        except KeyboardInterrupt:
            # Ignore repeat signals while terminating the entire worker process group.
            for s in old_handlers:
                signal.signal(s, signal.SIG_IGN)
            terminate_group(child)
            state.update(status='paused', paused_at=now())
            if current and current['status'] == 'running':
                current['status'] = 'interrupted'
                current['attempts'][-1].update(status='interrupted', finished_at=now())
            save(campaign / 'campaign.json', state)
            print('Stopped. Completed jobs and interrupted logs are preserved.', flush=True)
            return 130
        except Exception as exc:
            terminate_group(child)
            state.update(status='stopped_on_failure', finished_at=now(),
                         error=f'{type(exc).__name__}: {exc}')
            if current and current['status'] == 'running':
                current['status'] = 'failed'
                current['attempts'][-1].update(status='failed', finished_at=now(), error=state['error'])
            save(campaign / 'campaign.json', state)
            print(state['error'], file=sys.stderr, flush=True)
            return 2
        finally:
            terminate_group(child)
            for s, handler in old_handlers.items():
                signal.signal(s, handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--years', nargs='+', type=int, choices=[2023, 2030, 2035], default=list(CAMPAIGN_YEARS))
    parser.add_argument('--agents', nargs='+', choices=['single', 'multi'], default=['single', 'multi'])
    parser.add_argument('--regions', nargs='+', choices=REGIONS)
    parser.add_argument('--include-south-africa', action='store_true')
    parser.add_argument('--execute', action='store_true', help='Start paid API runs; without this flag only inspect the plan.')
    parser.add_argument('--resume', type=Path, help='Existing campaign directory; skip completed jobs.')
    parser.add_argument('--retry-failed', action='store_true', help='Retry interrupted/failed jobs as new attempts, not conversation replay.')
    args = parser.parse_args()
    for values in (args.years, args.agents, args.regions or []):
        if len(values) != len(set(values)):
            parser.error('Duplicate years/agents/regions are not allowed.')
    try:
        if args.resume:
            campaign = args.resume.resolve()
            if not campaign.is_relative_to(BASE / 'runs'):
                raise ValueError('--resume must point inside this model directory/runs.')
            state = json.loads((campaign / 'campaign.json').read_text())
            assert_unchanged(state)
        else:
            state = make_plan(args)
            campaign = BASE / 'runs' / datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
        print(json.dumps(dict(model=state['config']['model'], effort=state['config']['effort'],
                              settings=state['settings'], jobs=[j['id'] for j in state['jobs']],
                              output=str(campaign), paid_execution=args.execute), indent=2))
        if not args.execute:
            return 0
        sys.path.insert(0, str(BASE.parent))
        credential_receipt = {'source': 'environment', 'api_key_env': state['config']['api_key_env']}
        state['credential_source'] = credential_receipt
        if not os.environ.get(state['config']['api_key_env']):
            raise ValueError(f"Set {state['config']['api_key_env']} before --execute.")
        if not args.resume:
            campaign.mkdir(parents=True, exist_ok=False)
            save(campaign / 'campaign.json', state)
        return execute(state, campaign, args.retry_failed)
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(2, str(exc) + '\n')


if __name__ == '__main__':
    raise SystemExit(main())
