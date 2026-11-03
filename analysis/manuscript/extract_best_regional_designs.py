"""Extract lowest-cost accepted 2035 portfolios from immutable completed runs.
Reads final results only; never imports or calls the experiment engine.
"""
from pathlib import Path
import os, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import ArchiveResolver
_ARCHIVE = ArchiveResolver(Path(os.environ.get("GRID_AGENT_ARCHIVE_ROOT", "external-archive")), __import__("json").loads(os.environ.get("GRID_AGENT_ARCHIVE_MAP", "{}")))
_WORKSPACE = Path(os.environ.get("GRID_AGENT_MANUSCRIPT_ROOT", "manuscript-workspace")).resolve()
from datetime import datetime, timezone
import copy, hashlib, json, math

HERE=_WORKSPACE
EV=HERE/'evidence/best_regional_20261006'
REGIONS=['miso','ercot','nyiso','south_africa','australia','new_zealand','mexico','brazil']
GEN={'wind','solar','nuclear','coal','gas','oil','hydro','other','biomass','waste','csp','geothermal','fuel_cell'}

def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def read(p):return json.loads(Path(p).read_text())
def main():
    source=HERE/'evidence/completed_weather_20261003/results.json'
    spatial=HERE/'evidence/geographic_20261003/planning_maps.json'
    original=read(spatial); runs=read(source)['runs'];cases={};selection=[]
    pins={str(source):sha(source),str(spatial):sha(spatial)}
    for reg in REGIONS:
        options=[r for r in runs if r['region']==reg and r['demand_year']==r['weather_year']==2035]
        assert len(options)==4 and len({json.dumps(r['input_sha256'],sort_keys=True) for r in options})==1
        assert all(r['completed'] and r['selected']['acceptance']['passes'] for r in options)
        # Equal reported costs are ties, with a stable case-id representative, never a winner claim.
        options.sort(key=lambda r:(r['selected']['all_in_cost_billion'],r['case_id']))
        best=options[0]; minimum=best['selected']['all_in_cost_billion']
        ties=[r['case_id'] for r in options if math.isclose(r['selected']['all_in_cost_billion'],minimum,rel_tol=0,abs_tol=1e-9)]
        fpath=_ARCHIVE(best['source_output'])/'final.json';assert sha(fpath)==best['final_sha256'];pins[str(fpath)]=sha(fpath)
        f=read(fpath);fr=f['final_results'];dist=f['final_distribution_check']
        assert f['acceptance']['passes'] and dist['passes']
        assert math.isclose(fr['total_cost_billion']+dist['distribution_capex_billion'],minimum,abs_tol=2e-8)
        c=copy.deepcopy(original['cases'][reg]);c.pop('feeder_case',None)
        nodes={n['id']:n for n in c['nodes']}
        inherited={k:v['existing_generation_MW'] for k,v in nodes.items()}
        for n in nodes.values():
            n.update(existing_generation_MW=0.,new_generation_MW=0.,new_technologies_MW={},new_battery_MW=0.,new_battery_MWh=0.)
        for a in fr['assets']:
            n=nodes[a['bus'].split('::')[-1]];t=a['technology'];cap=float(a.get('capacity_MW') or 0)
            if t in GEN:n['existing_generation_MW' if a['existing'] else 'new_generation_MW']+=cap
            if not a['existing']:
                n['new_technologies_MW'][t]=n['new_technologies_MW'].get(t,0.)+cap
                if t=='battery':n['new_battery_MW']+=cap;n['new_battery_MWh']+=float(a.get('energy_MWh') or 0)
        for k,n in nodes.items():assert math.isclose(n['existing_generation_MW'],inherited[k],abs_tol=1e-5)
        for t in ['wind','solar','nuclear','battery']:
            assert math.isclose(sum(n['new_technologies_MW'].get(t,0) for n in nodes.values())/1000,fr['new_capacities_GW'][t],abs_tol=1e-5)
        assert math.isclose(sum(n['new_battery_MWh'] for n in nodes.values())/1000,fr['new_capacities_GW']['battery_energy_GWh'],abs_tol=1e-5)
        for q in fr['transmission']['corridors']:
            q['existing_rating_assumed']=(reg in {'miso','ercot','mexico','south_africa'} or (reg=='new_zealand' and q['existing_capacity_MW']>=49999))
        c.update(case_id=best['case_id'],deployment=best['deployment'],model=best['model'],architecture=best['agent'],
                 final_sha256=best['final_sha256'],candidate_sha256=best['selected']['candidate_sha256'],
                 new_capacities_GW=fr['new_capacities_GW'],corridors=fr['transmission']['corridors'],
                 annual_all_in_cost_billion=minimum,distribution_summary={k:v for k,v in dist.items() if k!='nodes'},
                 feeder_nodes=dist['nodes'],selected_design=f['final_design'],hours_modelled=fr['hours_modelled'],
                 selection=dict(compared=4,cost_tied_case_ids=ties,comparison_basis='Identical 2035 inputs; lowest reported selected all-in annual cost among physically accepted runs',
                                tie_policy='Case-id lexicographic representative for identical reported costs; no model superiority implied'),
                 candidates=[dict(case_id=r['case_id'],model=r['model'],architecture=r['agent'],accepted=True,cost_billion=r['selected']['all_in_cost_billion']) for r in options])
        cases[reg]=c;selection.append({k:c[k] for k in ['case_id','annual_all_in_cost_billion','selection']})
    EV.mkdir(exist_ok=True,parents=True)
    data=dict(schema_version=2,selection='Lowest observed selected all-in annual cost among four physically accepted 2035 runs per region; tied minima explicitly retained. Eight representative portfolios.',
              year=2035,spatial_semantics=original['spatial_semantics'],cases=cases)
    (EV/'planning_maps.json').write_text(json.dumps(data,indent=2,ensure_ascii=False,allow_nan=False)+'\n')
    (EV/'source_manifest.json').write_text(json.dumps(dict(created_utc=datetime.now(timezone.utc).isoformat(),files=pins,
         builder_sha256=sha(__file__),comparison_cases=32,selected_regions=8,year=2035,all_inputs_identical_within_region=True,
         source_results_sha256=sha(source),original_geographic_evidence_sha256=sha(spatial),
         new_inference_calls=0,new_optimizer_calls=0,new_power_flow_calls=0),indent=2)+'\n')
    print(json.dumps(selection,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
