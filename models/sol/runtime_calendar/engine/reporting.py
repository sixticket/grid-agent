"""Code-computed acceptance, separate from the language model's narrative."""
from pathlib import Path
import json

def project_years_pass(result, scenario):
 """Recheck built projects against fixed scenario inputs, not model narration."""
 tx=result.get('transmission') or {}
 rows=tx.get('projects',[])
 if not isinstance(rows,list) or any(not isinstance(row,dict) for row in rows):return False
 if any(row.get('built') and row.get('eligible_by_year') is False for row in rows):return False
 config=scenario.get('transmission') or {}
 year=config.get('planning_year',scenario.get('planning_year',scenario.get('year')))
 if year is None:return True
 if isinstance(year,bool) or not isinstance(year,int) or not 1<=year<=9999:return False
 projects={pr['id']:pr for pr in config.get('projects',[])}
 if not projects and not rows:return True
 if tx.get('planning_year')!=year:return False
 identifiers=[row.get('project') for row in rows]
 if any(not isinstance(identifier,str) for identifier in identifiers):return False
 if len(identifiers)!=len(set(identifiers)) or set(identifiers)!=set(projects):return False
 from transmission import project_eligibility
 for row in rows:
  project=projects[row['project']]
  eligible,_=project_eligibility(project,dict(config,planning_year=year))
  if not isinstance(row.get('built'),bool):return False
  if row.get('in_service_year')!=project.get('in_service_year') or row.get('eligible_by_year') is not eligible:return False
  if row['built'] and not eligible:return False
 return True

def assess(result,dist,scenario):
 result=result or {};dist=dist or {}
 threshold=float(sum(scenario['demand_MW']))/1000*scenario['assumptions']['unserved_fraction_limit']
 physics=result
 # Acceptance is physical validity only. Optimality is uncertifiable within any practical budget for the annual MIP and is reported as evidence, not required.
 checks=dict(solved=result.get('status') in ('optimal','feasible_time_limit'),reliable=result.get('unmet_demand_GWh',float('inf'))<=threshold,distribution=dist.get('passes') is True and dist.get('thermal_check_complete') is True,energy_budgets=all(x['passes'] for x in physics.get('energy_budget_checks',[])),storage_balance=physics.get('max_storage_balance_residual_MWh',float('inf'))<1e-3,nodal_balance=physics.get('max_nodal_balance_residual_MW',float('inf'))<1e-3,transmission_limits=max((result.get('transmission') or {}).get('max_interface_violation_MW',0) or 0,(result.get('transmission') or {}).get('max_zone_limit_violation_MW',0) or 0)<1e-3)
 checks['policy']=(result.get('policy_check',{}).get('passes') is True) if scenario.get('electricity_policy',{}).get('active') else True
 checks['project_years']=project_years_pass(result,scenario)
 evidence=dict(certified_optimal=result.get('status')=='optimal',mip_gap=result.get('mip_gap'),cost_billion=result.get('total_cost_billion'),cost_lower_bound_billion=result.get('cost_lower_bound_billion'))
 return dict(passes=all(checks.values()),checks=checks,evidence=evidence,unmet_limit_GWh=threshold,scope='Annual chronological model' if len(scenario['demand_MW'])==8760 else 'Short-window execution/physics validation only; not annual reliability evidence')

def write_report(out,final):
 out=Path(out);r=final.get('final_results') or {};d=final.get('final_distribution_check') or {};a=final.get('acceptance',{})
 lines=['# Experiment result','',f"Solver status: {r.get('status','not run')}. Code-computed acceptance: {a.get('passes','not assessed')}. ",a.get('scope',''),'','This is a regional planning counterfactual using explicit spatial/network assumptions. It is not parcel selection, a measured feeder study, AC transmission or N-1 certification.','',f"System annual-equivalent cost: {r.get('total_cost_billion')} billion USD; representative distribution upgrade cost: {d.get('distribution_capex_billion')} billion USD/year.",f"Unmet demand in simulated horizon: {r.get('unmet_demand_GWh')} GWh. Distribution pass: {d.get('passes')}.",'','## Failed checks','']
 lines += [f'- {k}' for k,v in a.get('checks',{}).items() if not v] or ['None reported.']
 tx=r.get('transmission') or {}
 if tx.get('planning_year') is not None:
  lines+=['',f"Official project availability uses planning year {tx['planning_year']}; later and unknown in-service dates are unavailable. This annual screen does not certify construction schedules or within-year availability."]
 policy=final.get('electricity_policy',{});target=policy.get('target')
 if target:
  pc=r.get('policy_check',{})
  lines+=['','## Electricity policy','',f"Mode: {policy.get('mode')}. Source target: {target['published_target']}.",f"Renewable fraction ({target['denominator']} basis): {pc.get('renewable_fraction')}; meets target: {pc.get('meets_target')}.",target['model_mapping'],f"Source: {target['source_url']}",'The target is applied to the archived input year; this is not a forecast or legal compliance certification.']
 e=a.get('evidence',{})
 lines+=['','## Solver evidence (not a pass/fail criterion)','',f"Certified optimal within tolerance: {e.get('certified_optimal')}. MIP gap: {e.get('mip_gap')}. Cost interval: lower bound {e.get('cost_lower_bound_billion')} to best found {e.get('cost_billion')} billion USD."]
 lines+=['','## Regional distribution evidence','','| Node | Pass | Lowest voltage | Highest voltage | Overload hours |','|---|---|---|---|---|']
 for k,v in d.get('nodes',{}).items():lines.append(f"| {k} | {v['passes']} | {v.get('worst_low_pu')} | {v.get('worst_high_pu')} | {v.get('thermal_overload_hours')} |")
 selection=final.get('final_selection')
 if selection:
  checkpoint=selection.get('dispatch_checkpoint',{})
  lines+=['','## Final design selection','',
          'Selection rule: lowest all-in cost among committed designs that passed every code-computed physical check. Agent search and stopping decisions are unchanged.',
          f"Selected source: {json.dumps(selection.get('source'),sort_keys=True)}. Selected result: {selection.get('selected')}.",
          f"Selected differs from last attempted design: {selection.get('selected_differs_from_last_attempt')}.",
          'The last attempted design and its independent acceptance result remain in `final.json.last_attempt`.',
          (f"Exact selected hourly dispatch: `{checkpoint['path']}` (checksum and metadata recorded in `final.json`)." if checkpoint.get('available') else
           'Exact selected hourly dispatch is unavailable: '+checkpoint.get('reason','not retained')),
          'Any rolling `dispatch_checkpoint.nc` or `dispatch/` files describe the last committed solve and must not be substituted for the selected checkpoint.']
 lines+=['','Full capacities, corridor choices, input assumptions and numerical checks: `final.json`. Agent runs also retain proposals, critique, counterfactual checks and prompt updates in `log.jsonl`. A model narrative never overrides the code-computed acceptance.','']
 (out/'report.md').write_text('\n'.join(lines))


def implementation_info():
    import hashlib,platform
    root=Path(__file__).resolve().parents[1]
    paths=sorted((root/'engine').glob('*.py'))+[root/'run.py',root/'requirements.lock.txt']
    digest=hashlib.sha256()
    for p in paths:
        digest.update(str(p.relative_to(root)).encode());digest.update(p.read_bytes())
    return dict(code_and_dependency_sha256=digest.hexdigest(),python=platform.python_version(),platform=platform.platform())
