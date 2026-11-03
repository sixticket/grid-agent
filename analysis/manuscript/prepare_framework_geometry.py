"""Freeze existing geographic and numerical evidence for the integrated Figure 1.
No model, API, optimizer, power-flow or experimental write operations.
"""
from pathlib import Path
import os, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import ArchiveResolver
_ARCHIVE = ArchiveResolver(Path(os.environ.get("GRID_AGENT_ARCHIVE_ROOT", "external-archive")), __import__("json").loads(os.environ.get("GRID_AGENT_ARCHIVE_MAP", "{}")))
_WORKSPACE = Path(os.environ.get("GRID_AGENT_MANUSCRIPT_ROOT", "manuscript-workspace")).resolve()
import json,hashlib,csv,math
import geopandas as gpd
import xarray as xr
import numpy as np
from shapely.geometry import box
from shapely.ops import transform
from pyproj import Transformer
R=_WORKSPACE
E=R/'evidence/integrated_loop_20261006';E.mkdir(parents=True,exist_ok=True)
geo=R/'evidence/geographic_20261003/ne_50m_admin_0_countries.zip'
casespath=R/'evidence/best_regional_20261006/planning_maps.json'
cases=json.loads(casespath.read_text())['cases']
tr=Transformer.from_crs(4326,'ESRI:54030',always_xy=True)
def polygons(g):
 if g.is_empty:return []
 if g.geom_type=='Polygon':return [list(g.exterior.coords)]
 if hasattr(g,'geoms'):return [p for a in g.geoms for p in polygons(a)]
 return []
world=[]
for _,r in gpd.read_file('zip://'+str(geo)).iterrows():
 if r['ADMIN']=='Antarctica':continue
 g=transform(tr.transform,r.geometry.intersection(box(-180,-58,180,84))).simplify(27000,preserve_topology=True)
 world.append(dict(name=r['ADMIN'],polygons=polygons(g)))
allp=[p for r in world for a in r['polygons'] for p in a]
names={'miso':'MISO','ercot':'ERCOT','nyiso':'NYISO','mexico':'Mexico','brazil':'Brazil','south_africa':'South Africa','australia':'Australia','new_zealand':'New Zealand'}
regions=[]
for key in names:
 c=cases[key];nodes=[dict(id=n['id'],xy=tr.transform(n['longitude'],n['latitude']),new_MW=n['new_technologies_MW']) for n in c['nodes']]
 regions.append(dict(id=key,name=names[key],nodes=nodes,center=[sum(n['xy'][k] for n in nodes)/len(nodes) for k in range(2)],case_id=c['case_id'],new_capacities_GW=c['new_capacities_GW'],annual_cost_billion=c['annual_all_in_cost_billion'],corridors=c['corridors'],candidate_sha256=c['candidate_sha256']))
p=_ARCHIVE('weather_prediction/inputs/2035/ercot/profiles/COAS.csv')
rows=list(csv.DictReader(p.open()))[:168]
profiles={k:[float(r[k]) for r in rows] for k in ['demand_MW','solar_cf','wind_cf']}
# Local projection preserves the structure of the selected ERCOT model topology.
ct=Transformer.from_crs(4326,'+proj=laea +lat_0=31 +lon_0=-100 +datum=WGS84 +units=m',always_xy=True)
c=cases['ercot'];xy={n['id']:ct.transform(n['longitude'],n['latitude']) for n in c['nodes']}
out=dict(bounds=[min(p[0] for p in allp),min(p[1] for p in allp),max(p[0] for p in allp),max(p[1] for p in allp)],world=world,regions=regions,model_zones=sum(len(r['nodes']) for r in regions),profiles=profiles,profile_semantics='First 168 prepared hours of ERCOT COAS 2035. Projected demand and NASA-derived solar/wind profiles on the 2023 intraday time template. Traces are independently normalized for a conceptual depiction, not measured target-year operation.',profile_source=str(p),topology=dict(nodes=xy,corridors=c['corridors']),sources={str(p):hashlib.sha256(p.read_bytes()).hexdigest(),str(geo.relative_to(R)):hashlib.sha256(geo.read_bytes()).hexdigest(),str(casespath.relative_to(R)):hashlib.sha256(casespath.read_bytes()).hexdigest()},spatial_semantics='Natural Earth country outlines supply context. Black points are all 58 representative model zones, not actual new-plant sites or full service boundaries. Topology is the existing ERCOT model, not mapped conductor routes. Capacity bars show frozen selected 2035 zonal totals; battery bars are power, not energy.')
# Read the already accepted, selected dispatch artifact. No PyPSA import or solve.
raw=_ARCHIVE('weather_prediction/gpt_6_1_sol/runs/20261001T004043_005423Z/2035/ercot/multi/attempt_001')
final=json.loads((raw/'final.json').read_text());meta=final['final_selection']['dispatch_checkpoint'];dispatch_path=raw/meta['path']
assert hashlib.sha256((raw/'final.json').read_bytes()).hexdigest()==cases['ercot']['final_sha256']
assert final['final_results']['new_capacities_GW']==cases['ercot']['new_capacities_GW']
assert final['final_selection']['all_in_cost_billion']==cases['ercot']['annual_all_in_cost_billion']
assert hashlib.sha256(dispatch_path.read_bytes()).hexdigest()==meta['sha256']
with xr.open_dataset(dispatch_path) as ds:
 gen=ds['generators_t_p'].isel(snapshots=slice(0,168))
 groups={k:np.zeros(168) for k in ['existing','nuclear','wind','solar','battery']}
 for name in ds['generators_t_p_i'].values:
  k='existing' if str(name).startswith('existing__') else str(name).split('::')[0]
  if k in groups:groups[k]+=np.maximum(gen.sel(generators_t_p_i=name).values,0)
 sto=ds['storage_units_t_p_dispatch'].isel(snapshots=slice(0,168))
 for name in ds['storage_units_t_p_dispatch_i'].values:
  k='existing' if str(name).startswith('existing__') else 'battery'
  groups[k]+=np.maximum(sto.sel(storage_units_t_p_dispatch_i=name).values,0)
 out['dispatch']={k:v.tolist() for k,v in groups.items()}
 out['dispatch_semantics']='First 168 hours from the retained accepted ERCOT 2035 Sol multi-agent dispatch. Stacks are generation plus positive storage discharge, including energy subsequently used to charge storage; not net load. Black is inherited plant/storage dispatch and colours are new technology dispatch.'
 out['dispatch_candidate_sha256']=meta['candidate_sha256']
 out['sources'][str(dispatch_path)]=meta['sha256']
 out['sources'][str(raw/'final.json')]=cases['ercot']['final_sha256']
(E/'framework_geometry.json').write_text(json.dumps(out,separators=(',',':'))+'\n')
print({'regions':len(regions),'zones':out['model_zones'],'profile_hours':len(rows)})
