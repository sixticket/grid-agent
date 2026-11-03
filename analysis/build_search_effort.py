"""Render search/token tables from frozen evidence; no experimental or API calls."""
from pathlib import Path
import json,statistics,hashlib,argparse
try:
 from .common import evidence_root,output_root
except ImportError:
 from common import evidence_root,output_root
HERE=Path(__file__).resolve().parent
E=HERE/'evidence/search_tokens_20261007'
MODELS=['gpt-6.1-sol','claude-opus-5-5']
NAMES={'gpt-6.1-sol':'GPT-6.1 Sol','claude-opus-5-5':'Opus 5.5'}
REGIONS=['miso','ercot','nyiso','south_africa','australia','new_zealand','mexico','brazil']
LABELS=dict(zip(REGIONS,['MISO','ERCOT','NYISO','South Africa','Australia','New Zealand','Mexico','Brazil']))
def main():
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('--evidence',type=Path,default=evidence_root()/'evidence/search_tokens_20261007/analysis.json')
 parser.add_argument('--out',type=Path,default=output_root())
 a=parser.parse_args();a.out.mkdir(parents=True,exist_ok=True)
 d=json.loads(a.evidence.read_text());cases=d['cases'];rs=d['responses'];assert len(cases)==96 and len(rs)==2257
 assert len({(r['model'],r['response_id']) for r in rs})==2257
 for c in cases:
  r=[a for a in rs if a['case_id']==c['case_id']]
  for k in ['input_tokens','output_tokens','reasoning_tokens','cache_read_tokens','cache_write_tokens','uncached_input_tokens']:assert sum(a[k] for a in r)==c[k]
  assert c['input_tokens']==sum(c[k] for k in ['uncached_input_tokens','cache_read_tokens','cache_write_tokens'])
 assert sum(c['input_tokens'] for c in cases)==276386026
 assert sum(c['output_tokens'] for c in cases)==3834189
 assert sum(c['evaluation_count'] for c in cases)==382
 assert sum(c['failed_evaluations'] for c in cases)==75
 assert sum(c['selected_evaluation']==1 for c in cases)==56
 assert sum(c['cheaper_failed_events'] for c in cases)==36
 rows=[]
 for model in MODELS:
  for arch in ['single','multi']:
   cc=[c for c in cases if c['model']==model and c['architecture']==arch];assert len(cc)==24
   inputs=[c['input_tokens']/1e6 for c in cc];outputs=[c['output_tokens']/1e3 for c in cc]
   rows.append([NAMES[model],arch.title(),'24',*[f'{f(v):,.3f}' for v in [inputs,outputs] for f in [sum,statistics.mean,statistics.median]]])
 caption=r'\textbf{Input and output tokens across all 96 planning trajectories.} Input columns use millions of tokens; output columns use thousands. Means and medians are per trajectory. Counts cover complete search histories, including repeated context and inherited responses counted once. Input includes cache reads and writes; output includes reasoning. The archived evidence retains integer token counts for every response.'
 text=[r'\begin{tableorg}[tbp]',r'\caption{'+caption+'}',r'\label{tab:search-token-usage}',r'\centering\small',r'\makebox[\textwidth][c]{\begin{tabular*}{170mm}{@{\extracolsep{\fill}}llrrrrrrr@{}}',r'\toprule',r'Deployment & Architecture & $n$ & \multicolumn{3}{c}{Input tokens (million)} & \multicolumn{3}{c}{Output tokens (thousand)} \\',r'\cmidrule(lr){4-6}\cmidrule(lr){7-9}',r' & & & Total & Mean & Median & Total & Mean & Median \\',r'\midrule']
 text+=[' & '.join(x)+r'\\' for x in rows];text += [r'\bottomrule',r'\end{tabular*}}',r'\end{tableorg}','']
 (a.out/'token_usage_table.tex').write_text('\n'.join(text))
 text=[r'Tables~\ref{tab:search-sol} and~\ref{tab:search-opus} retain every trajectory. Each paired entry is single/multi for the same system and year. Assessments include feeder-only rechecks; their selected index identifies the first occurrence of the retained candidate. Failed assessments fail the recorded distribution check. Probes are uncommitted system-only verifications without feeder acceptance. Repeated designs remain separate assessments. Input tokens include cache reads and writes, and output includes the reasoning subset. Full-precision counts, native round/turn indices, role attribution and response provenance are supplied in the frozen evidence archive.','']
 idx={(c['model'],c['region'],c['year'],c['architecture']):c for c in cases}
 for model in MODELS:
  text += [r'\begin{tableorg}[htbp]',r'\caption{\textbf{Assessment histories and tokens for all 48 '+NAMES[model]+r' trajectories.} Entries are single/multi. Input is in millions of tokens and output in thousands; displayed values are rounded. Selected is the source assessment index, not a native turn or cycle.}',r'\label{tab:search-'+('sol' if model==MODELS[0] else 'opus')+'}',r'\centering\small',r'\makebox[\textwidth][c]{\begin{tabular*}{170mm}{@{\extracolsep{\fill}}lrrrrrrr@{}}',r'\toprule',r'System & Year & Assessments & Selected & Failed & Probes & Input (M) & Output (k) \\',r'\midrule']
  for region in REGIONS:
   for year in [2023,2030,2035]:
    pair=[idx[model,region,year,a] for a in ['single','multi']]
    cells=[LABELS[region] if year==2023 else '',str(year)]
    cells+=['/'.join(str(c[k]) for c in pair) for k in ['evaluation_count','selected_evaluation','failed_evaluations','verification_calls']]
    cells+=['/'.join(f'{c[k]/factor:.3f}' for c in pair) for k,factor in [('input_tokens',1e6),('output_tokens',1e3)]]
    text.append(' & '.join(cells)+r'\\')
   if region!=REGIONS[-1]:text.append(r'\addlinespace[3pt]')
  text += [r'\bottomrule',r'\end{tabular*}}',r'\end{tableorg}','']
 (a.out/'search_effort_appendix.tex').write_text('\n'.join(text))
 receipt={'status':'passed','trajectories':len(cases),'responses':len(rs),'input_tokens':sum(c['input_tokens'] for c in cases),'output_tokens':sum(c['output_tokens'] for c in cases),'assessments':sum(c['evaluation_count'] for c in cases),'new_tables':3,'data_sha256':hashlib.sha256(a.evidence.read_bytes()).hexdigest(),'builder_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
 (a.out/'search_table_validation.json').write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt))
if __name__=='__main__':main()
