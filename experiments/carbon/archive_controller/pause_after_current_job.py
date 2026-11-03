#!/usr/bin/env python3
"""Finish a user-authorized pause after a separately watched worker exits.

This helper never signals the worker or changes pinned experiment sources. An
already-suspended controller is removed only after the worker's successful exit,
durable results, journals, and source/input identity have all been verified.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time

sys.dont_write_bytecode = True
BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import run_campaign as controller


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def read(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    with tmp.open('w') as f:
        json.dump(value, f, indent=2, ensure_ascii=False, allow_nan=False)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def process(pid):
    result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'pid=,ppid=,stat=,command='],
                            capture_output=True, text=True)
    if result.returncode or not result.stdout.strip():
        return None
    parts = result.stdout.strip().split(None, 3)
    require(len(parts) == 4, 'Cannot parse process identity')
    return dict(pid=int(parts[0]), ppid=int(parts[1]), stat=parts[2], command=parts[3])


def suspended_controller(pid, campaign, model, agent):
    proc = process(pid)
    require(proc is not None and 'T' in proc['stat'], 'Controller must remain suspended')
    args = shlex.split(proc['command'])
    require(str(BASE / 'run_campaign.py') in args and '--execute' in args,
            'Suspended PID is not the expected experiment controller')
    if '--resume' in args:
        require(args[args.index('--resume') + 1] == str(campaign), 'Controller campaign differs')
    else:
        require('--models' in args and args[args.index('--models') + 1] == model,
                'Controller model differs')
        require('--agents' in args and args[args.index('--agents') + 1] == agent,
                'Controller architecture differs')
    return proc


def read_lines(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def verify_results(job):
    out = Path(job['output'])
    final = read(out / 'final.json')
    require((out / 'report.md').is_file(), 'Final report is missing')
    require(final.get('experiment') == 'co2_constraint' and final.get('execution_mode') == 'agent',
            'Final file is not a completed CO2 agent result')
    require(final.get('region') == job['case']['region'] and final.get('year') == job['case']['year'],
            'Final region/year differs')
    require(final.get('co2_case') == job['case'], 'Final fixed CO2 case differs')
    require(isinstance(final.get('completed_at'), str), 'Worker completion marker is missing')
    dt.datetime.fromisoformat(final['completed_at'])
    require(final.get('hours') == job['settings']['hours'], 'Final simulation horizon differs')
    require(final.get('paper_run_settings') == dict(**job['settings'], **job['config']),
            'Final model or experiment settings differ')
    require(final.get('input_sha256') == job['input_sha256'] and
            final.get('source_sha256') == job['source_sha256'], 'Final source/input receipt differs')
    require(controller.input_hashes(Path(job['pack'])) == job['input_sha256'], 'Inputs changed')
    require(controller.source_hashes(job['model_key'], job['runtime']) == job['source_sha256'],
            'Pinned sources changed')
    parsed = {p.name: read_lines(p) for p in out.glob('*.jsonl')}
    events = parsed['log.jsonl']
    raw = parsed['api_responses.jsonl']
    usage = parsed['api_usage.jsonl']
    requests = parsed['recovery_requests.jsonl']
    assistant = [e['payload'] for e in events if e['event'] == 'assistant']
    require(job['config']['provider'] == 'anthropic' and job['agent'] == 'single',
            'This pause audit is scoped to the authorized active Claude single job')
    ids = [r['response_id'] for r in raw]
    require(ids and len(ids) == len(set(ids)), 'API response identities are absent or duplicated')
    require(ids == [r['response_id'] for r in usage] == [r['response_id'] for r in requests]
            == [r['id'] for r in assistant], 'API journals do not cover every assistant response exactly')
    require(assistant == [r['response'] for r in raw], 'Assistant log differs from raw response journal')
    require(all(r.get('provider') == 'anthropic' and r.get('requested_model') == job['config']['model']
                and r.get('response_id') == r.get('response', {}).get('id') for r in raw),
            'Raw response provider/model identity differs')
    accounting = final.get('api_usage_summary', {})
    require(accounting.get('api_calls') == len(usage), 'Final API accounting count differs')
    for name in ('input_tokens', 'output_tokens'):
        require(accounting.get(name) == sum(r.get(name, 0) for r in usage),
                f'Final API accounting differs: {name}')
    require(final.get('api_response_journal') == dict(recorded_responses=len(raw),
                accounted_responses=len(usage), ledger_coverage_complete=True),
            'Final API coverage receipt differs')
    require(type(final.get('acceptance', {}).get('passes')) is bool, 'Acceptance result is missing')
    require(isinstance(final['acceptance'].get('co2_budget'), dict), 'CO2 verification result is missing')
    files = {}
    for path in sorted(out.rglob('*')):
        require(not path.is_symlink(), 'Result directory contains a symbolic link')
        if path.is_file():
            files[str(path.relative_to(out))] = dict(sha256=sha(path), bytes=path.stat().st_size)
    summary = dict(accepted=final['acceptance']['passes'],
                   solver_status=(final.get('final_results') or {}).get('status'),
                   co2_budget=final['acceptance']['co2_budget'], api_usage_summary=accounting)
    return final, summary, files


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--campaign', type=Path)
    ap.add_argument('--controller-pid', type=int)
    ap.add_argument('--worker-pid', type=int)
    ap.add_argument('--exit-receipt', type=Path, default=BASE / 'pause_records/current_worker_exit.json')
    ap.add_argument('--state', type=Path, default=BASE / 'pause_state.json')
    ap.add_argument('--status', action='store_true')
    args = ap.parse_args()
    if args.status:
        print(json.dumps(read(args.state), ensure_ascii=False, indent=2))
        return 0
    require(args.campaign and args.controller_pid and args.worker_pid, 'Campaign and process IDs are required')
    campaign = args.campaign.resolve()
    require(campaign.is_relative_to(BASE / 'runs'), 'Campaign must be an existing CO2 run')
    state = dict(schema_version=1, status='waiting_for_job', campaign=str(campaign),
                 job_id=None, controller_pid=args.controller_pid, worker_pid=args.worker_pid,
                 guard_pid=os.getpid(), requested_at=now(), completed_at=None,
                 result_manifest=None, worker_exit_code=None, phase='validating',
                 exit_receipt=str(args.exit_receipt.resolve()), pause_reason='user_requested')
    with (BASE / '.pause_after_current_job.lock').open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('A pause guard is already running.', flush=True)
            return 3
        try:
            before = read(campaign / 'campaign.json')
            require(before.get('status') == 'running' and before.get('pid') == args.controller_pid,
                    'Saved campaign controller identity differs')
            running = [j for j in before['jobs'] if j['status'] == 'running']
            require(len(running) == 1, 'Campaign must have exactly one running job')
            job = running[0]
            state['job_id'] = job['id']
            require(job['attempts'][-1].get('pid') == args.worker_pid, 'Saved worker identity differs')
            require(Path(job['output']).resolve().is_relative_to(campaign), 'Job output escapes campaign')
            suspended_controller(args.controller_pid, campaign, job['model_key'], job['agent'])
            sequence = read(BASE / 'sequence.json')
            require(str(campaign) in [s['campaign'] for s in sequence['stages']], 'Campaign is outside saved sequence')
            queue = read(BASE / 'sequence_state.json')
            require(queue.get('sequence_sha256') == sha(BASE / 'sequence.json'), 'Sequence identity changed')
            queue_proc = process(queue.get('pid', -1))
            require(not queue_proc or str(BASE / 'sequence_queue.py') not in queue_proc['command']
                    or 'Z' in queue_proc['stat'], 'Sequence queue must be stopped')
            registration = read(args.exit_receipt)
            require(registration.get('worker_pid') == args.worker_pid and
                    registration.get('controller_pid') == args.controller_pid and
                    registration.get('registered_at') and 'NOTE_EXITSTATUS' in registration.get('source', ''),
                    'Exit watcher registration differs')
            identity = registration.get('identity', '')
            require(str(BASE / 'worker.py') in identity and str(Path(job['output']) / 'job.json') in identity,
                    'Exit watcher did not register the expected worker')
            require(identity.split()[0] == str(args.controller_pid), 'Registered worker parent differs')
            controller.assert_unchanged(before)
            records = BASE / 'pause_records' / dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            records.mkdir(parents=True, exist_ok=False)
            save(records / 'campaign_before_pause.json', before)
            state.update(phase='waiting_for_worker_exit', records=str(records),
                         registered_at=registration['registered_at'])
            save(args.state, state)
            print(json.dumps(dict(status=state['status'], guard_pid=os.getpid(), job_id=job['id'])), flush=True)
            while True:
                receipt = read(args.exit_receipt)
                require(receipt.get('worker_pid') == args.worker_pid and
                        receipt.get('controller_pid') == args.controller_pid and
                        receipt.get('registered_at') == registration['registered_at'],
                        'Exit receipt identity changed')
                if receipt.get('status') == 'exited':
                    break
                require(receipt.get('status') == 'watching', 'Exit watcher failed or entered an unknown state')
                watcher = process(receipt.get('watcher_pid', -1))
                require(watcher and 'Z' not in watcher['stat'], 'Exit watcher stopped without an exit receipt')
                suspended_controller(args.controller_pid, campaign, job['model_key'], job['agent'])
                state['updated_at'] = now()
                save(args.state, state)
                time.sleep(5)
            raw_status = receipt.get('wait_status')
            code = receipt.get('exit_code')
            require(type(raw_status) is int and type(code) is int and
                    os.waitstatus_to_exitcode(raw_status) == code, 'Exit status evidence is invalid')
            state.update(worker_exit_code=code, worker_exit_observed_at=receipt.get('observed_at', now()),
                         phase='verifying_results')
            save(args.state, state)
            require(code == 0, f'Worker exited with code {code}; controller remains suspended')
            worker = process(args.worker_pid)
            require(worker is None or 'Z' in worker['stat'], 'Worker is still active')
            require(read(campaign / 'campaign.json') == before, 'Campaign changed during the pause request')
            final, summary, files = verify_results(job)
            manifest = records / 'result_manifest.json'
            save(manifest, dict(schema_version=1, created_at=now(), campaign=str(campaign), job_id=job['id'],
                                output=job['output'], worker_exit=receipt, files=files,
                                acceptance=final['acceptance'], api_response_count=final['api_usage_summary']['api_calls']))
            state.update(result_manifest=str(manifest), phase='results_verified')
            save(args.state, state)
            after = copy.deepcopy(before)
            target = next(j for j in after['jobs'] if j['id'] == job['id'])
            target['attempts'][-1].update(status='completed', completed_at=final['completed_at'],
                                         exit_code=code, summary=summary)
            target.update(status='completed', summary=summary)
            all_done = all(j['status'] == 'completed' for j in after['jobs'])
            after.update(status='completed' if all_done else 'paused', paused_at=now(),
                         pause_reason='user_requested', pause_receipt=str(args.state.resolve()))
            if all_done:
                after['completed_at'] = final['completed_at']
            save(records / 'campaign_reconciliation_pending.json', after)
            suspended_controller(args.controller_pid, campaign, job['model_key'], job['agent'])
            # The worker has exited successfully; only its suspended waiter is removed.
            os.kill(args.controller_pid, signal.SIGKILL)
            for _ in range(100):
                proc = process(args.controller_pid)
                if not proc or 'Z' in proc['stat']:
                    break
                time.sleep(0.05)
            else:
                raise RuntimeError('Suspended controller did not exit; campaign was not reconciled')
            state.update(phase='controller_stopped', controller_stopped_at=now())
            save(args.state, state)
            save(campaign / 'campaign.json', after)
            queue = read(BASE / 'sequence_state.json')
            queue.update(status='paused', pause_reason='user_requested', paused_at=now(),
                         updated_at=now(), pause_receipt=str(args.state.resolve()))
            for stage in queue.get('stages', []):
                if stage.get('campaign') == str(campaign):
                    stage.update(status='completed' if all_done else 'paused',
                                 jobs_completed=sum(j['status'] == 'completed' for j in after['jobs']))
            save(BASE / 'sequence_state.json', queue)
            state.update(status='paused', phase='complete', completed_at=now(),
                         campaign_status=after['status'], jobs_completed=sum(j['status'] == 'completed' for j in after['jobs']),
                         jobs_pending=sum(j['status'] == 'pending' for j in after['jobs']),
                         user_resume_required=True)
            save(args.state, state)
            print(json.dumps(dict(status='paused', job_id=job['id'], worker_exit_code=code,
                                  jobs_completed=state['jobs_completed'])), flush=True)
            return 0
        except BaseException as exc:
            state.update(status='blocked', phase_before_block=state.get('phase'), updated_at=now(),
                         error_type=type(exc).__name__, reason=str(exc), user_resume_required=True)
            save(args.state, state)
            print(f'Pause guard blocked: {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
            return 2


if __name__ == '__main__':
    raise SystemExit(main())
