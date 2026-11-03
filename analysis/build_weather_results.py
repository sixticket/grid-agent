#!/usr/bin/env python3
"""Audit and render completed hosted weather trajectories without inference or solves.

The original experiment archives are read-only inputs. Candidate selection reuses the manuscript's pure-JSON
physical acceptance and committed-history reconstruction.
"""
import argparse
import csv
from collections import Counter
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import statistics

HERE = Path(__file__).resolve().parent
try:
    from .common import evidence_root, output_root, add_archive_arguments, resolver_from_args, ArchiveResolver, paper_data
except ImportError:
    from common import evidence_root, output_root, add_archive_arguments, resolver_from_args, ArchiveResolver, paper_data
EVIDENCE = 'evidence/completed_weather_20261003'
REGIONS = ['miso', 'ercot', 'nyiso', 'south_africa', 'australia', 'new_zealand', 'mexico', 'brazil']
LABELS = dict(zip(REGIONS, ['MISO', 'ERCOT', 'NYISO', 'South Africa', 'Australia', 'New Zealand', 'Mexico', 'Brazil']))
MODEL_LABELS = {'sol_6_1': 'GPT-6.1 Sol', 'opus_5_5': 'Claude Opus 5.5'}


def require(value, message):
    if not value:
        raise ValueError(message)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + '\n')


def audit(root, out, resolver=None):
    root, out = Path(root).resolve(), Path(out).resolve()
    resolver = resolver or ArchiveResolver(root)
    out.mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location('committed_evidence', HERE / 'committed_evidence.py')
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    sources = {}

    def pin(path, expected=None):
        path = resolver(path)
        key = str(path)
        if key not in sources:
            sources[key] = {'sha256': sha(path), 'bytes': path.stat().st_size}
        if expected is not None:
            require(sources[key]['sha256'] == expected, 'Source hash mismatch: ' + key)
        return sources[key]['sha256']

    active_path = root / 'ACTIVE_HOSTED_QUEUE.json'
    active = json.loads(active_path.read_text())
    require(active['status'] == 'completed' and active['completed_experiments'] == 80, 'Hosted queue is not complete')
    pin(active_path)
    for k in ('queue_file', 'launch_file'):
        pin(active[k])
    queue = json.loads(resolver(active['queue_file']).read_text())
    require(queue['status'] == 'completed' and not queue.get('error'), 'Queue has terminal errors')
    for path, digest in queue['preserved_all_final_sha256'].items():
        pin(path, digest)
    runs = []
    all_response_ids = set()
    annual_demands = {}
    for deployment, key in [('sol_6_1', 'sol_campaign'), ('opus_5_5', 'opus_campaign')]:
        campaign_path = resolver(active[key])
        campaign = json.loads(campaign_path.read_text())
        pin(campaign_path)
        for job in campaign['jobs']:
            case = deployment + '/' + job['id']
            require(job['status'] == 'completed' and job['attempts'][-1]['exit_code'] == 0, 'Incomplete case: ' + case)
            attempt = resolver(job['output'])
            final_path = attempt / 'final.json'
            final = json.loads(final_path.read_text())
            require(final['acceptance']['passes'] is True, 'Rejected final: ' + case)
            require(final['final_selection']['policy'] == helper.POLICY, 'Selection rule changed: ' + case)
            require(final['hours'] == 8760 and final['mode'] == 'brownfield', 'Physical scope changed: ' + case)
            for name in ('final.json', 'log.jsonl', 'api_usage.jsonl', 'api_responses.jsonl', 'job.json', 'best_accepted.json'):
                if (attempt / name).exists():
                    pin(attempt / name)
            for name in ('dispatch_checkpoint.json', 'dispatch_checkpoint.nc', 'input_transformations.jsonl', 'recovery_requests.jsonl'):
                if (attempt / name).exists():
                    pin(attempt / name)
            for relative, digest in final['input_sha256'].items():
                pin(resolver(job['pack']) / relative, digest)
            replay = helper.replay((attempt / 'log.jsonl').read_bytes(), job['agent'], final)
            selected = helper.summarize_candidate(final['final_design'], final['final_results'], final['final_distribution_check'], final, final['final_selection']['source'], None)
            require(replay['selected']['candidate_sha256'] == selected['candidate_sha256'], 'Committed selection mismatch: ' + case)
            require(selected['acceptance']['passes'], 'Recomputed physical rejection: ' + case)
            require(abs(selected['all_in_cost_billion'] - final['final_selection']['all_in_cost_billion']) < 1e-9, 'All-in cost mismatch: ' + case)
            metrics = final['metrics']
            require(replay['logged_simulations'] == metrics['sim_runs'], 'Committed count mismatch: ' + case)
            require(replay['logged_verifications'] == metrics.get('verification_sims', 0), 'Verification count mismatch: ' + case)
            ledger = [json.loads(x) for x in (attempt / 'api_usage.jsonl').read_text().splitlines() if x.strip()]
            responses = [json.loads(x) for x in (attempt / 'api_responses.jsonl').read_text().splitlines() if x.strip()]
            ids = [x['response_id'] for x in ledger]
            require(len(ids) == len(set(ids)), 'Duplicate received response: ' + case)
            require(not (all_response_ids & set(ids)), 'Cross-case response duplication: ' + case)
            all_response_ids.update(ids)
            require(Counter(ids) == Counter(x['response_id'] for x in responses), 'Incomplete response ledger: ' + case)
            require(len(ids) == final['api_usage_summary']['api_calls'], 'Response count mismatch: ' + case)
            prediction = final.get('prediction', {})
            climate = prediction.get('climate', {})
            year = job['year']
            weather_year = prediction.get('weather_year', year)
            demand_path = resolver(job['pack']) / 'demand.csv'
            if str(demand_path) not in annual_demands:
                with demand_path.open() as stream:
                    annual_demands[str(demand_path)] = sum(sum(float(v) for k, v in row.items() if k != 'time') for row in csv.DictReader(stream)) / 1e6
            if year != 2023:
                require(climate['model'] == 'MPI-ESM1-2-HR' and climate['scenario'] == 'ssp245', 'Climate identity changed: ' + case)
                require(climate['projection_year'] == year and climate['weather_template_year'] == 2023 and climate['time_axis_year'] == 2023, 'Weather chronology mismatch: ' + case)
            runs.append(dict(case_id=case, deployment=deployment, model=campaign['config']['model'],
                year=year, demand_year=year, weather_year=weather_year,
                weather_source='ERA5 2023' if year == 2023 else 'NASA daily / ERA5 intraday synthetic',
                climate_model=None if year == 2023 else 'MPI-ESM1-2-HR', climate_scenario=None if year == 2023 else 'ssp245',
                weather_template_year=2023, time_axis_year=2023, region=job['region'], agent=job['agent'],
                completed=True, source_output=str(attempt), final_sha256=pin(final_path),
                selected=selected, initial=replay['initial'], first_accepted=replay['first_accepted'],
                candidates=replay['candidates'], initial_state_sha256=replay['initial_state_sha256'],
                metrics=metrics, stopped_by=final.get('stopped_by'), cycles_run=final.get('cycles_run'),
                api_usage_summary=paper_data(dict(final['api_usage_summary'], api_calls=len(ids))),
                settings=final['paper_run_settings'], input_sha256=final['input_sha256'], source_sha256=final['source_sha256'],
                implementation=final['implementation'], recovery=final.get('recovery'),
                annual_demand_TWh=annual_demands[str(demand_path)],
                started_at=job['attempts'][-1]['started_at'], finished_at=job['attempts'][-1]['finished_at'],
                climate=climate, protocol_audit=({k:final['thinking_protocol']['audit'][k] for k in ['coverage_complete', 'audited_responses', 'total_dropped_blocks']} if 'thinking_protocol' in final else None)))
    require(len(runs) == 80 and len({r['case_id'] for r in runs}) == 80, 'Coverage mismatch')
    require(all(r['demand_year'] == r['weather_year'] for r in runs), 'Demand/weather year mismatch')
    indexed = {(r['deployment'], r['year'], r['region'], r['agent']): r for r in runs}
    paired = []
    for year in (2030, 2035):
        for region in REGIONS:
            for agent in ('single', 'multi'):
                s = indexed['sol_6_1', year, region, agent]
                o = indexed['opus_5_5', year, region, agent]
                require(s['input_sha256'] == o['input_sha256'], 'Hosted inputs differ')
                numerical = ['hours', 'start_hour', 'policy', 'mode', 'solver', 'threads', 'solver_seconds', 'max_cycles', 'max_runs', 'max_turns']
                require(all(s['settings'][k] == o['settings'][k] for k in numerical), 'Hosted numerical settings differ')
                physical = [k for k in s['source_sha256'] if k.startswith('runtime_original/experiment/') and any(t in k for t in ['engine/planning.py', 'engine/simulator.py', 'engine/distribution.py', 'engine/transmission.py', 'engine/scenario_core.py', 'engine/policy.py', 'shared/'])]
                require(all(s['source_sha256'][k] == o['source_sha256'].get(k) for k in physical), 'Hosted physical source differs')
                initial_match = s['initial_state_sha256'] == o['initial_state_sha256'] if agent == 'single' else None
                require(agent != 'single' or initial_match, 'Hosted single-agent initial state differs')
                sc = s['selected']['all_in_cost_billion']; oc = o['selected']['all_in_cost_billion']
                sl = s['selected']['all_in_cost_lower_bound_billion']; ol = o['selected']['all_in_cost_lower_bound_billion']
                paired.append(dict(year=year, region=region, agent=agent,
                    opus_minus_sol_USD=(oc - sc) * 1e9, opus_minus_sol_percent=100 * (oc - sc) / sc,
                    interval_percent=[100 * (ol - sc) / sc, 100 * (oc - sl) / sc],
                    solver_cost_intervals_overlap=max(sl, ol) <= min(sc, oc) + 1e-11,
                    same_inputs=True, same_physical_source=True, same_initial_exposed_state=initial_match))
    architecture = []
    for deployment, years in [('sol_6_1', (2023, 2030, 2035)), ('opus_5_5', (2030, 2035))]:
        for year in years:
            for region in REGIONS:
                s = indexed[deployment, year, region, 'single']; m = indexed[deployment, year, region, 'multi']
                sc = s['selected']['all_in_cost_billion']; mc = m['selected']['all_in_cost_billion']
                sl = s['selected']['all_in_cost_lower_bound_billion']; ml = m['selected']['all_in_cost_lower_bound_billion']
                architecture.append(dict(deployment=deployment, year=year, region=region,
                    multi_minus_single_USD=(mc - sc) * 1e9, multi_minus_single_percent=100 * (mc - sc) / sc,
                    interval_percent=[100 * (ml - sc) / sc, 100 * (mc - sl) / sc],
                    solver_cost_intervals_overlap=max(sl, ml) <= min(sc, mc) + 1e-11))
    summary = {}
    for deployment, year in sorted({(r['deployment'], r['year']) for r in runs}):
        group = [r for r in runs if (r['deployment'], r['year']) == (deployment, year)]
        summary[deployment + '_' + str(year)] = dict(completed=len(group), accepted=sum(r['selected']['acceptance']['passes'] for r in group),
            committed_simulations=sum(r['metrics']['sim_runs'] for r in group),
            verification_simulations=sum(r['metrics'].get('verification_sims', 0) for r in group),
            model_requests=sum(r['api_usage_summary']['api_calls'] for r in group),
            multi_cycles_median=statistics.median(r['cycles_run'] for r in group if r['agent'] == 'multi'),
            initially_accepted=sum(r['initial']['acceptance']['passes'] for r in group),
            runtime_records_missing=sum(r['metrics'].get('runtime_seconds') is None for r in group))
    data = dict(schema_version=1, snapshot_revision='completed-hosted-weather80-20261003',
        extraction_cutoff_utc=datetime.now(timezone.utc).isoformat(), completed_at_utc=max(r['finished_at'] for r in runs),
        selection_policy=helper.POLICY, source_queue=str(Path(active['queue_file'])),
        audit=dict(committed_histories_reconstructed=80, selected_candidate_matches=80, physical_acceptance_recomputed=80,
            logical_evaluation_counts_verified=80, response_ledgers_verified=80, unique_received_responses=len(all_response_ids),
            preserved_completed_finals_verified=len(queue['preserved_all_final_sha256']),
            hosted_future_input_pairs_verified=32, hosted_physical_source_pairs_verified=32, hosted_single_initial_states_verified=16,
            new_inference_calls=0, new_optimizer_calls=0),
        limitations=['One trajectory per deployment/system/year/architecture; no replicated inferential test.',
            'Solver intervals condition on selected designs, not global design-search optimality or statistical uncertainty.',
            'NASA daily climate realization plus ERA5 2023 intraday templates; no deterministic target-year weather forecast.',
            'Earlier Sol is gpt-6-sol; new Sol is gpt-6.1-sol. Between-cohort differences also include response ceilings and search.',
            'Resumed trajectories have no uninterrupted wall-runtime estimate; inherited responses are counted once.'],
        runs=runs, summary=summary, deployment_pairs=paired, architecture_pairs=architecture)
    data = paper_data(data)
    require(all(sha(Path(p)) == info['sha256'] for p, info in sources.items()), 'An archive source changed during audit')
    save(out / EVIDENCE / 'results.json', data)
    save(out / EVIDENCE / 'source_manifest.json', dict(extraction_cutoff_utc=data['extraction_cutoff_utc'], sources=sources))
    return data


def table(rows, caption, label, headers):
    return '\n'.join([r'\begin{table}[tbp]', r'\caption{' + caption + '}', r'\label{' + label + '}',
        r'\centering\small', r'\begingroup\setlength{\tabcolsep}{4pt}',
        r'\begin{tabular}{@{}l' + 'r' * (len(headers) - 1) + r'@{}}', r'\toprule',
        ' & '.join(headers) + r'\\', r'\midrule',
        *[' & '.join(row) + r'\\' for row in rows], r'\bottomrule', r'\end{tabular}', r'\endgroup', r'\end{table}', ''])


def render(data, out, evidence_path=None):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "figures").mkdir(parents=True, exist_ok=True)
    require(len(data['runs']) == 80 and all(r['demand_year'] == r['weather_year'] for r in data['runs']), 'Frozen cohort is not year-matched')
    runs = data['runs']; idx = {(r['deployment'], r['year'], r['region'], r['agent']): r for r in runs}
    text = r'''\subsection{Planning with projected weather and matched demand}\label{sec:weather-results}
The completed hosted campaign adds eighty annual trajectories, comprising sixteen GPT-6.1 Sol controls with 2023 demand and ERA5 2023 weather, and sixty-four future-weather trajectories with GPT-6.1 Sol and Claude Opus 5.5. Each future deployment covers eight systems, 2030 and 2035 demand, and both architectures. NASA MPI-ESM1-2-HR SSP2-4.5 daily fields and ERA5 2023 intraday templates supply the target-year resource profiles (Methods, Section~\ref{sec:future-weather}). All eighty selected candidates pass the recomputed system and representative-feeder acceptance gate. Full committed-history reconstruction confirms that each selected result is the lowest-cost physically accepted committed candidate. All trajectories end through their single-agent finish decision or multi-agent judge.

'''
    for year in (2030, 2035):
        rows = [[LABELS[region]] + ['/'.join(f"{idx[model, year, region, agent]['selected']['all_in_cost_billion']:.6f}" for agent in ('single', 'multi')) for model in ('sol_6_1', 'opus_5_5')] for region in REGIONS]
        text += table(rows, f'Selected annual cost under {year} demand and NASA-derived {year} weather. Entries are single/multi, in billion USD per year including annualized representative-distribution interventions. Each cell is one trajectory; candidate-specific solver intervals are retained in the comparison evidence.', 'tab:weather-' + str(year), ['System', 'GPT-6.1 Sol', 'Claude Opus 5.5']) + '\n'
    pairs = data['deployment_pairs']
    lower = sum(p['opus_minus_sol_USD'] < -.01 for p in pairs); equal = sum(abs(p['opus_minus_sol_USD']) <= .01 for p in pairs); higher = sum(p['opus_minus_sol_USD'] > .01 for p in pairs)
    overlap = sum(p['solver_cost_intervals_overlap'] for p in pairs)
    text += f'Across the 32 matched future-weather deployment pairs, Opus selects a lower annual cost in {lower}, an equal cost to the nearest cent in {equal}, and a higher cost in {higher}. The candidate-specific solver intervals overlap in {overlap} pairs. Shared demand, weather, inventory and network hashes, numerical settings and physical-code hashes are verified for all 32 pairs; all sixteen single-agent initial exposed states also match. Figure~\\ref{{fig:weather-comparison}} shows achieved costs and their candidate-conditional numerical bounds.\n\n'
    text += r'''Additional multi-agent search does not uniformly reduce selected cost. Across the sixteen future region--year architecture pairs per deployment, GPT-6.1 Sol's multi-agent candidate is cheaper in one, equal to the nearest cent in seven and more expensive in eight. Opus's corresponding counts are three, ten and three. In Opus ERCOT 2035, multi-agent selection reduces annual cost from USD 83.762708 billion to USD 83.150660 billion (USD 612.048 million; 0.731\%). In South Africa 2035, it reduces cost from USD 14.202359 billion to USD 14.101251 billion (USD 101.108 million; 0.712\%). Conversely, the MISO 2035 single-agent selections cost approximately 0.48\% less than their multi-agent counterparts in both deployments. These are achieved outcomes of individual searches, with feasibility, economic improvement and stopping effort reported separately.

'''
    rows = []
    for model, year in [('sol_6_1', 2023), ('sol_6_1', 2030), ('sol_6_1', 2035), ('opus_5_5', 2030), ('opus_5_5', 2035)]:
        s = data['summary'][model + '_' + str(year)]
        rows.append([('Sol 6.1' if model == 'sol_6_1' else 'Opus 5.5') + ' ' + str(year), str(s['committed_simulations']), str(s['verification_simulations']), str(s['model_requests']), f"{s['multi_cycles_median']:g}"])
    text += table(rows, 'Evaluation effort in the completed eighty-trajectory hosted campaign. Each row contains sixteen trajectories. Evaluations are logical requests, including exact-result reuse, rather than physical optimizer invocations. The 2023 control row uses ERA5 2023 weather; the future rows use the corresponding NASA-derived weather.', 'tab:weather-effort', ['Deployment/year', 'Committed', 'Verify', 'Responses', 'Median cycles']) + '\n'
    sol = [r for r in runs if r['deployment'] == 'sol_6_1']; opus = [r for r in runs if r['deployment'] == 'opus_5_5']
    text += f"Across the new hosted campaign, GPT-6.1 Sol records {sum(r['metrics']['sim_runs'] for r in sol)} committed and {sum(r['metrics'].get('verification_sims', 0) for r in sol)} verification requests, with {sum(r['api_usage_summary']['api_calls'] for r in sol):,} unique received responses. Opus records {sum(r['metrics']['sim_runs'] for r in opus)} committed and {sum(r['metrics'].get('verification_sims', 0) for r in opus)} verification requests, with {sum(r['api_usage_summary']['api_calls'] for r in opus):,} responses. The median multi-agent search length is one cycle in each deployment/year cohort; the longest new searches occur in Sol ERCOT 2030 (eighteen cycles) and South Africa 2030 (fifteen). Ledger attribution counts each inherited or newly received response once; three resumed trajectories have no uninterrupted wall-runtime estimate.\n\n"
    text += r'''The sixteen GPT-6.1 Sol base-year controls also yield accepted candidates (Table~\ref{tab:sol61-control}). Five systems retain identical selected costs across architectures; NYISO, Mexico and South Africa have slightly higher multi-agent selected costs. The sixteen Opus 2023 trajectories complete the 32-case baseline comparison. Their recorded default response ceiling is 16,000 tokens, compared with 64,000 tokens in the GPT-6.1 Sol campaign. One trajectory per cell does not establish a general model ranking.

'''
    control_rows = []
    for region in REGIONS:
        s = idx['sol_6_1', 2023, region, 'single']; m = idx['sol_6_1', 2023, region, 'multi']
        control_rows.append([LABELS[region], f"{s['selected']['all_in_cost_billion']:.6f}/{m['selected']['all_in_cost_billion']:.6f}", f"{s['metrics']['sim_runs']+s['metrics'].get('verification_sims', 0)}/{m['metrics']['sim_runs']+m['metrics'].get('verification_sims', 0)}", f"{s['api_usage_summary']['api_calls']}/{m['api_usage_summary']['api_calls']}"])
    text += table(control_rows, 'GPT-6.1 Sol controls with 2023 demand and ERA5 2023 weather. Entries are single/multi. Selected costs are billion USD per year including distribution interventions; evaluations include committed and hypothetical requests.', 'tab:sol61-control', ['System', 'Selected cost', 'Evaluations', 'Responses'])
    (out / 'future_weather_results.tex').write_text(text)
    demand_rows=[]
    for region in REGIONS:
        values=[idx['sol_6_1',y,region,'single']['annual_demand_TWh'] for y in (2023,2030,2035)]
        demand_rows.append([LABELS[region],f"{values[0]:.3f}",f"{values[1]:.3f}",f"{100*(values[1]/values[0]-1):.1f}",f"{values[2]:.3f}",f"{100*(values[2]/values[0]-1):.1f}"])
    caption='Annual electricity demand in the year-matched inputs. The 2023 demand is paired with ERA5 2023 weather; 2030 and 2035 demand are paired with the corresponding NASA-derived weather. Energy is in TWh; changes are relative to 2023. Both architectures share each input. South African values are spatialized model outputs, Australian growth is mapped between consumption definitions, and New Zealand uses an island winter-growth annualization.'
    (out/'future_inputs_table.tex').write_text(table(demand_rows,caption,'tab:future-demand-inputs',['System','Demand 2023','Demand 2030','Change (\\%)','Demand 2035','Change (\\%)']))
    save(out/'figures/fig3_weather_comparison_audit.json',dict(source=EVIDENCE+'/results.json',source_sha256=sha(evidence_path or out/EVIDENCE/'results.json'),builder_sha256=sha(HERE/'build_weather_results.py'),year_matched=True,deployment_pairs=data['deployment_pairs'],architecture_pairs=[p for p in data['architecture_pairs'] if p['year']!=2023],interval_scope='Candidate-conditional solver bounds including distribution cost; not statistical confidence intervals.'))
    figure(data, out)


def figure(data, out):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams.update({'font.family': 'Arial', 'font.size': 8, 'pdf.fonttype': 42, 'axes.spines.top': False, 'axes.spines.right': False, 'text.color': '#000000', 'axes.labelcolor': '#000000', 'axes.titlecolor': '#000000', 'xtick.color': '#000000', 'ytick.color': '#000000'})
    fig, axes = plt.subplots(2, 2, figsize=(7.2, 6.0), sharey=True)
    colors = ['#2166ac', '#d6604d']
    labels = ['MISO', 'ERCOT', 'NYISO', 'South Africa', 'Australia', 'New Zealand', 'Mexico', 'Brazil']
    for col, year in enumerate((2030, 2035)):
        for row in (0, 1):
            ax = axes[row, col]
            ax.axvline(0, color='#000000', linewidth=.7)
            ax.grid(axis='x', color='#dddddd', linewidth=.5)
            ax.set_axisbelow(True)
            ax.set_yticks(range(8), labels)
            ax.set_ylim(7.6, -.6)
            for which, color in enumerate(colors):
                for k, region in enumerate(REGIONS):
                    if row == 0:
                        model = ('sol_6_1', 'opus_5_5')[which]
                        p = next(x for x in data['architecture_pairs'] if (x['deployment'], x['year'], x['region']) == (model, year, region))
                        value = p['multi_minus_single_percent']; label = ('Sol 6.1', 'Opus 5.5')[which]
                    else:
                        agent = ('single', 'multi')[which]
                        p = next(x for x in data['deployment_pairs'] if (x['year'], x['region'], x['agent']) == (year, region, agent))
                        value = p['opus_minus_sol_percent']; label = ('Single', 'Multi')[which]
                    low, high = p['interval_percent']
                    y = k + (-.12 if which == 0 else .12)
                    ax.plot([low, high], [y, y], color=color, linewidth=1.5)
                    ax.scatter(value, y, color=color, s=20, marker='o' if which == 0 else 's', facecolors=color if which == 0 else 'white', linewidths=1, label=label if k == 0 else None, zorder=3)
            ax.set_xlabel('Multi minus single cost (%)' if row == 0 else 'Opus minus Sol cost (%)')
            ax.set_title('abcd'[row*2+col] + '  ' + str(year), loc='left', fontweight='bold')
            ax.legend(frameon=False, fontsize=7.5, loc='lower right')
    fig.tight_layout(pad=1.4, w_pad=2, h_pad=2)
    (out / 'figures').mkdir(exist_ok=True)
    fig.savefig(out / 'figures/fig3_weather_comparison.pdf', bbox_inches='tight')
    fig.savefig(out / 'figures/fig3_weather_comparison.png', dpi=220, bbox_inches='tight')
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=evidence_root() / 'weather_prediction', help='Archive folder containing ACTIVE_HOSTED_QUEUE.json')
    add_archive_arguments(parser)
    parser.add_argument('--out', type=Path, default=output_root())
    parser.add_argument('--evidence', type=Path, help='Render an already frozen snapshot without re-extraction.')
    args = parser.parse_args()
    data = json.loads(args.evidence.read_text()) if args.evidence else audit(args.root, args.out, resolver_from_args(args))
    render(data, args.out, args.evidence)
    print(json.dumps({'completed': len(data['runs']), 'accepted': sum(r['selected']['acceptance']['passes'] for r in data['runs']), 'audit': data['audit']}))


if __name__ == '__main__':
    main()
