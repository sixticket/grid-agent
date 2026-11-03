#!/usr/bin/env python3
"""Suspend an existing worker at the next saved tool/API result.

This external guard never changes experiment sources, terminates a worker,
or resumes any process. A stopped process retains its exact live state.
"""
import argparse
from contextlib import closing
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import time


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def save(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + f'.{os.getpid()}.tmp')
    with temporary.open('w') as file:
        json.dump(value, file, indent=2, ensure_ascii=False)
        file.write('\n')
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def process(pid):
    result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'pid=,ppid=,stat=,command='],
                            capture_output=True, text=True)
    if not result.stdout.strip():
        return None
    parts = result.stdout.strip().split(None, 3)
    started = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'lstart='],
                             capture_output=True, text=True).stdout.strip()
    return dict(pid=int(parts[0]), ppid=int(parts[1]), stat=parts[2],
                command=parts[3], started=started)


def require_identity(expected):
    actual = process(expected['pid'])
    if actual is None or any(actual[key] != expected[key] for key in ('pid', 'command', 'started')):
        raise RuntimeError('Process identity changed: ' + str(expected['pid']))
    return actual


def boundary(name, record):
    if name == 'api_responses.jsonl':
        response = record.get('response', {})
        if record.get('response_id') and response.get('id') == record['response_id']:
            return dict(kind='api_response', response_id=record['response_id'])
    elif name == 'log.jsonl':
        if record.get('event') == 'assistant':
            return dict(kind='assistant_response', response_id=record.get('payload', {}).get('id'))
        payload = record.get('payload', {})
        if record.get('event') == 'tool':
            return dict(kind='tool_result', tool=payload.get('name'), turn=payload.get('turn'))
    return None


def suspend(expected):
    require_identity(expected)
    os.kill(expected['pid'], signal.SIGSTOP)
    for _ in range(100):
        actual = require_identity(expected)
        if 'T' in actual['stat']:
            return actual
        time.sleep(0.01)
    raise RuntimeError('SIGSTOP did not produce a stopped process')


def seal(path):
    # Do not acquire the worker's journal locks: it may be stopped holding one.
    digest = hashlib.sha256()
    with path.open('rb') as file:
        os.fsync(file.fileno())
        for chunk in iter(lambda: file.read(1024 * 1024), b''):
            digest.update(chunk)
    return dict(bytes=path.stat().st_size, sha256=digest.hexdigest())


def finish(state_path, state, hit):
    worker = state['identities'][str(state['worker_pid'])]
    actual = suspend(worker)
    state.update(status='paused', paused_at=now(), boundary=hit,
                 worker_state=actual['stat'], user_resume_required=True)
    save(state_path, state)
    output = Path(state['output'])
    evidence = {}
    for name in ('log.jsonl', 'api_responses.jsonl', 'api_usage.jsonl',
                 'recovery_requests.jsonl', 'dispatch_checkpoint.nc',
                 'dispatch_checkpoint.json', 'best_accepted.json'):
        path = output / name
        if path.is_file():
            evidence[name] = seal(path)
    for key in ('controller_pid', 'queue_pid'):
        actual = require_identity(state['identities'][str(state[key])])
        if 'T' not in actual['stat']:
            suspend(state['identities'][str(state[key])])
    base = state_path.parent
    campaign_file = Path(state['campaign']) / 'campaign.json'
    campaign = json.loads(campaign_file.read_text())
    campaign.update(status='paused', pause_reason='user_requested_after_current_result',
                    pause_receipt=str(state_path), paused_at=state['paused_at'])
    save(campaign_file, campaign)
    queue = json.loads((base / 'sequence_state.json').read_text())
    queue.update(status='paused', pause_reason='user_requested_after_current_result',
                 pause_receipt=str(state_path), updated_at=now())
    save(base / 'sequence_state.json', queue)
    manifest = Path(state['records']) / 'saved_result_manifest.json'
    save(manifest, dict(output=str(output), boundary=hit, files=evidence,
                        live_process_required=True, created_at=now()))
    state.update(result_manifest=str(manifest), verified_at=now())
    save(state_path, state)
    save(base / 'pause_state.json', state)
    print(json.dumps(dict(status='paused', worker_pid=state['worker_pid'], boundary=hit)), flush=True)


def run(state_path):
    state_path = Path(state_path).resolve()
    state = json.loads(state_path.read_text())
    if state['status'] not in ('arming', 'waiting_for_result'):
        raise RuntimeError('Pause is not awaiting a result')
    with state_path.with_suffix('.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for key in ('queue_pid', 'controller_pid'):
            actual = require_identity(state['identities'][str(state[key])])
            if 'T' not in actual['stat']:
                raise RuntimeError('Controllers must already be suspended')
        require_identity(state['identities'][str(state['worker_pid'])])
        handles = {}
        try:
            with closing(select.kqueue()) as queue:
                registrations = []
                for name, offset in state['baseline'].items():
                    file = (Path(state['output']) / name).open('rb')
                    file.seek(offset['bytes'])
                    handles[name] = file
                    registrations.append(select.kevent(file.fileno(), filter=select.KQ_FILTER_VNODE,
                        flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                        fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME))
                queue.control(registrations, 0, 0)
                state.update(status='waiting_for_result', guard_pid=os.getpid(), armed_at=now())
                save(state_path, state)
                save(state_path.parent / 'pause_state.json', state)
                print(json.dumps(dict(status='armed', guard_pid=os.getpid())), flush=True)
                while True:
                    # Drain first, including results written while the guard was being armed.
                    for name, file in handles.items():
                        while True:
                            offset = file.tell()
                            line = file.readline()
                            if not line or not line.endswith(b'\n'):
                                file.seek(offset)
                                break
                            hit = boundary(name, json.loads(line))
                            if hit:
                                hit.update(file=name, byte_offset=offset, observed_at=now())
                                finish(state_path, state, hit)
                                return
                    events = queue.control(None, 8, 10)
                    if any(event.fflags & (select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME) for event in events):
                        raise RuntimeError('A watched result journal was replaced')
                    if not events:
                        require_identity(state['identities'][str(state['worker_pid'])])
        except BaseException as exc:
            # Preserve live state and prevent uncontrolled continuation if the guard fails.
            try:
                suspend(state['identities'][str(state['worker_pid'])])
                stopped = True
            except Exception:
                stopped = False
            state.update(status='blocked', error=f'{type(exc).__name__}: {exc}',
                         worker_suspended=stopped, updated_at=now(), user_resume_required=True)
            save(state_path, state)
            save(state_path.parent / 'pause_state.json', state)
            raise
        finally:
            for file in handles.values():
                file.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True)
    args = parser.parse_args()
    run(args.state)
