"""Read archived plans and committed outcomes; no inference, optimization, or source edits."""
from pathlib import Path
import os, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import ArchiveResolver
_ARCHIVE = ArchiveResolver(Path(os.environ.get("GRID_AGENT_ARCHIVE_ROOT", "external-archive")), __import__("json").loads(os.environ.get("GRID_AGENT_ARCHIVE_MAP", "{}")))
_WORKSPACE = Path(os.environ.get("GRID_AGENT_MANUSCRIPT_ROOT", "manuscript-workspace")).resolve()
from collections import Counter
import argparse, hashlib, json
from openpyxl import load_workbook

HERE = _WORKSPACE
OUT = HERE / 'evidence/expert_comparison_20261007'

def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--project-root', type=Path, default=Path(os.environ.get("GRID_AGENT_ARCHIVE_ROOT", "external-archive")))
    args = parser.parse_args()
    root = args.project_root
    OUT.mkdir(parents=True, exist_ok=True)
    result_path = HERE / 'evidence/reported96_20261006/results.json'
    runs = json.loads(result_path.read_text())['runs']
    assert len(runs) == 96
    assert all(r['year'] == r['demand_year'] == r['weather_year'] and r['selected']['acceptance']['passes'] for r in runs)
    sources = {}
    def source(key, rel, url):
        p = root / rel
        sources[key] = dict(archive_path=rel, sha256=sha(p), url=url)
        return p
    au = load_workbook(source('aemo', 'prediction_documents/australia_nz/aemo_isp-chart-data_2026.xlsx', 'https://www.aemo.com.au/-/media/files/major-publications/isp/2026/supporting-materials/2026-isp-chart-data.xlsx'), read_only=True, data_only=True)
    br = load_workbook(source('epe', 'prediction_documents/brazil/epe_pde-2035-dados-relatorio-final_2026_xlsx/PDE 2035_Dados_Capítulo 03_Geração Centralizada de Energia Elétrica.xlsx', 'https://www.epe.gov.br/sites-pt/publicacoes-dados-abertos/publicacoes/Documents/PDE%202035_Dados_Relat%C3%B3rio%20Final.zip'), read_only=True, data_only=True)
    nz = load_workbook(source('mbie', 'prediction_documents/australia_nz/mbie_edgs-results_2024.xlsx', 'https://www.mbie.govt.nz/assets/Data-Files/Energy/electricity-demand-generation-scenarios-2024-results.xlsx'), read_only=True, data_only=True)
    source('irp', 'prediction_documents/south_africa/dmre_user-friendly-irp_2025.pdf', 'https://www.dee.gov.za/wp-content/uploads/2026/08/User-Friendly-IRP-2025.pdf')
    ei = load_workbook(source('eia', 'prediction_documents/us_national/eia_aeo2026-electric-power-projections-emm-01-texas-reliability-entity_2026.xlsx', 'https://www.eia.gov/outlooks/aeo/supplement/excel/suptab_54.1.xlsx'), read_only=True, data_only=True)
    rows = []
    def record(region, metric, key, reference, source_key, locator, components, category='capacities_GW', **notes):
        cohort = [r for r in runs if r['region'] == region and r['year'] == 2035]
        assert len(cohort) == 4
        vals = [r['selected'][category].get(key, 0) for r in cohort]
        lo, hi = min(vals), max(vals)
        rows.append(dict(region=region, metric=metric, reference_GW=reference,
            agent_min_GW=lo, agent_max_GW=hi, difference_min_GW=lo-reference,
            difference_max_GW=hi-reference, source=source_key, locator=locator,
            source_components=components, result_field=category+'.'+key,
            case_ids=[r['case_id'] for r in cohort], agent_values_GW=vals,
            comparison_type='descriptive external-plan benchmark; not a controlled human-AI test', **notes))
    a = au['Figure 1']
    assert a['AC34'].value == '2034-35'
    for metric, key, coords in [('Wind', 'wind', ['AC43','AC44']), ('Utility solar', 'solar', ['AC45']), ('Coal', 'coal', ['AC35','AC36'])]:
        c = {p:a[p].value for p in coords}
        record('australia', metric, key, sum(c.values())/1000, 'aemo', 'Figure 1!'+','.join(coords), c,
            horizon='financial year ending June 2035; agent calendar 2035', scenario='2026 ISP Step Change',
            scope='Onshore plus offshore wind; utility solar excludes rooftop and small-scale PV; coal includes black and brown coal. Chart source values are MW, converted to GW.',
            caveat='The agent retains the 2023 fleet; ISP includes retirements, staged investment and consumer resources. Wind categories are aggregated; siting and offshore costs are not harmonized.')
    b = br['Figura 3-25']
    assert str(b['A8'].value).startswith('2035-12')
    for metric,key,coord in [('Wind','wind','E8'),('Centralized solar','solar','F8'),('Battery power','battery','I8')]:
        record('brazil', metric, key, b[coord].value, 'epe', 'Figura 3-25!'+coord, {coord:b[coord].value},
            horizon='December 2035', scenario='PDE 2035 reference',
            excluded_distributed_capacity_GW=b['G8'].value,
            scope='Centralized capacity categories. MMGD (78.099 GW) is a separate expert-plan category, not included in the solar row.',
            caveat='Model solar is zonal and has no separate future MMGD rollout. The solar gap must not be interpreted as the gap in all national PV. Hydro expansion, thermal choices, costs and hydrology also differ.')
    # Visual transcription checked against PDF page 41 (printed page 31), Table 1.
    # The current base includes installed, under-construction and deemed-online 2025 assets.
    irp = {'wind':{'base_end_2025_MW':5344,'2026_2030_MW':7341,'2031_2035_MW':[2500,2600,2600,3000,3000]},
           'solar':{'base_end_2025_MW':3646,'2026_2030_MW':10313,'2031_2035_MW':[1000,1200,1400,1600,2000]}}
    for key,c in irp.items():
        total=(c['base_end_2025_MW']+c['2026_2030_MW']+sum(c['2031_2035_MW']))/1000
        record('south_africa', 'Wind' if key=='wind' else 'Solar PV',key,total,'irp','PDF page 41 / printed page 31, Table 1',c,
            horizon='end-2025 base plus scheduled additions through 2035',scenario='IRP 2025 Proposed Balanced Plan',
            excluded_distributed_capacity_GW=16.065,
            scope='Solar PV excludes the separately reported distributed-generation, CSP and hybrid columns. The plan does not list wind/PV retirements in this schedule.',
            caveat='Expert base is 2025, model base is 2023; future carbon policy, retirement treatment, technology options and build sequence are not harmonized.')
    n = list(nz['GEM build schedule'].values)
    assert n[0][:8] == ('Commissioning_Year','Value','Variable','Unit','Island','Technology_Type','Commodity','Scenario')
    for key,tech in [('wind','Wind'),('solar','Solar')]:
        selected=[(i+1,r) for i,r in enumerate(n) if i and r[7]=='Reference' and r[5]==tech and 2024<=r[0]<=2035]
        assert all(r[3]=='MW' for _,r in selected)
        record('new_zealand',tech+' additions',key,sum(r[1] for _,r in selected)/1000,'mbie',
            'GEM build schedule; Reference, '+tech+', commissioning years 2024-2035',
            [{'row':i,'year':r[0],'MW':r[1],'island':r[4]} for i,r in selected],category='new_capacities_GW',
            horizon='new capacity commissioned 2024-2035; agent additions to inherited 2023 fleet',scenario='EDGS 2024 Reference',
            caveat='Expert uses SADEM/GEM, including geothermal and hydro expansion; agent demand uses Transpower 2026 SOSA Medium Demand, with inherited geothermal/hydro but no new geothermal/hydro options.')
    e=ei[ei.sheetnames[0]]
    assert e['M10'].value==2035 and e['D4'].value=='cb2026'
    er=[r for r in runs if r['region']=='ercot' and r['year']==2035]
    ercot=dict(reference='AEO2026 Counterfactual Baseline; not the Reference case',source='eia',year=2035,
        planned_nuclear_additions_GW=e['M32'].value,unplanned_nuclear_additions_GW=e['M44'].value,
        nuclear_net_summer_capacity_GW=e['M19'].value,
        agent_new_nuclear_GW=[r['selected']['new_capacities_GW']['nuclear'] for r in er],
        agent_annual_demand_TWh=er[0]['annual_demand_TWh'],
        locator='cb2026.d021826b!M19,M32,M44',
        caveat='Different demand, baseline year and capacity rating. Zero reference nuclear additions and nonzero agent additions are a structural contrast; no percentage accuracy or exact total-capacity gap is computed.')
    repairs=Counter(r['region'] for r in runs if not r['initial']['acceptance']['passes'])
    assert sum(repairs.values())==28
    assert all([k for k,v in r['initial']['acceptance']['checks'].items() if not v]==['distribution'] for r in runs if not r['initial']['acceptance']['passes'])
    evidence=dict(version=1,created='2026-10-07',results_path=str(result_path.relative_to(HERE)),results_sha256=sha(result_path),
        policy='All four accepted 2035 trajectories per system; report observed min-max, not confidence intervals. No new experiment. No human superiority/accuracy inference.',
        sources=sources,comparisons=rows,ercot_structural_contrast=ercot,
        workflow=dict(runs=96,first_candidate_accepted=68,first_candidate_failed=28,all_selected_accepted=96,
            first_failure_checks={'distribution':28},repairs_by_region=dict(repairs),
            cases=[r['case_id'] for r in runs if not r['initial']['acceptance']['passes']]))
    (OUT/'comparison.json').write_text(json.dumps(evidence,indent=2,ensure_ascii=False)+'\n')
    names={'australia':'Australia (NEM)','brazil':'Brazil','south_africa':'South Africa','new_zealand':'New Zealand'}
    def fmt(lo,hi,signed=False):
        def f(x):return f'{x:+.2f}' if signed else f'{x:.2f}'
        return f(lo) if f(lo)==f(hi) else (r'\(['+f(lo)+','+f(hi)+r']\)' if signed else f(lo)+'--'+f(hi))
    lines=[]
    for r in rows:
        lines.append(f"{names[r['region']]} & {r['metric']} & {r['reference_GW']:.2f} & {fmt(r['agent_min_GW'],r['agent_max_GW'])} & {fmt(r['difference_min_GW'],r['difference_max_GW'],True)} \\\\")
    table=r'''\begin{table}[tbp]
\caption{Published expert plans and agent-assisted designs for 2035. Values are GW. Agent ranges include the selected accepted outputs from all four deployment--architecture trajectories per system. Differences are agent minus published plan under the respective modelling assumptions.}
\label{tab:expert-capacity}
\centering\small
\setlength{\tabcolsep}{5pt}
\begin{tabularx}{\textwidth}{@{}lXrrr@{}}
\toprule
System & Capacity measure & Expert plan & Agent range & Difference \\
\midrule
'''+ '\n'.join(lines)+r'''
\bottomrule
\end{tabularx}
\smallskip
\par\footnotesize The Australian reference is the AEMO 2026 ISP Step Change, financial year ending June 2035; wind combines onshore and offshore, solar excludes rooftop/small-scale PV. The Brazilian reference uses PDE 2035 December capacity; 78.099~GW of micro-/mini-distributed generation (MMGD) is excluded from the solar reference. The South African reference combines the IRP 2025 end-2025 base with scheduled 2026--2035 additions; the Solar PV column excludes 16.065~GW in the separate distributed-generation column, CSP and hybrid capacity. The New Zealand reference uses EDGS 2024 Reference additions commissioned in 2024--2035, compared with model additions to the inherited 2023 fleet. All other rows describe total capacity. Each study retains its own demand, retirement, policy, technology and cost assumptions. Source locators and category mappings are specified in Methods and Appendix~\ref{app:expert-plans}; document links are collected in Appendix~\ref{app:regional-sources}.
\end{table}
'''
    (HERE/'expert_comparison_table.tex').write_text(table)
    for w in [au,br,nz,ei]:w.close()
    print(json.dumps({'rows':len(rows),'initially_rejected':28,'source_files':len(sources),'ercot':ercot}))

if __name__=='__main__':
    main()
