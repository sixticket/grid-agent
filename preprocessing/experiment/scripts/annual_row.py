"""Print a one-line summary of an annual result directory (for VALIDATION.md and the manuscript table)."""
import json,sys
for path in sys.argv[1:]:
    d=json.load(open(path+'/final.json'));r=d['final_results'];a=d['acceptance'];dc=d['final_distribution_check'] or {};tx=r['transmission']
    built=[p['id'] for p in tx.get('projects',[]) if p.get('built')];exp=sum(c['new_circuits'] for c in tx['corridors'])
    binding=[(i.get('id') or i.get('interface'),i['binding_hours']) for i in tx.get('interfaces',[]) if i.get('binding_hours',0)>0]
    sat=[c['corridor'] for c in tx['corridors'] if c.get('total_capacity_MW',0)>0 and c.get('peak_utilization',0)>=0.999]
    fails={k:(v.get('overvoltage_hours'),v.get('undervoltage_hours')) for k,v in dc.get('nodes',{}).items() if not v['passes']}
    new={k:round(v,1) for k,v in r['new_capacities_GW'].items() if v>0.05}
    print(f"{path}: status={r['status']} solve={r['solve_seconds']:.0f}s cost={r['total_cost_billion']:.2f} lb={r['cost_lower_bound_billion']:.2f} gap={r['mip_gap']:.4f} unmet={r['unmet_demand_GWh']:.3f}GWh passes={a['passes']} dist_fail={fails} projects={built} expansions={exp} binding={binding} saturated={sat} newGW={new}")
