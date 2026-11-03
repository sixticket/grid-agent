"""Render complete main-text results from frozen evidence; no experimental calls."""
from collections import Counter
from pathlib import Path
import hashlib
import json
import math
import argparse
try:
    from .common import evidence_root, output_root, validate_cohort, REPORTED_SNAPSHOT
except ImportError:
    from common import evidence_root, output_root, validate_cohort, REPORTED_SNAPSHOT

HERE = Path(__file__).resolve().parent
REGIONS = ['miso', 'ercot', 'nyiso', 'south_africa', 'australia',
           'new_zealand', 'mexico', 'brazil']
NAMES = ['MISO', 'ERCOT', 'NYISO', 'South Africa', 'Australia (NEM)',
         'New Zealand', 'Mexico', 'Brazil']
ALIASES = {'sol_6_1': 'sol61',
           'claude_opus': 'opus', 'opus_5_5': 'opus'}
LABELS = {'sol61': 'GPT-6.1 Sol', 'opus': 'Opus 5.5'}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, default=evidence_root() / REPORTED_SNAPSHOT)
    parser.add_argument('--out', type=Path, default=output_root())
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    source_data = json.loads(args.evidence.read_text())
    validate_cohort(source_data['runs'])
    records = []
    for source in [str(args.evidence)]:
        for record in source_data['runs']:
            records.append((source, record))
    assert len(records) == 96
    keyed = {}
    cells = []
    for source, record in records:
        model = ALIASES[record['deployment']]
        key = (model, record['demand_year'], record['region'], record['agent'])
        assert key not in keyed and record['demand_year'] == record['weather_year']
        selected = record['selected']
        assert record['completed'] and selected['acceptance']['passes']
        value = selected['all_in_cost_billion']
        assert math.isfinite(value) and value > 0
        assert math.isclose(value, selected['system_cost_billion'] +
                            selected['distribution_cost_billion'], abs_tol=1e-8)
        keyed[key] = record
        cells.append(dict(model=model, year=key[1], region=key[2], architecture=key[3],
                          case_id=record['case_id'], source=source, accepted=True,
                          selected_annual_cost_billion=value, displayed_cost=f'{value:.3f}'))
    expected = {(model, year, region, agent)
                for model in ['sol61', 'opus'] for year in [2023, 2030, 2035]
                for region in REGIONS for agent in ['single', 'multi']}
    assert set(keyed) == expected and len(expected) == 96
    cohort_rows = []
    tex = [r'\subsection{Complete experiment coverage and retained planning outcomes}\label{sec:complete-results}',
           r'The three-year GPT-6.1 Sol and Claude Opus 5.5 coverage contains 96 trajectories across eight systems, three matching demand/weather years, two deployments and single- and multi-agent architectures. All 96 trajectories are reported in Table~\ref{tab:complete-coverage}. The Opus 2023 results come from the earlier base-year campaign; deployment versions and recorded inference settings remain distinct.',
           r'Every reported trajectory retains a physically accepted committed candidate. Table~\ref{tab:all-selected-costs} reports its selected total annual cost for every system, deployment, year and architecture. These outcomes demonstrate operation of the framework across the full evaluated scope. Detailed costs, candidate-specific solver bounds and effort records remain in Appendix~\ref{app:outcomes}.',
           r'The geographic portfolios in Fig.~\ref{fig:geographic-planning} show the lowest-cost accepted 2035 design observed for each system across both deployments and architectures, with tied costs explicitly identified. The Opus ERCOT repair in Fig.~\ref{fig:ercot-verified} is a separate process example. These spatial outputs complement the complete results in Tables~\ref{tab:complete-coverage} and~\ref{tab:all-selected-costs}.',
           r'\begin{table}[htp]',
           r'\caption{\textbf{Complete experimental coverage and physical acceptance.} Each cohort contains eight systems and both architectures. The complete GPT-6.1 Sol--Opus grid contains 96 trajectories. Accepted counts refer to trajectories with a retained committed candidate passing the implemented system and representative-feeder gate. Baseline resources are ERA5 2023; future resources are reconstructed for the indicated NASA projection year.}',
           r'\label{tab:complete-coverage}', r'\centering\small',
           r'\begin{tabular}{@{}llrrrr@{}}', r'\toprule',
           r'Deployment & Demand / weather & Single & Multi & Accepted & Total \\', r'\midrule']
    for model, years in [('sol61', [2023, 2030, 2035]), ('opus', [2023, 2030, 2035])]:
        for year in years:
            rows = [r for (m, y, _, _), r in keyed.items() if (m, y) == (model, year)]
            counts = Counter(r['agent'] for r in rows)
            assert counts == {'single': 8, 'multi': 8}
            cohort_rows.append(dict(model=model, year=year, single=8, multi=8, accepted=16, total=16))
            tex.append(f'{LABELS[model]} & {year} / '+('ERA5' if year == 2023 else 'NASA')+
                       r' & 8 & 8 & 16 & 16 \\')
    tex += [r'\midrule', r'Total & Year matched & 48 & 48 & 96 & 96 \\',
            r'\bottomrule', r'\end{tabular}', r'\end{table}',
            r'\begin{tableorg}[p]',
            r'\caption{\textbf{Selected planning outcomes for all 96 trajectories.} Entries are total annual power-system costs in billion USD per year, including annualized representative-feeder interventions, rounded to three decimals. S and M denote single- and multi-agent architectures. The Accepted column counts retained physically accepted trajectories within each system--deployment row. Full numerical precision, candidate-specific solver bounds and evaluation effort are retained in Appendix~\ref{app:outcomes}.}',
            r'\label{tab:all-selected-costs}', r'\centering\small',
            r'\makebox[\textwidth][c]{\begin{tabular*}{170mm}{@{\extracolsep{\fill}}llrrrrrrr@{}}',
            r'\toprule',
            r'System & Deployment & \multicolumn{2}{c}{2023} & \multicolumn{2}{c}{2030} & \multicolumn{2}{c}{2035} & Accepted \\',
            r'\cmidrule(lr){3-4}\cmidrule(lr){5-6}\cmidrule(lr){7-8}',
            r' & & S & M & S & M & S & M & \\', r'\midrule']
    table_cells = 0
    for region, name in zip(REGIONS, NAMES):
        for i, model in enumerate(['sol61', 'opus']):
            row = [name if i == 0 else '', LABELS[model]]
            n = 0
            for year in [2023, 2030, 2035]:
                for agent in ['single', 'multi']:
                    record = keyed.get((model, year, region, agent))
                    row.append(f"{record['selected']['all_in_cost_billion']:.3f}" if record else '--')
                    n += record is not None
                    table_cells += record is not None
            row.append(f'{n}/{n}')
            tex.append(' & '.join(row) + r' \\')
        if region != REGIONS[-1]:
            tex.append(r'\addlinespace[3pt]')
    tex += [r'\bottomrule', r'\end{tabular*}}', r'\end{tableorg}']
    assert table_cells == 96
    for i in [1, 2, 3]:
        tex[i] += '\n'
    (args.out / 'main_results.tex').write_text('\n'.join(tex) + '\n')
    receipt = dict(reported=96, primary_three_year_grid=96, 
                   single=48, multi=48, accepted=96, year_matched=True,
                   illustrative_map_cases=8, illustrated_selection='Lowest observed accepted 2035 cost across both deployments and architectures; explicit ties',
                   illustrated_year=2035, illustrated_architectures=['single','multi'],
                   sources={str(args.evidence): sha(args.evidence)},
                   builder_sha256=sha(__file__), tex_sha256=sha(args.out / 'main_results.tex'),
                   coverage_rows=cohort_rows, cost_cells=cells,
                   new_inference_calls=0, new_optimizer_calls=0, new_power_flow_calls=0)
    (args.out / 'main_results_audit.json').write_text(
        json.dumps(receipt, indent=2, ensure_ascii=False) + '\n')
    print(json.dumps({k: receipt[k] for k in ['reported', 'primary_three_year_grid',
                                            'single', 'multi', 'accepted']}))


if __name__ == '__main__':
    main()
