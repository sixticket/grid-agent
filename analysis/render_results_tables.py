#!/usr/bin/env python3
"""Render manuscript results from the frozen, audited JSON evidence."""
import argparse
import json
from pathlib import Path
try:
    from .common import evidence_root, output_root, REPORTED_SNAPSHOT
except ImportError:
    from common import evidence_root, output_root, REPORTED_SNAPSHOT

REGIONS=['miso','ercot','nyiso','south_africa','australia','new_zealand','mexico','brazil']
LABELS=dict(miso='MISO',ercot='ERCOT',nyiso='NYISO',south_africa='South Africa',australia='Australia',new_zealand='New Zealand',mexico='Mexico',brazil='Brazil')

def table(rows, caption, label, headers):
    columns='l'+'r'*(len(headers)-1)
    start=r'\begin{tabular}{@{}'+columns+'@{}}'
    end=r'\end{tabular}'
    if label=='tab:future-demand-inputs':
        start=r'\begingroup\setlength{\tabcolsep}{3pt}'+'\n'+start
        end+='\n'+r'\endgroup'
    if label=='tab:sol-cycles':
        start=r'\begin{tabular*}{0.8\textwidth}{@{\extracolsep{\fill}}'+columns+'@{}}'
        end=r'\end{tabular*}'
    return '\n'.join([r'\begin{table}[tbp]',r'\caption{'+caption+'}',r'\label{'+label+'}',r'\centering\small',start,r'\toprule',' & '.join(headers)+r'\\',r'\midrule',*[' & '.join(row)+r'\\' for row in rows],r'\bottomrule',end,r'\end{table}',''])

def main():
    p=argparse.ArgumentParser();p.add_argument('--evidence',type=Path,default=evidence_root()/REPORTED_SNAPSHOT.parent/'base2023_results.json');p.add_argument('--out',type=Path,default=output_root());a=p.parse_args()
    d=json.loads(a.evidence.read_text())
    assert len(d['runs'])==32 and {r['model'] for r in d['runs']}=={'gpt-6.1-sol','claude-opus-5-5'}
    a.out.mkdir(parents=True,exist_ok=True)
    render_opus(d,a.out)
    print('Rendered the sixteen included Opus 2023 outcomes.')

def render_opus(data, out):
    """Report every Opus cell completed at the frozen extraction cutoff."""
    runs=[r for r in data['runs'] if r['deployment']=='claude_opus']
    assert len(runs) == 16 and all(r['completed'] and r['selected']['acceptance']['passes'] for r in runs)
    indexed={(r['year'],r['region'],r['agent']):r for r in runs}
    def rows(year):
        result=[]
        for region in REGIONS:
            cells=[indexed.get((year,region,agent)) for agent in ('single','multi')]
            if not any(cells):
                continue
            def joined(fn):
                return '/'.join(fn(c) if c is not None else r'\textemdash' for c in cells)
            result.append([LABELS[region],joined(lambda c:f"{c['selected']['all_in_cost_billion']:.6f}"),
                joined(lambda c:str(c['metrics']['sim_runs']+c['metrics'].get('verification_sims',0))),
                joined(lambda c:str(c['metrics']['api_calls']))])
        return result
    def totals(year):
        s=data['summary']['claude_opus_'+str(year)]
        return (f"The {year}-demand cohort records {s['committed_simulations']} committed evaluations, "
                f"{s['verification_simulations']} verification requests and {s['model_requests']:,} received model responses. ")
    caption=('Claude Opus 5.5 planning with 2023 demand and ERA5 2023 weather. Entries are single/multi. '
             'Selected costs are billion USD per year, including representative-distribution interventions. '
             'System evaluations count committed and uncommitted verification requests, including exact-result reuse; '
             'model requests count unique received responses in the selected trajectory ledger. '
             '')
    base=r"""\subsection{Opus planning with 2023 demand and ERA5 2023 weather}\label{sec:opus-base}
Claude Opus 5.5 runs sixteen trajectories with 2023 demand and ERA5 2023 weather, with both architectures in each of the eight systems (Table~\ref{tab:opus-2023}). Every selected candidate passes the same code-computed system and representative-feeder conditions. Selection retains the lowest all-in annual cost among physically accepted committed candidates. Together with the sixteen GPT-6.1 Sol 2023 controls, these results give thirty-two baseline trajectories (Fig.~\ref{fig:model-comparison}).

"""+table(rows(2023),caption,'tab:opus-2023',['System','Selected cost','System evaluations','Model requests'])+'\n'
    base+=totals(2023)
    base+=r"""Solver cost intervals condition on each selected design. Each cell contains one trajectory. Model protocols, search ceilings and implementations are listed in Appendix~\ref{app:deployments}.
"""
    (out/'opus_base_results.tex').write_text(base)


if __name__=='__main__':main()
