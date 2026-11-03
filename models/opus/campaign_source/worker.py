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
    args = parser.parse_args()
    record = json.loads(args.job.read_text())
    job, config, settings = record['job'], record['config'], record['settings']
    pack, out = Path(job['pack']), args.job.resolve().parent
    sys.path.insert(0, str(BASE))
    from run_campaign import input_hashes, source_hashes, save, assert_worker_authorized
    assert_worker_authorized(record)
    if record['source_sha256'] != source_hashes() or job['input_sha256'] != input_hashes(pack):
        raise ValueError('Code or inputs changed after scheduling.')
    if (out / 'log.jsonl').exists() or (out / 'final.json').exists():
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
    runtime = Path(job['runtime'])
    sys.path.insert(0, str(runtime / 'engine'))
    import scenario_core
    from climate_adapter import install_climate_adapter
    adapter_receipt = install_climate_adapter(scenario_core, pack, expected_region=job['region'], expected_year=job['year'])
    save(out / 'climate_adapter.json', adapter_receipt)
    manifest_path = pack / 'prediction_manifest.json'
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else None
    module = importlib.import_module(job['agent'] + '_agent')
    sys.argv = [job['agent'] + '_agent', '--scenario', str(pack), '--planning-mode', 'brownfield',
                '--policy', settings['policy'], '--provider', config['provider'], '--model', config['model']]
    sys.argv += (['--max-cycles', str(settings['max_cycles'])] if job['agent'] == 'multi' else
                 ['--max-runs', str(settings['max_runs']), '--max-turns', str(settings['max_turns'])])
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
