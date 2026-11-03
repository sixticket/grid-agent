#!/usr/bin/env python3
"""Render the accepted carbon-constrained portfolios without invoking simulation.

Usage: python geo_carbon.py --project /path/to/grid_agent --out /path/to/output
Only reads completed campaign evidence and the existing Natural Earth archives.
"""
from pathlib import Path
from archive_paths import resolve, analysis_root, configure, drawing_modules
from collections import defaultdict
import argparse
import csv
import hashlib
import json
import math
import os
import sys
import tempfile

os.environ.setdefault('MPLCONFIGDIR', str(Path(tempfile.gettempdir()) / 'carbon-geo-mpl'))
sys.dont_write_bytecode = True
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.lines import Line2D

REGION_NAMES = {'nyiso': 'NYISO', 'miso': 'MISO', 'mexico': 'Mexico',
                'brazil': 'Brazil', 'south_africa': 'South Africa'}
MODEL_NAMES = {'sol': 'GPT-6.1 Sol', 'opus': 'Claude Opus 5.5'}
METHOD_ORDER = [('sol', 'single'), ('sol', 'multi'), ('opus', 'single'), ('opus', 'multi')]
TECHS = ['wind', 'solar', 'nuclear', 'battery']
SOURCE_FILES = {}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def pin(path):
    SOURCE_FILES[str(path)] = sha(path)


def read_json(path):
    pin(path)
    return json.loads(Path(path).read_text())


def dump(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n')


def extract(project, geo):
    sequence = read_json(project / 'co2_constraint/sequence.json')
    candidates = defaultdict(list)
    for stage in sequence['stages']:
        campaign = read_json(resolve(stage['campaign']) / 'campaign.json')
        assert campaign['status'] == 'completed'
        for job in campaign['jobs']:
            assert job['status'] == 'completed'
            attempt = job['attempts'][-1]
            assert attempt['status'] == 'completed'
            source = resolve(attempt['output'])
            final = read_json(source / 'final.json')
            best = read_json(source / 'best_accepted.json')
            assert final['acceptance']['passes'] and best['acceptance']['passes']
            assert final['acceptance']['checks']['co2_budget']
            gate = best['acceptance']['co2_budget']
            assert gate['passes'] and gate['emissions_within_cap']
            assert final['hours'] == 8760
            for a, b in [('design', 'final_design'), ('results', 'final_results'),
                         ('distribution_check', 'final_distribution_check')]:
                assert best[a] == final[b], (job['id'], a)
            cost = best['all_in_cost_billion']
            assert cost == final['final_selection']['all_in_cost_billion']
            assert math.isclose(cost, best['results']['total_cost_billion'] +
                                best['distribution_check']['distribution_capex_billion'], abs_tol=1e-8)
            candidates[job['case']['id']].append({
                'job_id': job['id'], 'model_key': job['model_key'], 'agent': job['agent'],
                'region': final['region'], 'year': final['year'], 'cost': cost,
                'source': str(source), 'final': final, 'best': best})
    assert len(candidates) == 10 and all(len(v) == 4 for v in candidates.values())
    cases = []
    for case_id, choices in candidates.items():
        choices.sort(key=lambda v: (v['cost'], METHOD_ORDER.index((v['model_key'], v['agent']))))
        selected = choices[0]
        same_inputs = [v['final']['input_sha256'] for v in choices]
        assert all(v == same_inputs[0] for v in same_inputs)
        reg, year = selected['region'], selected['year']
        best, final = selected['best'], selected['final']
        nodes_path = project / 'experiment' / reg / 'nodes.csv'
        pin(nodes_path)
        with nodes_path.open(newline='', encoding='utf-8-sig') as f:
            node_rows = list(csv.DictReader(f))
        nodes = {n['id']: {'id': n['id'], 'latitude': float(n['latitude']),
                           'longitude': float(n['longitude']),
                           'location_basis': n['location_basis'],
                           'existing_generation_MW': 0., 'new_generation_MW': 0.,
                           'new_technologies_MW': {}, 'new_battery_MW': 0.,
                           'new_battery_MWh': 0.} for n in node_rows}
        for asset in best['results']['assets']:
            node = nodes[asset['bus'].split('::')[-1]]
            tech = asset['technology']
            cap = float(asset.get('capacity_MW') or 0.)
            if tech in geo.GEN:
                node['existing_generation_MW' if asset['existing'] else 'new_generation_MW'] += cap
            if not asset['existing']:
                node['new_technologies_MW'][tech] = node['new_technologies_MW'].get(tech, 0.) + cap
                if tech == 'battery':
                    node['new_battery_MW'] += cap
                    node['new_battery_MWh'] += float(asset.get('energy_MWh') or 0.)
        totals = best['results']['new_capacities_GW']
        for tech in TECHS:
            assert math.isclose(sum(n['new_technologies_MW'].get(tech, 0) for n in nodes.values()) / 1000,
                                totals[tech], abs_tol=1e-5)
        assert math.isclose(sum(n['new_battery_MWh'] for n in nodes.values()) / 1000,
                            totals['battery_energy_GWh'], abs_tol=1e-5)
        # No hidden hydrogen or fuel-cell power may disappear from a four-colour legend.
        assert all(abs(totals.get(t, 0)) < 1e-8 for t in ['electrolyzer', 'fuel_cell', 'h2_storage_GWh'])
        corridors = best['results']['transmission']['corridors']
        for corridor in corridors:
            corridor['existing_rating_assumed'] = reg in {'miso', 'mexico', 'south_africa'}
            assert corridor['bus0'] in nodes and corridor['bus1'] in nodes
        gate = best['acceptance']['co2_budget']
        cases.append({'case_id': case_id, 'region': reg, 'year': year,
                      'job_id': selected['job_id'], 'model_key': selected['model_key'],
                      'agent': selected['agent'], 'source': selected['source'],
                      'final_sha256': sha(Path(selected['source']) / 'final.json'),
                      'best_accepted_sha256': sha(Path(selected['source']) / 'best_accepted.json'),
                      'candidate_sha256': best.get('dispatch_checkpoint', {}).get('candidate_sha256'),
                      'accepted': True, 'co2_accepted': True,
                      'annual_all_in_cost_billion': selected['cost'],
                      'co2_emitted_Mt': gate['emitted_Mt'], 'co2_cap_Mt': gate['annual_cap_Mt'],
                      'nodes': list(nodes.values()), 'new_capacities_GW': totals,
                      'corridors': corridors, 'operating_2023_plant_points': [],
                      'tied_minimum_job_ids': [x['job_id'] for x in choices
                                             if x['cost'] == selected['cost']],
                      'candidate_costs': [{'job_id': x['job_id'], 'all_in_cost_billion': x['cost']}
                                          for x in choices]})
    for source in geo.EV.glob('ne_*.zip'):
        pin(source)
    return {'schema_version': 1, 'selection': 'Lowest observed all-in annual cost among four accepted '
            'carbon-constrained trajectories for each region-year. Exact cost ties use the order '
            'Sol single, Sol multi, Opus single, Opus multi, and are explicitly disclosed. '
            'Selection is descriptive, not a global optimality '
            'or deployment-ranking claim.',
            'spatial_semantics': {'nodes': 'Prepared model-zone representative coordinates; additions '
                                  'are zonal aggregates, not proposed plant sites.',
                                  'corridors': 'Straight model connections, not conductor routes.',
                                  'basemap': 'Natural Earth 1:50m, local Lambert azimuthal equal-area.',
                                  'pie_area': '9 + total added generation and battery power in GW '
                                  'times a fixed factor; minimum visible area is retained.',
                                  'transfer': 'Selected new plus official-project capacity.',
                                  'emissions': 'Direct modelled generator CO2, excluding lifecycle '
                                  'and imported-power emissions; ceilings are research mappings.'},
            'cases': cases}


def save(fig, folder, stem):
    folder.mkdir(parents=True, exist_ok=True)
    for ext in ['pdf', 'svg']:
        fig.savefig(folder / (stem + '.' + ext))
    fig.savefig(folder / (stem + '.png'), dpi=260)


def legend(fig, pub, anchor, size=7, ncol=6):
    handles = [Line2D([], [], marker='o', ls='', mfc=pub.COLORS[t], mec='white',
                      ms=5, label=t.capitalize()) for t in TECHS]
    handles += [Line2D([], [], color=pub.INK, lw=.75, label='Existing link'),
                Line2D([], [], color=pub.TRANSFER, lw=1.7, label='Added transfer')]
    fig.legend(handles=handles, loc='upper left', bbox_to_anchor=anchor, ncol=ncol,
               frameon=False, fontsize=size, handlelength=1.2, columnspacing=1.4, borderaxespad=0)


def overview(data, out, geo, pub):
    fig = plt.figure(figsize=(180/25.4, 222/25.4))
    fig.text(.025, .985, 'Accepted portfolios under annual carbon ceilings',
             fontsize=10, weight='bold', va='top')
    fig.text(.025, .963, 'Lowest observed cost in each of ten region-year comparisons', fontsize=7.8)
    for index, case in enumerate(data['cases']):
        x, y = .023 + (index % 2) * .5, .929 - (index // 2) * .169
        label = f"{chr(97 + index)}  {REGION_NAMES[case['region']]} {case['year']}"
        fig.text(x, y, label, fontsize=8.3, weight='bold')
        ax = fig.add_axes([x, y-.132, .255, .118])
        trans = geo.basemap(ax, case['region'], scale_bar=False)
        coords = geo.coords(case, trans)
        pub.model_routes(ax, case, coords)
        for node in case['nodes']:
            pub.pie(ax, coords[node['id']], node)
        sx, right = x+.265, x+.461
        fig.text(sx, y-.027, 'Added power', fontsize=7, weight='bold')
        fig.text(right, y-.027, 'GW', fontsize=7, ha='right')
        for j, tech in enumerate(TECHS):
            yy = y-.047-j*.018
            fig.add_artist(Line2D([sx+.003], [yy+.0025], transform=fig.transFigure,
                                 marker='o', ls='', ms=3.4, mfc=pub.COLORS[tech], mec='none'))
            fig.text(sx+.019, yy, tech.capitalize(), fontsize=6.8)
            fig.text(right, yy, f"{case['new_capacities_GW'][tech]:.2f}",
                     fontsize=6.9, ha='right')
        fig.text(x, y-.149, f"{MODEL_NAMES[case['model_key']]} · {case['agent']}" +
                 (' · tied minimum' if len(case['tied_minimum_job_ids']) > 1 else ''), fontsize=6.4)
    legend(fig, pub, (.025, .069), ncol=3, size=7)
    fig.text(.025, .019, 'Zonal additions and schematic links; enlarged maps and ledgers in the atlas.', fontsize=6.8)
    save(fig, out, 'geographic_carbon_overview')
    plt.close(fig)


def atlas_page(case, geo, pub):
    fig = plt.figure(figsize=(180/25.4, 195/25.4))
    fig.text(.025, .98, f"{REGION_NAMES[case['region']]} {case['year']}", fontsize=11, weight='bold', va='top')
    fig.text(.975, .98, f"{MODEL_NAMES[case['model_key']]} · {case['agent']}", fontsize=8, ha='right', va='top')
    fig.text(.025, .947, f"Annual cost  US$ {case['annual_all_in_cost_billion']:.3f} billion", fontsize=8)
    fig.text(.975, .947, f"CO$_2$  {case['co2_emitted_Mt']:.2f} / {case['co2_cap_Mt']:.2f} Mt ceiling", fontsize=8, ha='right')
    legend(fig, pub, (.025, .923), size=6.7)
    ax = fig.add_axes([.09, .421, .82, .46])
    trans = geo.basemap(ax, case['region'])
    coords = geo.coords(case, trans)
    pub.model_routes(ax, case, coords, True)
    for node in case['nodes']:
        pub.pie(ax, coords[node['id']], node, True)
    pub.labelled_nodes(ax, case, coords, case['region'], True)
    xcols = [.027, .067, .30, .435, .57, .705, .84, .975]
    for x, head in zip(xcols, ['#', 'Zone', 'Existing', 'Wind', 'Solar', 'Nuclear', 'Battery', 'Energy']):
        fig.text(x, .392, head, fontsize=7.4, weight='bold', ha='left' if x<.1 else 'right')
    for x, unit in zip(xcols[2:], ['GW', 'GW', 'GW', 'GW', 'GW', 'GWh']):
        fig.text(x, .377, unit, fontsize=7, ha='right')
    fig.add_artist(Line2D([.025, .978], [.365, .365], transform=fig.transFigure, color=pub.INK, lw=.65))
    for j, node in enumerate(case['nodes']):
        y = .346-j*.022
        fig.text(xcols[0], y, str(j+1), fontsize=7.2, weight='bold')
        code = ('LRZ '+pub.short(case['region'], node['id'])) if case['region']=='miso' else node['id']
        fig.text(xcols[1], y, code, fontsize=7.2)
        values = [node['existing_generation_MW']/1000] + [node['new_technologies_MW'].get(t, 0)/1000 for t in TECHS] + [node['new_battery_MWh']/1000]
        for x, value in zip(xcols[2:], values):
            fig.text(x, y, pub.format_value(value), fontsize=7.2, ha='right')
    bottom = .346-len(case['nodes'])*.022
    fig.add_artist(Line2D([.025, .978], [bottom+.006, bottom+.006], transform=fig.transFigure, color=pub.INK, lw=.65))
    fig.text(.067, bottom-.014, 'Total', fontsize=7.4, weight='bold')
    totals = [sum(n['existing_generation_MW'] for n in case['nodes'])/1000] + [case['new_capacities_GW'][t] for t in TECHS] + [case['new_capacities_GW']['battery_energy_GWh']]
    for x, value in zip(xcols[2:], totals):
        fig.text(x, bottom-.014, pub.format_value(value), fontsize=7.4, weight='bold', ha='right')
    fig.text(.025, .059, f"Added transfer  {pub.transfer_total(case):.2f} GW", fontsize=7.4)
    tie = len(case['tied_minimum_job_ids'])
    selection = f'Representative of {tie} trajectories tied at the minimum recorded cost.' if tie > 1 else 'Lowest recorded cost among four accepted trajectories.'
    fig.text(.025, .040, selection, fontsize=7)
    fig.text(.025, .020, 'Existing is inherited generation. Locations and straight links retain their model-zone meaning.', fontsize=6.6)
    return fig


def tex_captions(data, out):
    lines = [r'% Insert after copying figure_carbon into the manuscript project.',
             r'\begin{figure}[p]', r'\centering',
             r'\includegraphics[height=0.70\textheight,keepaspectratio]{figure_carbon/geographic_carbon_overview.pdf}',
             r'\caption{\textbf{Lowest-cost accepted portfolios under annual carbon ceilings.} '
             r'Each panel shows one of ten region--year cases, selected by minimum observed all-in '
             r'annual cost among four physically accepted carbon-constrained trajectories. '
             r'GPT-6.1 Sol single-agent attains or ties the minimum in all ten cases and is '
             r'the displayed representative. Exact cost ties are indicated. Colours identify '
             r'added wind, solar, nuclear and battery power; pie area increases with the sum '
             r'of these additions, with a fixed minimum visible area. Hollow points indicate '
             r'no added power. Grey lines are existing model connections, with dashed lines '
             r'denoting assumed ratings. Magenta lines indicate selected new or official-project '
             r'transfer capacity. Locations are model-zone representatives and links are '
             r'schematic connections, rather than proposed plant sites or physical conductor '
             r'routes. The basemap is Natural Earth. All displayed candidates satisfy the '
             r'research annual CO$_2$ ceiling and the physical acceptance checks.}',
             r'\label{fig:carbon-geography}', r'\end{figure}', '']
    for case in data['cases']:
        stem = f"{case['region']}_{case['year']}"
        tie_count = len(case['tied_minimum_job_ids'])
        tie = (f' It is a representative of {tie_count} trajectories tied at the minimum recorded cost.'
               if tie_count > 1 else '')
        lines += [r'\begin{figure}[p]', r'\centering',
                  r'\includegraphics[height=0.74\textheight,keepaspectratio]{figure_carbon/regional_carbon_atlas/'+stem+'.pdf}',
                  r'\caption{\textbf{Accepted carbon-constrained additions for '+REGION_NAMES[case['region']]+' '+str(case['year'])+'.} '
                  'The displayed GPT-6.1 Sol single-agent portfolio achieves the minimum observed '
                  'all-in annual cost among four accepted trajectories.'+tie+
                  r' Numbered model locations link to the zonal ledger. Existing is inherited '
                  r'generation; Energy is added battery energy. The design costs USD '
                  f"{case['annual_all_in_cost_billion']:.3f}"+r' billion per year and emits '
                  f"{case['co2_emitted_Mt']:.3f}"+r'~MtCO$_2$ against a research ceiling of '
                  f"{case['co2_cap_Mt']:.3f}"+r'~MtCO$_2$. Encodings and spatial interpretation '
                  r'follow Fig.~\ref{fig:carbon-geography}. All representative feeder checks pass.}',
                  r'\label{fig:carbon-atlas-'+stem.replace('_','-')+'}', r'\end{figure}', '']
    (out/'geographic_carbon_captions.tex').write_text('\n'.join(lines))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    configure(args.project, args.out)
    geo, pub = drawing_modules(args.project)
    for source in [Path(__file__), Path(geo.__file__),
                   Path(pub.__file__)]:
        pin(source)
    args.out.mkdir(parents=True, exist_ok=True)
    data = extract(args.project, geo)
    dump(args.out/'geographic_carbon_evidence.json', data)
    tex_captions(data, args.out)
    overview(data, args.out, geo, pub)
    with PdfPages(args.out/'geographic_carbon_atlas.pdf', metadata={
            'Title': 'Lowest-cost accepted carbon-constrained planning portfolios'}) as pdf:
        for case in data['cases']:
            fig = atlas_page(case, geo, pub)
            pdf.savefig(fig)
            save(fig, args.out/'regional_carbon_atlas', f"{case['region']}_{case['year']}")
            plt.close(fig)
    files = [args.out/'geographic_carbon_captions.tex', args.out/'geographic_carbon_atlas.pdf', args.out/'geographic_carbon_overview.pdf',
             args.out/'geographic_carbon_overview.svg', args.out/'geographic_carbon_overview.png']
    files += sorted((args.out/'regional_carbon_atlas').glob('*'))
    dump(args.out/'geographic_carbon_audit.json', {
        'cases': len(data['cases']), 'source_files_sha256': SOURCE_FILES,
        'selection_policy': data['selection'], 'spatial_semantics': data['spatial_semantics'],
        'checks': ['All 40 trajectories completed and physically/carbon accepted',
                   'All final design/results/distribution exactly equal best_accepted',
                   'All-in costs equal system plus distribution costs',
                   'Matched comparison inputs have identical hashes',
                   'Zonal assets reconcile with reported aggregate capacities',
                   'External zone labels avoid capacity symbols'],
        'new_optimizer_calls': 0, 'new_api_calls': 0, 'new_power_flow_calls': 0,
        'output_sha256': {str(p.relative_to(args.out)): sha(p) for p in files}})
    print(json.dumps({'cases': len(data['cases']), 'outputs': len(files), 'out': str(args.out)}))


if __name__ == '__main__':
    main()
