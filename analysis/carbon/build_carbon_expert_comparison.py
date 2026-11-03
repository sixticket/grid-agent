"""Compare saved carbon-constrained candidates with archived capacity-plan values."""
import argparse
import hashlib
import json
from pathlib import Path
from archive_paths import resolve, analysis_root, configure, drawing_modules


def read(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    configure(args.project, args.out)
    args.out.mkdir(parents=True, exist_ok=True)
    carbon_path = analysis_root(args.project) / 'carbon_data.json'
    reference_path = args.project / 'Nature_Energy/evidence/expert_comparison_20261007/comparison.json'
    carbon, archive = read(carbon_path), read(reference_path)
    sources = {k: archive['sources'][k] for k in ('epe', 'irp')}
    for source in sources.values():
        assert sha(args.project / source['archive_path']) == source['sha256']
    cohorts = {}
    candidate_sources = {}
    methods = {('sol', 'single'), ('sol', 'multi'), ('opus', 'single'), ('opus', 'multi')}
    for region in ('brazil', 'south_africa'):
        cohort = [r for r in carbon['rows'] if r['region'] == region and r['year'] == 2035]
        assert len(cohort) == 4 and {(r['model'], r['agent']) for r in cohort} == methods
        for row in cohort:
            directory = resolve(row['output'])
            best, final = read(directory / 'best_accepted.json'), read(directory / 'final.json')
            assert best['acceptance']['passes'] and final['acceptance']['passes']
            assert final['acceptance']['co2_budget']['passes']
            assert best['results']['capacities_GW'] == row['capacities'] == final['final_results']['capacities_GW']
            assert best['design'] == final['final_design']
            assert best['results']['hours_modelled'] == 8760
            assert row['cap'] == final['acceptance']['co2_budget']['annual_cap_Mt']
            for name in ('best_accepted.json', 'final.json'):
                candidate_sources[str(directory / name)] = sha(directory / name)
        cohorts[region] = cohort
    comparisons = []
    for old in archive['comparisons']:
        if old['region'] not in cohorts:
            continue
        # Reuse official-source transcription and category mapping only.
        # No archived agent capacities or differences are carried forward.
        region = old['region']
        key = old['result_field'].split('.')[1]
        values = [{'model': r['model'], 'agent': r['agent'], 'capacity_GW': r['capacities'][key]} for r in cohorts[region]]
        lower, upper = min(v['capacity_GW'] for v in values), max(v['capacity_GW'] for v in values)
        reference = old['reference_GW']
        comparisons.append({
            'region': region, 'year': 2035, 'metric': old['metric'],
            'result_field': old['result_field'], 'reference_GW': reference,
            'carbon_agent_min_GW': lower, 'carbon_agent_max_GW': upper,
            'difference_min_GW': lower - reference, 'difference_max_GW': upper - reference,
            'carbon_agent_values': values, 'carbon_budget_Mt': cohorts[region][0]['cap'],
            **{k: old[k] for k in ('source', 'locator', 'source_components', 'horizon', 'scenario', 'scope', 'excluded_distributed_capacity_GW')},
        })
    assert len(comparisons) == 5
    output = {
        'cohort': 'carbon-constrained', 'year': 2035, 'systems': ['brazil', 'south_africa'],
        'accepted_candidates': 8, 'capacity_basis': 'Total capacity including inherited assets; battery power in GW.',
        'range_semantics': 'Observed range of four retained accepted trajectories per system, not uncertainty intervals.',
        'sources': sources, 'comparisons': comparisons,
        'input_sha256': {str(carbon_path): sha(carbon_path), str(reference_path): sha(reference_path)},
        'candidate_source_sha256': candidate_sources,
        'builder_sha256': sha(Path(__file__)),
        'independence': 'PDE 2035 supplies Brazilian demand and the adopted numerical emissions ceiling; IRP 2025 supplies the South African emissions pathway. Capacity comparisons are descriptive and not validation against withheld targets.',
        'solar_boundary': 'Brazil plan centralized solar excludes 78.099 GW MMGD. South Africa Solar PV excludes 16.065 GW distributed generation, CSP and hybrid columns. Model solar has no separate future distributed-generation rollout.',
        'new_optimizer_calls': 0, 'new_api_calls': 0,
    }
    (args.out / 'carbon_expert_comparison.json').write_text(json.dumps(output, indent=2) + '\n')
    def interval(lo, hi, signed=False):
        fmt = '+.2f' if signed else '.2f'
        if round(lo, 2) == round(hi, 2):
            return format(lo, fmt)
        return rf'\([{format(lo, fmt)},{format(hi, fmt)}]\)' if signed else f'{lo:.2f}--{hi:.2f}'
    lines = []
    for r in comparisons:
        system = 'Brazil' if r['region'] == 'brazil' else 'South Africa'
        metric = r['metric']
        if metric in ('Centralized solar', 'Solar PV'):
            metric = r'Solar$^\dagger$'
        lines.append(f"{system} & {metric} & {r['reference_GW']:.2f} & {interval(r['carbon_agent_min_GW'],r['carbon_agent_max_GW'])} & {interval(r['difference_min_GW'],r['difference_max_GW'],True)} " + r'\\')
    table = r'''\begin{table}[tbp]
\caption{Published plans and carbon-constrained portfolios in 2035. Values are total capacities in GW, including inherited assets. Carbon ranges span all four retained accepted deployment--architecture trajectories per system. Differences are carbon-constrained agent values minus the published plan, calculated before rounding. Brazil and South Africa use annual model budgets of 29 and 142~MtCO$_2$, respectively.}
\label{tab:expert-capacity}
\centering\small
\setlength{\tabcolsep}{5pt}
\begin{tabularx}{\textwidth}{@{}lXrrr@{}}
\toprule
System & Capacity & Expert plan & Carbon range & Difference \\
\midrule
''' + '\n'.join(lines) + r'''
\bottomrule
\end{tabularx}
\smallskip
\par\footnotesize $^\dagger$ Brazil's PDE 2035 centralized-solar category excludes 78.099~GW of micro-/mini-distributed generation. South Africa's IRP 2025 Solar PV column excludes 16.065~GW in its separate distributed-generation column, CSP and hybrid capacity. Model solar has no separate future distributed-generation rollout. The Brazilian values describe December 2035; the South African values combine the end-2025 base and scheduled additions through 2035. Demand, resources, inherited fleets, retirements, technology options and delivery schedules retain their respective study assumptions. Source locations and mappings are given in Methods and Appendix~\ref{app:regional-sources}.
\end{table}
'''
    (args.out / 'carbon_expert_comparison_table.tex').write_text(table)
    for r in comparisons:
        print(r['region'], r['metric'], r['reference_GW'], r['carbon_agent_min_GW'], r['carbon_agent_max_GW'])


if __name__ == '__main__':
    main()
