"""Build manuscript figures from archived completed evidence; no model/solver calls.

Run with Python, matplotlib and numpy. The explicit completed-case snapshot is
evidence/reported96_20261006/base2023_results.json. Missing trajectories are never imputed.
"""
from pathlib import Path
import os, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import ArchiveResolver
_ARCHIVE = ArchiveResolver(Path(os.environ.get("GRID_AGENT_ARCHIVE_ROOT", "external-archive")), __import__("json").loads(os.environ.get("GRID_AGENT_ARCHIVE_MAP", "{}")))
_WORKSPACE = Path(os.environ.get("GRID_AGENT_MANUSCRIPT_ROOT", "manuscript-workspace")).resolve()
import hashlib
import json
import math
from collections import Counter

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
from matplotlib.lines import Line2D
from matplotlib.ticker import MaxNLocator, ScalarFormatter
import numpy as np


HERE = _WORKSPACE
SOURCE = HERE / 'evidence/reported96_20261006/base2023_results.json'
DATA = None
RUNS = {}
OUT = HERE / 'figures'
plt.rcParams.update({
    'font.family': 'Arial', 'font.size': 8,
    'axes.spines.top': False, 'axes.spines.right': False,
    'axes.linewidth': .7, 'axes.labelsize': 8, 'xtick.labelsize': 7,
    'ytick.labelsize': 7, 'axes.titleweight': 'bold',
    'pdf.fonttype': 42, 'ps.fonttype': 42, 'savefig.facecolor': 'white',
    'text.color': '#000000', 'axes.labelcolor': '#000000', 'axes.titlecolor': '#000000',
    'xtick.color': '#000000', 'ytick.color': '#000000',
})
NAVY, TEAL, GREY = '#254B70', '#198576', '#525B63'
ORANGE = '#BB6525'


def framework():
    """Figure 1 is authored by build_framework_ppt.mjs; this builder preserves it."""
    return None


REGIONS = ['miso', 'ercot', 'nyiso', 'south_africa', 'australia',
           'new_zealand', 'mexico', 'brazil']
LABELS = ['MISO', 'ERCOT', 'NYISO', 'South Africa', 'Australia',
          'New Zealand', 'Mexico', 'Brazil']
SHORT_LABELS = ['MISO', 'ERCOT', 'NYISO', 'ZA', 'AU', 'NZ', 'MX', 'BR']
DEPLOYMENTS = [('sol_6_1', 'Sol 6.1', NAVY), ('claude_opus', 'Opus', ORANGE)]
SERIES = [(deployment, agent, label + ' ' + agent, colour)
          for deployment, label, colour in DEPLOYMENTS for agent in ('single', 'multi')]
BOUND_SCOPE = ('Intervals are solver lower-to-selected cost bounds conditional on each selected design, '
               'including its fixed representative-distribution cost.')
REQUEST_SCOPE = ('Committed simulation requests plus uncommitted verification requests; exact-result '
                 'cache reuse can satisfy a request without a new solver execution.')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def requests(record):
    values = [record['metrics']['sim_runs'], record['metrics'].get('verification_sims', 0)]
    assert all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in values)
    assert sum(values) > 0
    return sum(values)


def cost(record):
    selected = record['selected']
    low, high = selected['all_in_cost_lower_bound_billion'], selected['all_in_cost_billion']
    assert all(isinstance(value, (int, float)) and math.isfinite(value) for value in (low, high))
    assert low <= high + 1e-8 and high > 0
    # Directly reconstruct both endpoints independently from their components.
    assert math.isclose(high, selected['system_cost_billion'] + selected['distribution_cost_billion'], abs_tol=1e-8)
    assert math.isclose(low, selected['system_cost_lower_bound_billion'] + selected['distribution_cost_billion'], abs_tol=1e-8)
    return low, high


def validate_data(data):
    global DATA, RUNS
    DATA = data
    RUNS = {(row['deployment'], row['year'], row['region'], row['agent']): row for row in data['runs']}
    assert len(RUNS) == len(data['runs']), 'Duplicate model/year/region/architecture keys'
    expected = {(model, 2023, region, agent) for model in ('sol_6_1', 'claude_opus') for region in REGIONS for agent in ('single', 'multi')}
    assert set(RUNS) == expected and len(RUNS) == 32
    for row in RUNS.values():
        assert row['completed'] is True and row['selected']['acceptance']['passes'] is True
        assert row['selected']['hours_modelled'] == 8760
        assert row['demand_year'] == row['weather_year'] == 2023
        requests(row)
        cost(row)
    cohorts = {}
    for model, year in sorted({(key[0], key[1]) for key in RUNS}):
        rows = [row for key, row in RUNS.items() if key[:2] == (model, year)]
        computed = dict(completed=len(rows), committed_simulations=sum(row['metrics']['sim_runs'] for row in rows),
                        verification_simulations=sum(row['metrics'].get('verification_sims', 0) for row in rows))
        summary = data['summary'][f'{model}_{year}']
        for field, value in computed.items():
            assert summary[field] == value, f'Independent summary mismatch: {model}/{year}/{field}'
        computed['system_evaluation_requests'] = sum(requests(row) for row in rows)
        cohorts[f'{model}_{year}'] = computed
    pair_checks = 0
    return dict(cohorts=cohorts, completed_case_count=len(RUNS), accepted_case_count=len(RUNS),
                base_year_cases=sum(key[1] == 2023 for key in RUNS),
                future_cases=sum(key[1] != 2023 for key in RUNS),
                future_matched_opus_sol_cells=sum(key[0] == 'claude_opus' and key[1] != 2023 for key in RUNS),
                prior_sol_opus_pair_checks=pair_checks,
                expected_missing=[])


def fingerprint(figure, caption):
    return dict(figure=figure, source=str(SOURCE.relative_to(HERE)), source_sha256=sha(SOURCE),
                builder_sha256=sha(Path(__file__)), extraction_cutoff_utc=DATA.get('extraction_cutoff_utc'),
                snapshot_revision=DATA.get('snapshot_revision'), request_definition=REQUEST_SCOPE,
                year_field_meaning='demand year', weather_dataset='ERA5', weather_year=2023,
                bound_scope=BOUND_SCOPE, caption=caption)


def save_figure(fig, stem, audit):
    fig.savefig(OUT / (stem + '.pdf'), metadata={'Creator': 'Audited completed-case figure builder'})
    fig.savefig(OUT / (stem + '.png'), dpi=200)
    plt.close(fig)
    audit['typography_policy'] = 'Black text; white heatmap numbers only on dark cells for contrast'
    audit['rendered_sha256'] = {suffix: sha(OUT / (stem + '.' + suffix)) for suffix in ('pdf', 'png')}
    (OUT / (stem + '_audit.json')).write_text(json.dumps(audit, indent=2, allow_nan=False) + '\n')


def heatmap(ax, year, series, vmax, *, short=False, show_rows=True):
    values = np.full((len(series), len(REGIONS)), np.nan)
    cells = []
    for row, (model, agent, _label, _colour) in enumerate(series):
        for col, region in enumerate(REGIONS):
            record = RUNS.get((model, year, region, agent))
            value = requests(record) if record else None
            if value is not None:
                values[row, col] = value
            cells.append(dict(deployment=model, year=year, region=region, agent=agent,
                              completed=record is not None, requests=value))
    cmap = plt.get_cmap('Blues').copy()
    cmap.set_bad('#ECEEEF')
    im = ax.imshow(np.ma.masked_invalid(values), cmap=cmap, vmin=0, vmax=vmax, aspect='auto', interpolation='nearest')
    for row in range(len(series)):
        for col in range(len(REGIONS)):
            value = values[row, col]
            ax.text(col, row, 'n/c' if np.isnan(value) else str(int(value)), ha='center', va='center',
                    color='white' if np.isfinite(value) and value > vmax * .56 else '#000000',
                    fontsize=7.6 if not short else 7.3)
    ax.set_xticks(range(len(REGIONS)), SHORT_LABELS if short else LABELS)
    ax.set_yticks(range(len(series)), [x[2] for x in series] if show_rows else [])
    ax.tick_params(axis='both', length=0, pad=5)
    if not short:
        ax.set_xticklabels(LABELS, rotation=22, ha='right', rotation_mode='anchor')
    else:
        ax.set_xticklabels(SHORT_LABELS, rotation=30, ha='right', rotation_mode='anchor', fontsize=6.8)
    ax.set_xticks(np.arange(-.5, len(REGIONS), 1), minor=True)
    ax.set_yticks(np.arange(-.5, len(series), 1), minor=True)
    ax.grid(which='minor', color='white', linewidth=1.1)
    ax.tick_params(which='minor', length=0)
    for edge in ax.spines.values():
        edge.set_visible(False)
    for label, series_row in zip(ax.get_yticklabels(), series):
        label.set_color('#000000')
    return im, cells


FIG2_CAPTION = ('GPT-6.1 Sol and Claude Opus 5.5 with 2023 demand and ERA5 2023 weather. '
    'a, System-evaluation requests for each model, architecture and region across 32 trajectories. Cell numbers are exact; colour '
    'uses one linear scale. Requests comprise committed simulations and uncommitted verifications, '
    'including exact-result cache reuse. b-d, Selected all-in annual costs in NYISO, South Africa and '
    'Mexico on magnified, panel-specific scales. Filled circles denote single-agent and open squares '
    'multi-agent trajectories; colour denotes deployment. Points show selected costs, including '
    'annualized representative-distribution interventions. Horizontal segments extend from the solver '
    'lower bound plus the selected distribution cost to the selected total. Bounds condition on each selected design.')


def model_comparison():
    audit = fingerprint('fig2_model_comparison', FIG2_CAPTION)
    audit.update(cells=[], cost_panels=[])
    fig = plt.figure(figsize=(6.7, 6.3))
    ax = fig.add_axes([.15, .575, .78, .33])
    vmax = max(requests(row) for key, row in RUNS.items() if key[1] == 2023)
    im, cells = heatmap(ax, 2023, SERIES, vmax)
    ax.set_title('a   System-evaluation requests', loc='left', fontsize=9.2, pad=13)
    # Demand and weather cohort are specified in the caption.
    cax = fig.add_axes([.95, .575, .018, .33])
    bar = fig.colorbar(im, cax=cax)
    bar.ax.tick_params(labelsize=7, length=2)
    bar.ax.yaxis.set_major_locator(MaxNLocator(nbins=5, integer=True))
    audit['cells'] = cells
    audit['request_colour_limits'] = [0, vmax]
    for index, (region, label) in enumerate((('nyiso', 'NYISO'), ('south_africa', 'South Africa'), ('mexico', 'Mexico'))):
        ax = fig.add_axes([.15 + index * .283, .105, .245, .30])
        rows = []
        for row, (model, agent, label_series, colour) in enumerate(SERIES):
            low, high = cost(RUNS[model, 2023, region, agent])
            y = len(SERIES) - 1 - row
            ax.plot([low, high], [y, y], color=colour, linewidth=1.7, solid_capstyle='butt')
            if high - low > 1e-10:
                ax.plot(low, y, '|', color=colour, markersize=6)
            ax.plot(high, y, marker='o' if agent == 'single' else 's', markersize=4.6,
                    markerfacecolor=colour if agent == 'single' else 'white', markeredgecolor=colour, markeredgewidth=1)
            rows.append(dict(deployment=model, agent=agent, lower_billion=low, selected_billion=high))
        ax.set_ylim(-.6, len(SERIES)-.4)
        ax.set_yticks(range(len(SERIES)-1, -1, -1), [row[2] for row in SERIES] if index == 0 else [])
        ax.tick_params(axis='y', length=0, pad=5)
        ax.tick_params(axis='x', labelsize=6.5, pad=4)
        ax.spines['left'].set_visible(False)
        ax.xaxis.set_major_locator(MaxNLocator(nbins=3, min_n_ticks=2))
        formatter = ScalarFormatter(useOffset=False)
        formatter.set_scientific(False)
        ax.xaxis.set_major_formatter(formatter)
        ax.grid(axis='x', color='#E4E8EB', linewidth=.6)
        low, high = min(x['lower_billion'] for x in rows), max(x['selected_billion'] for x in rows)
        margin = max((high - low) * .15, high * 1e-8)
        ax.set_xlim(low - margin, high + margin)
        ax.set_title(f'{chr(98 + index)}   {label}', loc='left', fontsize=9, pad=12)
        audit['cost_panels'].append(dict(region=region, rows=rows, x_limits=list(ax.get_xlim())))
    fig.text(.57, .025, 'Annual cost (USD billion)', ha='center', fontsize=8)
    save_figure(fig, 'fig2_model_comparison', audit)
    return audit


def main():
    checks = validate_data(json.loads(SOURCE.read_text()))
    OUT.mkdir(exist_ok=True)
    base = model_comparison()
    assert sum(cell['completed'] for cell in base['cells']) == 32
    checks.update(source_sha256=sha(SOURCE), builder_sha256=sha(Path(__file__)), figure2_cases=32, caption2=FIG2_CAPTION)
    (OUT / 'figure_values.json').write_text(json.dumps(checks, indent=2, allow_nan=False) + '\n')
    print(json.dumps({'completed_case_count':32,'base_year_cases':32,'figure2_cases':32}))


if __name__ == '__main__':
    main()
