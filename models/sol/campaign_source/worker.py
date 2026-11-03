#!/usr/bin/env python3
"""Run one new experiment using this model directory's frozen engine."""

# Preserved campaign source reference.
if __name__ == '__main__':
    raise SystemExit('Reference only; use tools/run_campaign.py. Historical replay requires the original pinned archive.')

import argparse
import importlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import sys

BASE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job', type=Path, required=True)
    parser.add_argument('--audit-history', action='store_true')
    args = parser.parse_args()
    record = json.loads(args.job.read_text())
    job, config, settings = record['job'], record['config'], record['settings']
    pack, out = Path(job['pack']), args.job.resolve().parent
    sys.path.insert(0, str(BASE))
    from run_campaign import input_hashes, source_hashes, save, assert_worker_authorized
    assert_worker_authorized(record)
    if record['source_sha256'] != source_hashes() or job['input_sha256'] != input_hashes(pack):
        raise ValueError('Code or inputs changed after scheduling.')
    sys.path.insert(0, str(BASE / 'adapters'))
    from history_resume import load_manifest, verify_copy, verify_audit, accounting_split, require, expected_events, sha
    history = load_manifest(record['history_resume'], record) if record.get('history_resume') else None
    event_count = expected_events(record) if history else None
    if args.audit_history and history is None:
        raise ValueError('--audit-history requires pinned history recovery')
    if history:
        require(not (out / 'final.json').exists(), 'Cannot replay a finalized attempt')
        verify_copy(history, out)
        if not args.audit_history:
            verify_audit(record, out)
    elif (out / 'log.jsonl').exists() or (out / 'final.json').exists():
        raise ValueError('Worker requires a fresh attempt directory.')
    os.environ.update(GRID_EXPERIMENT_HOURS=str(settings['hours']), GRID_EXPERIMENT_START='0',
                      GRID_SOLVER=settings['solver'], GRID_SOLVER_THREADS=str(settings['threads']),
                      GRID_SOLVER_TIME_LIMIT=str(settings['solver_seconds']),
                      GRID_SOLVER_LOGDIR=str(out), GRID_RUN_OUTPUT=str(out),
                      GRID_API_USAGE_LOG=str(out / 'api_usage.jsonl'),
                      GRID_API_RESPONSE_LOG=str(out / 'api_responses.jsonl'),
                      GRID_REASONING_EFFORT=config['effort'], GRID_ANTHROPIC_CACHE_TTL='5m')
    for key in ('GRID_RESUME', 'GRID_RECOVERY_AUDIT', 'GRID_RESUME_MAX_CYCLES', 'GRID_SOLVER_EXTRA'):
        os.environ.pop(key, None)
    if history:
        os.environ['GRID_RESUME'] = '1'
    if args.audit_history:
        os.environ['GRID_RECOVERY_AUDIT'] = '1'
    audit_counts = dict(api_calls=0, optimization_calls=0)
    frontier = {}
    if args.audit_history:
        import socket
        def no_network(*args, **kwargs):
            audit_counts['api_calls'] += 1
            raise RuntimeError('Network/API disabled during history replay audit')
        def no_optimization(*args, **kwargs):
            audit_counts['optimization_calls'] += 1
            raise RuntimeError('Optimization disabled during history replay audit')
        socket.create_connection = no_network
        socket.socket.connect = no_network
        socket.socket.connect_ex = no_network
    runtime = Path(job['runtime'])
    sys.path.insert(0, str(runtime / 'engine'))
    import scenario_core
    from climate_adapter import install_climate_adapter
    adapter_receipt = install_climate_adapter(scenario_core, pack, expected_region=job['region'], expected_year=job['year'])
    if not args.audit_history:
        save(out / 'climate_adapter.json', adapter_receipt)
    manifest_path = pack / 'prediction_manifest.json'
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else None
    if args.audit_history:
        import providers
        import planning
        import recovery
        providers.OpenAIChat.step = no_network
        planning.solve_system = no_optimization
        planning.choose_solver = no_optimization
        original_frontier = recovery.Recovery.frontier
        def capture_frontier(replayed, operation):
            frontier.update(next_operation=operation, replayed_events=replayed.index,
                            total_events=len(replayed.events))
            if history.get('kind') == 'sol_openai_sa_history_resume' and operation == 'api:openai':
                import hashlib
                request = sys._getframe(1).f_locals.get('request')
                require(request is not None, 'SA replay frontier lacks the next critic request')
                frontier['next_request'] = dict(
                    instructions_sha256=hashlib.sha256(request['system'].encode()).hexdigest(),
                    max_output_tokens=request['max_tokens'] or providers.MAX_TOKENS,
                    previous_response_id=request['previous_response_id'],
                    request_hash=hashlib.sha256(recovery.canonical(request).encode()).hexdigest())
            return original_frontier(replayed, operation)
        recovery.Recovery.frontier = capture_frontier
    module = importlib.import_module(job['agent'] + '_agent')
    sys.argv = [job['agent'] + '_agent', '--scenario', str(pack), '--planning-mode', 'brownfield',
                '--policy', settings['policy'], '--provider', config['provider'], '--model', config['model']]
    sys.argv += (['--max-cycles', str(settings['max_cycles'])] if job['agent'] == 'multi' else
                 ['--max-runs', str(settings['max_runs']), '--max-turns', str(settings['max_turns'])])
    if args.audit_history:
        try:
            module.main()
        except recovery.ReplayComplete as completed:
            require(completed.code == 0 and frontier.get('next_operation') == 'api:openai'
                    and frontier.get('replayed_events') == frontier.get('total_events') == event_count
                    and audit_counts == dict(api_calls=0, optimization_calls=0),
                    'History replay audit reached a different frontier')
            verify_copy(history, out)
            save(out / 'replay_audit.json', dict(status='passed', **frontier, **audit_counts,
                 history_resume=record['history_resume'], source_sha256=record['source_sha256'],
                 job_sha256=sha(out / 'job.json')))
            return 0
        raise ValueError('History audit did not reach replay frontier')
    module.main()
    final = json.loads((out / 'final.json').read_text())
    final.update(region=job['region'], year=job['year'], agent=job['agent'], mode='brownfield',
                 hours=settings['hours'], start_hour=0,
                 physical_runtime=str(runtime), physics_profile=job['physics_profile'],
                 climate_model=job['climate_model'], weather_source=job['weather_source'],
                 climate_adapter=adapter_receipt,
                 paper_run_settings=dict(**settings, **config), input_sha256=job['input_sha256'],
                 source_sha256=record['source_sha256'],
                 runtime_versions={p: version(p) for p in ('openai', 'anthropic', 'numpy', 'pandas',
                                                           'pypsa', 'linopy', 'gurobipy', 'OpenDSSDirect.py')})
    if manifest:
        final['prediction'] = manifest['scenario']
        final['prediction_run_settings'] = dict(**settings, **config)
        final['distribution_scope'] = ('Representative feeders are normalized to each scenario zonal peak. '
                                       'Demand growth changes the implied feeder count; this does not test growth '
                                       'on a fixed 2023 distribution fleet or account for new-feeder construction costs.')
    if config['provider'] == 'anthropic':
        from opus_protocol import validate_journals
        final['thinking_protocol'] = validate_journals(out)
    from api_usage import summarize_ledger
    final['api_usage_summary'] = summarize_ledger(out / 'api_usage.jsonl')
    if history:
        final['history_resume'] = dict(reference=record['history_resume'],
                                     source_attempt=history['source_attempt'],
                                     **accounting_split(history, out))
    from collections import Counter
    journal = [json.loads(x) for x in (out / 'api_responses.jsonl').read_text().splitlines() if x.strip()]
    ledger = [json.loads(x) for x in (out / 'api_usage.jsonl').read_text().splitlines() if x.strip()]
    if Counter(x['response_id'] for x in ledger) - Counter(x['response_id'] for x in journal):
        raise ValueError('API response journal does not cover usage ledger')
    final['api_response_journal'] = dict(recorded_responses=len(journal), accounted_responses=len(ledger), ledger_coverage_complete=True)
    final['agent_reported_usage'] = {key: final['metrics'].get(key)
                                   for key in ('api_calls', 'input_tokens', 'output_tokens')}
    for key in ('api_calls', 'input_tokens', 'output_tokens'):
        final['metrics'][key] = final['api_usage_summary'].get(key)
    final['metrics']['legacy_estimated_llm_cost_usd'] = final['metrics'].get('estimated_llm_cost_usd')
    final['metrics']['estimated_llm_cost_usd'] = final['api_usage_summary']['estimated_cost_usd']
    final['metrics']['billed_api_calls_recorded'] = final['api_usage_summary']['api_calls']
    save(out / 'final.json', final)
    from reporting import write_report
    write_report(out, final)
    with (out / 'report.md').open('a') as f:
        f.write('\n## Model and usage\n\n' + json.dumps(config, ensure_ascii=False) + '\n\n' +
                json.dumps(final['api_usage_summary'], indent=2) + '\n')
        if manifest:
            f.write('\n## Future demand\n\n' + json.dumps(manifest['scenario'], ensure_ascii=False, indent=2) + '\n')
    # A completed search may fail physical acceptance; record that outcome and continue.
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
