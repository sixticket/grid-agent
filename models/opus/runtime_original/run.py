#!/usr/bin/env python3
"""One entry point for the independent four-region experiment."""
from pathlib import Path
import argparse,os,sys,json,datetime,subprocess
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'engine'))
REGIONS=('miso','ercot','nyiso','south_africa','australia','new_zealand','mexico','brazil')

def main():
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('--region',choices=REGIONS,required=True);p.add_argument('--mode',choices=('greenfield','brownfield'),required=True)
 p.add_argument('--policy',choices=('baseline','published'),default='baseline',help='fixed electricity target; published is available for australia and nyiso')
 p.add_argument('--agent',choices=('none','single','multi'),default='none');p.add_argument('--hours',type=int,default=8760);p.add_argument('--start-hour',type=int,default=0)
 p.add_argument('--provider',choices=('openai','anthropic'),default='openai');p.add_argument('--model');p.add_argument('--max-cycles',type=int,default=6);p.add_argument('--max-runs',type=int,default=12);p.add_argument('--max-turns',type=int,default=40)
 p.add_argument('--solver-seconds',type=float,default=86400,help='safety ceiling per solve (zombie guard), not a budget; solves run to the 0.1%% gap tolerance');p.add_argument('--solver',choices=('auto','gurobi','highs'),default='auto',help='auto = Gurobi if installed and licensed, else HiGHS');p.add_argument('--solver-threads',type=int,default=18);p.add_argument('--design',type=Path,help='JSON object of design overrides for deterministic run');p.add_argument('--network',type=Path,help='alternative network JSON (e.g. network_fullmesh.json) for ablations; default <region>/network.json');p.add_argument('--output',type=Path)
 a=p.parse_args();os.environ.update(GRID_EXPERIMENT_HOURS=str(a.hours),GRID_EXPERIMENT_START=str(a.start_hour),GRID_SOLVER_TIME_LIMIT=str(a.solver_seconds),GRID_SOLVER=a.solver,GRID_SOLVER_THREADS=str(a.solver_threads))
 from policy import load_policy
 try:electricity_policy=load_policy(a.region,a.policy)
 except ValueError as exc:p.error(str(exc))
 stamp=datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f');out=a.output.resolve() if a.output else ROOT/a.region/a.mode/'runs'/stamp
 out.mkdir(parents=True,exist_ok=True);os.environ['GRID_SOLVER_LOGDIR']=str(out)
 if a.agent!='none':
  if a.design:p.error('--design is for deterministic runs; agent runs start from common defaults')
  key='OPENAI_API_KEY' if a.provider=='openai' else 'ANTHROPIC_API_KEY'
  if not os.environ.get(key):p.error(f'Set {key} in the environment; no key is stored in this directory')
  cmd=[sys.executable,str(ROOT/'engine'/f'{a.agent}_agent.py'),'--scenario',a.region,'--planning-mode',a.mode,'--provider',a.provider,'--policy',a.policy]
  if a.model:cmd+=['--model',a.model]
  if a.network:cmd+=['--network',str(a.network.resolve())]
  cmd+=['--max-cycles',str(a.max_cycles)] if a.agent=='multi' else ['--max-runs',str(a.max_runs),'--max-turns',str(a.max_turns)]
  os.environ['GRID_RUN_OUTPUT']=str(out)
  code=subprocess.call(cmd,cwd=out)
  for finalpath in out.glob('final.json'):
   from reporting import write_report
   final=json.loads(finalpath.read_text());write_report(finalpath.parent,final)
  return code
 from scenario_core import load_pack
 from planning import with_planning_mode
 from reporting import assess,write_report,implementation_info
 base=load_pack(a.region,policy=a.policy);sim=with_planning_mode(base,a.mode,base.DATA_DIR/'inventory.json' if a.mode=='brownfield' else None,a.network or base.DATA_DIR/'network.json')
 design=dict(sim.DEFAULT_DESIGN)
 if a.design:design.update(json.loads(a.design.read_text()))
 errors=sim.validate_changes(design)
 if errors:p.error('; '.join(errors))
 print(f"[run] {a.region} {a.mode} {a.hours} h -> {out}",file=sys.stderr,flush=True)
 result=sim.run_simulation(design)
 if result.get('status') in ('optimal','feasible_time_limit'):print(f"[run] cost {result['total_cost_billion']:.3f} (lower bound {result['cost_lower_bound_billion']:.3f}) B$/yr, gap {result['mip_gap']}, unmet {result['unmet_demand_GWh']:.3f} GWh; distribution check...",file=sys.stderr,flush=True)
 dist=sim.run_distribution_check(design) if result.get('status') in ('optimal','feasible_time_limit') else None
 if dist:print(f"[run] distribution passes={dist['passes']} under={dist['undervoltage_hours']} over={dist['overvoltage_hours']}",file=sys.stderr,flush=True)
 final=dict(region=a.region,mode=a.mode,agent='none',electricity_policy=electricity_policy,network=str(a.network or 'network.json'),scenario_hash=sim.SCENARIO_HASH,hours=a.hours,start_hour=a.start_hour,final_design=design,final_results=result,final_distribution_check=dist)
 final['acceptance']=assess(result,dist,sim.SCENARIO)
 final['implementation']=implementation_info()
 (out/'final.json').write_text(json.dumps(final,indent=2,allow_nan=False))
 if result.get('status') in ('optimal','feasible_time_limit'):sim.export_solution(out/'dispatch')
 write_report(out,final)
 print(json.dumps(dict(output=str(out),solver=result.get('status'),acceptance=final['acceptance']),indent=2))
 return 0 if result.get('status') in ('optimal','feasible_time_limit') else 2
if __name__=='__main__':raise SystemExit(main())
