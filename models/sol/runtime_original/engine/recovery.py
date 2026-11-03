"""Replay flushed events to recover control state without repeating paid work.

Replay must agree with every historical event. Unknown/changed history fails
closed. Old runs lack dispatch snapshots: a new distribution-only variant then
requires explicit recovery rather than silently solving a different dispatch.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import time

class ReplayComplete(SystemExit):
    pass

def canonical(x):
    return json.dumps(x, sort_keys=True, separators=(',', ':'), ensure_ascii=False)

class Recovery:
    def __init__(self, out):
        self.out = Path(out)
        self.resume = os.environ.get('GRID_RESUME') == '1'
        self.audit = os.environ.get('GRID_RECOVERY_AUDIT') == '1'
        self.started = time.monotonic()
        self.index = 0
        self.limit_change = None
        self.events = []
        self.requests = {}
        self.requests_file = self.out / 'recovery_requests.jsonl'
        if self.requests_file.exists():
            for line in self.requests_file.read_text().splitlines():
                r = json.loads(line); self.requests[r['response_id']] = r['request_hash']
        self.last_design = None
        self.last_dist = None
        self.cache_ready = False
        self.snapshot = self.out / 'dispatch_checkpoint.nc'
        p = self.out / 'log.jsonl'
        if self.resume:
            if not p.exists(): raise RuntimeError('Resume requires log.jsonl')
            self.events = [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
            if not self.events: raise RuntimeError('Empty recovery log')
        elif p.exists() and p.stat().st_size:
            raise RuntimeError('Nonempty log: use GRID_RESUME=1; never append a new run to old history')

    def peek(self):
        return self.events[self.index] if self.index < len(self.events) else None

    def frontier(self, operation):
        if self.audit:
            print('RECOVERY_AUDIT_OK ' + canonical({'replayed_events': self.index,
                  'total_events': len(self.events), 'next_operation': operation}))
            raise ReplayComplete(0)

    def log(self, file, event, payload):
        record = {'event': event, 'payload': payload}
        old = self.peek()
        if old:
            if canonical(record) != canonical(old):
                normalized = copy.deepcopy(record)
                before = old.get('payload', {}).get('max_cycles')
                after = payload.get('max_cycles')
                allowed = (self.index == 0 and event == old.get('event') == 'meta'
                           and payload.get('baseline') == 'C_multi_agent'
                           and type(before) is int and type(after) is int and after > before
                           and os.environ.get('GRID_RESUME_MAX_CYCLES') == str(after))
                if allowed:
                    normalized['payload']['max_cycles'] = before
                    if canonical(normalized) == canonical(old):
                        self.limit_change = dict(parameter='max_cycles', before=before, after=after)
                        self.index += 1
                        return
                raise RuntimeError(f'Recovery mismatch at event {self.index}: {event}; no new work performed')
            self.index += 1
            return
        self.frontier('log:' + event)
        file.write(json.dumps(record) + '\n')
        file.flush()
        os.fsync(file.fileno())

    def install_providers(self, providers):
        original = getattr(providers.make_chat, "__dict__", {}).get("_recovery_original", providers.make_chat)
        recovery = self
        def make(provider, model=None):
            chat = original(provider, model)
            live_step = chat.step
            def step(system, tools=None, force_tool=None, max_tokens=None):
                request = dict(system=system, tools=tools, force_tool=force_tool,
                               max_tokens=max_tokens, model=getattr(chat,"model",model),
                               previous_response_id=getattr(chat,'prev_id',None),
                               pending=getattr(chat,'pending',None))
                request_hash = hashlib.sha256(canonical(request).encode()).hexdigest()
                old = recovery.peek()
                if not old:
                    recovery.frontier('api:' + provider)
                    result = live_step(system, tools, force_tool, max_tokens)
                    with recovery.requests_file.open('a') as f:
                        f.write(json.dumps({'response_id': result['raw'].get('id'), 'request_hash': request_hash})+'\n')
                        f.flush();os.fsync(f.fileno())
                    return result
                if old['event'] not in ('llm', 'assistant'):
                    raise RuntimeError(f'Expected API event at {recovery.index}, found {old["event"]}')
                raw = copy.deepcopy(old['payload']['response'] if old['event']=='llm' else old['payload'])
                if raw.get('id') in recovery.requests and recovery.requests[raw['id']] != request_hash:
                    raise RuntimeError('Recovery request changed: prompts, tools or conversation differ')
                if raw.get('model') != chat.model:
                    raise RuntimeError('Recovery model mismatch')
                if provider == 'openai':
                    expected_instructions = system
                    if force_tool and raw['id'] in recovery.requests:
                        # providers.OpenAIChat's exact documented 400/tool_choice fallback.
                        fallback = (system + '\n\n[Response format — mandatory] '
                                    f'Respond by calling the `{force_tool}` tool '
                                    'exactly once. Do not answer in plain text.')
                        if raw.get('instructions') == fallback:
                            expected_instructions = fallback
                    if raw.get('instructions') != expected_instructions:
                        raise RuntimeError('Recovery prompt mismatch; cannot silently change experiment')
                    # A stateful continuation must refer to the preceding recorded response.
                    if raw.get('previous_response_id') != chat.prev_id:
                        raise RuntimeError('Unlogged response/nudge or expired conversation history; exact replay unavailable')
                    calls=[dict(id=x['call_id'],name=x['name'],input=json.loads(x.get('arguments') or '{}'))
                           for x in raw['output'] if x['type']=='function_call']
                    text=''.join(c.get('text','') for x in raw['output'] if x['type']=='message'
                                 for c in x.get('content',[]) if c['type']=='output_text')
                    if hasattr(chat, 'restore_response'):
                        chat.restore_response(raw)
                    chat.prev_id=raw['id'];chat.pending=[]
                    return dict(text=text,tool_calls=calls,stop='tool_use' if calls else 'end',
                                usage=(raw['usage']['input_tokens'],raw['usage']['output_tokens']),raw=raw)
                raise RuntimeError('Legacy replay currently supports OpenAI only; refusing altered provider state')
            chat.step=step
            return chat
        make._recovery_original = original
        providers.make_chat=make

    def install_simulator(self, sim):
        sim = copy.copy(sim)
        run = getattr(sim.run_simulation, "__dict__", {}).get("_recovery_original", sim.run_simulation)
        check = getattr(sim.run_distribution_check, "__dict__", {}).get("_recovery_original", sim.run_distribution_check)
        self.sim=sim
        def replay_result(design, commit):
            e=self.peek();v=e['payload']
            if e['event']=='simulation':
                if canonical(v['design']) != canonical(design): raise RuntimeError('Replay simulation design mismatch')
                return v['results']
            if e['event']=='verification': return v['results']
            if e['event']=='tool' and v['name']=='run_simulation': return v['result']['results']
            raise RuntimeError('No recorded simulation at replay cursor')
        def wrapped_run(design, commit=True):
            if self.peek():
                r=copy.deepcopy(replay_result(design,commit))
                if commit:self.last_design=copy.deepcopy(design);self.last_dist=None
                return r
            self.frontier('simulation')
            result=run(design,commit=commit)
            if commit:
                self.last_design=copy.deepcopy(design);self.last_dist=None
                if result.get('status') in ('optimal','feasible_time_limit'):
                    sim.save_dispatch_checkpoint(self.snapshot)
                    self.cache_ready=True
                else:self.cache_ready=False
            return result
        def wrapped_check(design):
            e=self.peek()
            if e:
                v=e['payload']
                if e['event']=='simulation':result=v['distribution_check']
                elif e['event']=='distribution_check':result=v['results']
                elif e['event']=='tool' and v['name']=='run_simulation':result=v['result']['distribution_check']
                elif e['event']=='tool' and v['name']=='run_distribution_check':result=v['result']
                else:raise RuntimeError('No recorded distribution result at replay cursor')
                self.last_design=copy.deepcopy(design);self.last_dist=copy.deepcopy(result)
                return copy.deepcopy(result)
            self.frontier('distribution')
            if not self.cache_ready:
                if self.snapshot.exists():
                    sim.restore_dispatch_checkpoint(self.snapshot, design);self.cache_ready=True
                elif self.last_dist is not None and canonical(design)==canonical(self.last_design):
                    # Exact same design/dispatch already has a saved full OpenDSS result.
                    return copy.deepcopy(self.last_dist)
                else:
                    raise RuntimeError('Legacy log has no saved dispatch for this new feeder variant; refusing an unreported re-solve')
            result=check(design)
            self.last_design=copy.deepcopy(design);self.last_dist=copy.deepcopy(result)
            return result
        wrapped_run._recovery_original=run
        wrapped_check._recovery_original=check
        sim.run_simulation=wrapped_run
        sim.run_distribution_check=wrapped_check
        return sim

    def audit_final(self, final):
        if not self.audit: return
        if self.peek(): raise RuntimeError('Unconsumed historical events at finalization')
        old_path=self.out/'final.json'
        if old_path.exists():
            old=json.loads(old_path.read_text())
            new=copy.deepcopy(final)
            for d in (old,new):
                d.pop('implementation',None);d.pop('recovery',None)
                d.get('metrics',{}).pop('runtime_seconds',None)
                d.get('metrics',{}).pop('resume_process_seconds',None)
            if canonical(old)!=canonical(new):
                raise RuntimeError('Reconstructed final differs from saved scientific results/metrics')
        self.frontier('finalize:scientific_results_match')

    def provenance(self):
        return dict(resumed=self.resume,replayed_events=self.index,
                    **({"authorized_limit_change": self.limit_change} if self.limit_change else {}),
                    old_runtime_includes_pause=False,
                    runtime_scope='this process, including offline reconstruction; prior active time is not recovered' if self.resume else 'uninterrupted process',
                    legacy_dispatch_available=self.snapshot.exists())
