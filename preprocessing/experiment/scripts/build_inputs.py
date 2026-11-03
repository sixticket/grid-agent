"""Rebuild all four experiments from local source files; no network calls."""
from pathlib import Path
import csv,json,math,re,collections,hashlib
import numpy as np
import pandas as pd
import geopandas as gpd
import os
ROOT = Path(os.environ.get('GRID_DATA_ROOT', str(Path(__file__).resolve().parents[3] / 'data'))).expanduser().resolve() / 'experiment'

def js(path,data):path.write_text(json.dumps(data,indent=2,allow_nan=False)+'\n')
def rows(path):
 with path.open() as f:return list(csv.DictReader(f))
def number(v,default=0):
 try:x=float(v);return x if math.isfinite(x) else default
 except:return default
def ident(v):return re.sub('[^a-zA-Z0-9_-]','_',str(v))
def distance(a,b):
 la,lo,lb,lob=map(math.radians,[a[0],a[1],b[0],b[1]])
 return 6371*2*math.asin(min(1,math.sqrt(math.sin((la-lb)/2)**2+math.cos(la)*math.cos(lb)*math.sin((lo-lob)/2)**2)))
def nearest(lat,lon,nodes):return min(nodes,key=lambda k:distance((lat,lon),(nodes[k]['latitude'],nodes[k]['longitude'])))
# These are openly declared modelling defaults, not plant-level operating measurements.
PARAMS={
 'coal':dict(availability=.75,marginal=45,emissions=.95,fixed=45000),
 'gas':dict(availability=.9,marginal=75,emissions=.42,fixed=15000),
 'oil':dict(availability=.9,marginal=180,emissions=.75,fixed=15000),
 'nuclear':dict(availability=.9,marginal=12.9,emissions=0,fixed=100000),
 'hydro':dict(availability=1.,marginal=5,emissions=0,fixed=30000,energy_factor=.4),
 'biomass':dict(availability=.8,marginal=65,emissions=0,fixed=50000),
 'waste':dict(availability=.8,marginal=65,emissions=.4,fixed=50000),
 'geothermal':dict(availability=.9,marginal=10,emissions=.04,fixed=50000),
 'other':dict(availability=.7,marginal=100,emissions=.5,fixed=20000),
 'csp':dict(availability=1,marginal=5,emissions=0,fixed=30000,energy_factor=.35),
 'wind':dict(availability=1,marginal=.1,emissions=0,fixed=25000),
 'solar':dict(availability=1,marginal=.1,emissions=0,fixed=12000)}
# CSP is represented as energy-limited dispatchable generation; no reservoir chronology is inferred.
STORAGE={'battery':(4,.95),'pumped_storage':(10,math.sqrt(.8)),'other_storage':(8,math.sqrt(.65))}

def technology(row):
 t=row.get('Technology','').lower();fuel=row.get('Energy Source 1','')
 if 'pumped' in t:return 'pumped_storage'
 if 'batter' in t:return 'battery'
 if any(s in t for s in ['storage','flywheel','compressed']):return 'other_storage'
 if 'solar thermal' in t:return 'csp'
 if 'solar' in t:return 'solar'
 if 'wind' in t:return 'wind'
 if 'nuclear' in t:return 'nuclear'
 if 'hydro' in t:return 'hydro'
 if 'coal' in t or fuel in ['BIT','SUB','LIG','WC','RC','PC']:return 'coal'
 if 'natural gas' in t or fuel in ['NG','BFG','OG','PG']:return 'gas'
 if 'petroleum' in t or fuel in ['DFO','RFO','JF','KER','WO']:return 'oil'
 if 'geothermal' in t:return 'geothermal'
 if any(s in t for s in ['wood','biomass']):return 'biomass'
 if any(s in t for s in ['waste','landfill']):return 'waste'
 return 'other'

storage={(str(int(number(r['Plant Code']))),r['Generator ID']):r for r in rows(ROOT/'shared/raw/eia860_storage_2023.csv')}

def us_fleet(folder,nodes):
 plants={str(int(number(r['plant_code']))):r for r in rows(folder/'raw/plant_zone_matches.csv')}
 records=[]
 for r in rows(folder/'raw/generator_inventory_with_zones.csv'):
  plant=str(int(number(r['Plant Code'])));pr=plants.get(plant,{})
  lat=number(r.get('Latitude',pr.get('latitude')),None);lon=number(r.get('Longitude',pr.get('longitude')),None)
  group=r.get('effective_group') or r.get('geographic_group') or ''
  method=r.get('allocation_method','geographic proxy');review=r.get('needs_review','False')=='True' or r.get('accepted_for_geographic_proxy','True')!='True'
  if group not in nodes:
   if lat is not None and lon is not None:group=nearest(lat,lon,nodes);method='ASSUMED nearest centroid for unresolved assignment';review=True
   else:raise ValueError('Missing location for unassigned asset '+plant)
  cap=number(r['Nameplate Capacity (MW)']);tech=technology(r);status=r['Status'];energy=None;energy_basis='not applicable'
  if tech in STORAGE:
   st=storage.get((plant,r['Generator ID']),{});energy=number(st.get('Nameplate Energy Capacity (MWh)'))
   energy_basis='EIA-860 energy-storage schedule' if energy>0 else 'ASSUMED duration '+str(STORAGE[tech][0])+' hours'
   if energy<=0:energy=cap*STORAGE[tech][0]
  records.append(dict(source_id=plant+'_'+r['Generator ID'],name=r['Plant Name'],technology=tech,bus=group,capacity_MW=cap,energy_MWh=energy,status=status,active=status=='OP',latitude=lat,longitude=lon,allocation_basis=method,needs_review=review,capacity_basis='EIA-860 2023 year-end nameplate',energy_basis=energy_basis))
 return records

# Manually transcribed and cross-checked from Eskom Integrated Report 2023, printed pp166-167 (PDF page85).
ESKOM={'Arnot':2100,'Camden':1481,'Duvha':2875,'Grootvlei':570,'Hendrina':1098,'Kendal':3840,'Komati':0,'Kriel':2640,'Kusile':2880,'Lethabo':3558,'Majuba':3807,'Matimba':3690,'Matla':3450,'Medupi':3600,'Tutuka':3510,'Koeberg':1854,'Acacia':171,'Ankerlig':1327,'Gourikwa':740,'Port Rex':171,'Drakensberg':1000,'Ingula':1324,'Palmiet':400,'Gariep':360,'Vanderkloof':240,'Sere':100,'Ncora':2}
# 'gas' below is a modelling alias for the report's unresolved Gas/liquid fuel category.
IPP={'wind':3342,'solar':2212,'csp':500,'gas':1005,'hydro':18,'biomass':25,'waste':8}

def za_fleet(folder,nodes):
 wri=rows(folder/'raw/wri_south_africa.csv');rec=[];used=set()
 fuelmap={'Coal':'coal','Gas':'gas','Oil':'oil','Nuclear':'nuclear','Hydro':'hydro','Solar':'solar','Wind':'wind','Waste':'waste','Biomass':'biomass'}
 for name,cap in ESKOM.items():
  matches=[r for r in wri if name.lower() in r['name'].lower()]
  if len(matches)>1:raise ValueError('ambiguous Eskom plant '+name)
  row=matches[0] if matches else None
  if row:used.add(row['gppd_idnr']);lat=number(row['latitude']);lon=number(row['longitude']);coord='WRI plant coordinates'
  else:
   # Small missing stations retain reported capacity at a declared region proxy.
   region='EC' if name=='Ncora' else 'WC';lat=nodes[region]['latitude'];lon=nodes[region]['longitude'];coord='ASSUMED region centroid; plant coordinates unavailable'
  bus=nearest(lat,lon,nodes)
  tech='nuclear' if name=='Koeberg' else 'oil' if name in ['Acacia','Ankerlig','Gourikwa','Port Rex'] else 'pumped_storage' if name in ['Drakensberg','Ingula','Palmiet'] else 'hydro' if name in ['Gariep','Vanderkloof','Ncora'] else 'wind' if name=='Sere' else 'coal'
  rec.append(dict(source_id='ESKOM_'+ident(name),name=name,technology=tech,bus=bus,capacity_MW=cap,energy_MWh=cap*10 if tech=='pumped_storage' else None,status='nominal' if cap else 'closed',active=cap>0,latitude=lat,longitude=lon,allocation_basis='ASSUMED nearest representative supply point; '+coord,needs_review=True,capacity_basis='Eskom nominal capacity at 2023-03-31; not nameplate or hourly availability',energy_basis='ASSUMED 10h reservoir duration' if tech=='pumped_storage' else 'not applicable'))
 # Retain WRI locations as weights; reconcile totals to official IPP technology totals.
 # These rows are spatial allocations of a reported aggregate, not verified individual plant capacities.
 for tech,total in IPP.items():
  candidates=[]
  for r in wri:
   if r['gppd_idnr'] in used or number(r['commissioning_year'])>2023:continue
   typ=fuelmap[r['primary_fuel']]
   if typ=='solar' and any(s in r['name'].lower() for s in ['csp','bokpoort','kaxu','khi solar','xina','kathu']):typ='csp'
   if typ==tech:candidates.append(r)
  weights=sum(number(r['capacity_mw']) for r in candidates)
  if not weights:
   # Preserve aggregate capacity explicitly when locational evidence is absent.
   bus='KZN' if tech=='gas' else 'EC'
   rec.append(dict(source_id='IPP_unlocated_'+tech,name='Reported IPP '+('gas/liquid fuel' if tech=='gas' else tech)+' aggregate',technology=tech,bus=bus,capacity_MW=total,energy_MWh=None,status='reported aggregate',active=True,latitude=None,longitude=None,allocation_basis='ASSUMED representative region; no plant-level match',needs_review=True,capacity_basis='Eskom 2023-03-31 IPP technology total'+('; reported gas/liquid fuel, represented with assumed gas operating coefficients; actual fuel mix unresolved' if tech=='gas' else ''),energy_basis='not applicable'))
  else:
   for r in candidates:
    lat=number(r['latitude']);lon=number(r['longitude']);bus=nearest(lat,lon,nodes)
    rec.append(dict(source_id='IPP_'+r['gppd_idnr'],name=r['name'],technology=tech,bus=bus,capacity_MW=total*number(r['capacity_mw'])/weights,energy_MWh=None,status='aggregate allocation',active=True,latitude=lat,longitude=lon,allocation_basis='ASSUMED WRI capacity-weighted allocation of official IPP total; nearest representative node',needs_review=True,capacity_basis='Official technology total allocated using WRI weights; NOT updated plant-specific capacity',energy_basis='not applicable'))
 return rec

def prepare(region):
 folder=ROOT/region
 for name in ['profiles','greenfield','brownfield','results']: (folder/name).mkdir(exist_ok=True)
 if region!='south_africa':
  nodes={r['demand_group']:dict(id=r['demand_group'],latitude=float(r['latitude']),longitude=float(r['longitude']),location_basis=r['source']) for r in rows(folder/'raw/zone_centroids.csv')}
  frame=pd.read_csv(folder/'raw/demand_2023.csv');frame.rename(columns={frame.columns[0]:'time'},inplace=True);frame['time']=pd.to_datetime(frame.time,utc=True).dt.tz_localize(None)
  records=us_fleet(folder,nodes);demandkind='reported EIA-930 regional hourly demand';timebasis='UTC hourly interval start'
 else:
  geo=gpd.read_file(folder/'raw/provinces.geojson');cent=geo.to_crs('+proj=aea +lat_1=-22 +lat_2=-34 +lat_0=-28 +lon_0=25 +datum=WGS84 +units=m').centroid.to_crs(4326)
  names={'Eastern Cape':'EC','Limpopo':'LP','Mpumalanga':'MP','Gauteng':'GP','Western Cape':'WC','Northern Cape':'NC','KwaZulu-Natal':'KZN','North West':'NW','Free State':'FS'}
  nodes={}
  for (_,r),c in zip(geo.iterrows(),cent):
   name=r['shapeName'].replace('Nothern','Northern');code=names[name];nodes[code]=dict(id=code,latitude=c.y,longitude=c.x,location_basis='Administrative area centroid used as SUPPLY-AREA LOCATION PROXY; not exact electrical boundary')
  nodes['HYDRA']=dict(id='HYDRA',latitude=-30.714612,longitude=24.088830,location_basis='Hydra substation representative point from official Eskom tender; no electrical catchment boundary inferred')
  raw=pd.read_csv(folder/'raw/demand_ref_2023_modelled.csv');frame=pd.DataFrame({'time':pd.date_range('2023-01-01',periods=8760,freq='h')})
  demandnames={'EC':'Eastern Cape','LP':'Limpopo','MP':'Mpumalanga','GP':'Gauteng','WC':'Western Cape','NC':'Northern Cape','HYDRA':'Hydra Central','KZN':'Kwazulu Natal','NW':'North West','FS':'Free State'}
  assert (raw.overall_hour.to_numpy()==np.arange(1,8761)).all()
  for k,v in demandnames.items():frame[k]=raw[v+'_corrected'].values
  records=za_fleet(folder,nodes);demandkind='ESRG REF corrected MODEL demand; raw Eskom regional data unavailable';timebasis='Published hour order interpreted as SAST clock, local model year; interpretation explicit. ERA5 UTC converted to SAST for alignment.'
  js(folder/'raw/eskom_2023_transcription.json',{'source':'eskom_2023.pdf printed pages166-167','nominal_station_MW':ESKOM,'IPP_technology_MW_reported':{('Gas/liquid fuel' if k=='gas' else k):v for k,v in IPP.items()}})
 frame.to_csv(folder/'demand.csv',index=False)
 pd.DataFrame(nodes.values()).to_csv(folder/'nodes.csv',index=False)
 pd.DataFrame(records).to_csv(folder/'inventory_audit.csv',index=False)
 # Aggregate computational components by region, technology and status; retain every row in audit.
 group=collections.defaultdict(list)
 for r in records:
  if r['capacity_MW']>0:group[(r['bus'],r['technology'],r['active'])].append(r)
 assets=[]
 for (bus,tech,active),rs in group.items():
  cap=sum(x['capacity_MW'] for x in rs);params=PARAMS.get(tech,{})
  a=dict(id=bus+'_'+tech+('_active' if active else '_inactive'),bus=bus,technology=tech,capacity_MW=cap,annual_fixed_cost_usd=cap*params.get('fixed',10000))
  if tech in STORAGE:
   a.update(energy_MWh=sum(x['energy_MWh'] or 0 for x in rs),b1_fraction=.4,efficiency_store=STORAGE[tech][1],efficiency_dispatch=STORAGE[tech][1],availability=1 if active else 0)
  else:
   a.update(availability=params['availability'] if active else 0,marginal_cost_usd_per_MWh=params['marginal'],emissions_t_per_MWh=params['emissions'])
   if 'energy_factor' in params:a['annual_capacity_factor']=params['energy_factor'] if active else 0
   if tech=='nuclear':a['p_min_pu']=0.
  assets.append(a)
 # source inventory is fixed; new build limits are declared broad scenario bounds.
 peak=float(frame[list(nodes)].sum(axis=1).max());area_peak={k:float(frame[k].max()) for k in nodes}
 limits={k:dict(wind=peak*3,solar=peak*5,nuclear=peak,battery=peak,electrolyzer=peak,fuel_cell=peak,h2_storage_MWh=peak*2000) for k in nodes}
 netnodes=[dict(id=k,demand_share=float(frame[k].sum()/frame[list(nodes)].to_numpy().sum()),profiles_csv='profiles/'+k+'.csv',new_capacity_limits_MW=limits[k]) for k in nodes]
 corridors=[]
 # Fully connected candidate set: assumptions are uniform across systems and modes.
 for i,a in enumerate(nodes):
  for b in list(nodes)[i+1:]:
   km=distance((nodes[a]['latitude'],nodes[a]['longitude']),(nodes[b]['latitude'],nodes[b]['longitude']))
   # One large corridor can represent a bundle; 0.3 ohm/km is divided by bundle count.
   rating=max(1000,math.ceil(max(area_peak[a],area_peak[b])/1000)*1000)
   corridors.append(dict(id=a+'_'+b,bus0=a,bus1=b,length_km=km,circuit_rating_MW=rating,reactance_ohm=max(.1,.3*km/(rating/1000)),max_new_circuits=1,existing_circuits=0,new_annual_cost_per_circuit_usd=km*rating*200,existing_annual_fixed_cost_per_circuit_usd=0))
 js(folder/'network.json',dict(schema_version=1,scenario=region,provenance=dict(kind='derived',source='Reported/modelled zonal demand; centroid/representative-point straight corridors. All line capacity/reactance/cost values are stated modelling assumptions, not measured existing lines.'),voltage_kV=400,angle_bound_rad=.5,nodes=netnodes,corridors=corridors))
 js(folder/'inventory.json',dict(schema_version=1,scenario=region,provenance=dict(kind='derived',source='See raw sources and inventory_audit.csv. '+('EIA-860 year-end2023; unresolved assignments use labelled geographic proxies.' if region!='south_africa' else 'Eskom 2023-03-31 nominal fleet plus IPP technology totals spatialized using WRI weights. Not a fully verified plant-level census.'),year=2023),assets=assets))
 # Section length is calibrated so the bare peak load without any DER sits inside the voltage band (about 0.96 pu US, 0.98 pu ZA):
 # a distribution failure then reflects the design's PV/battery dispatch, not the feeder itself.
 dist=dict(feeder_kv=22. if region=='south_africa' else 12.47,feeder_peak_kW=5000.,line_r1_ohm_km=.25,line_x1_ohm_km=.33,line_km=3. if region=='south_africa' else 2.,v_band_pu=[.95,1.05],volt_var_pf=.95,volt_var_deadband_pu=1.03,line_upgrade_impedance_factor=.5,line_upgrade_cost_per_feeder_yr=30000. if region=='south_africa' else 40000.,line_normal_amps={'l1':400.,'l2':400.},line_upgrade_normal_amps={'l1':600.,'l2':600.})
 ass=dict(demand_source_type=demandkind,time_basis=timebasis,base_demand_year=2023,fleet_date='2023-03-31' if region=='south_africa' else '2023-12-31',network='lossless DC candidate corridor bundles; all existing inter-area circuits zero in both modes; brownfield retains generation/storage only',line_parameter_basis='ASSUMED 400kV, 0.3 ohm/km per 1GW bundle, 200 USD/MW-km-year; no geographic construction constraints',candidate_site_basis='one resource point per demand region; no parcel-level suitability claim; broad capacity bounds are assumptions',distribution_basis='ASSUMED two-section representative feeder; 400A normal,600A reinforced ratings; not measured local network',existing_operating_parameters=PARAMS,storage_duration_fallback_hours={k:v[0] for k,v in STORAGE.items()},storage_boundary='cyclic SOC; no free initial/final energy',voll_usd_per_MWh=50000,unserved_fraction_limit=1e-4,co2_limit_Mt=1000.,co2_policy='Permissive baseline; edit this fixed assumption for a policy scenario. Agents cannot relax the cap.',imports='No externally fixed import entitlement; model is a domestic/regionally isolated supply counterfactual',fuel_model='continuous dispatch with fixed availability factors, no unit commitment; hydro/CSP energy-limited, no seasonal reservoir dynamics',spatial_uncertainty='US conflicting/unassigned plants retain source flags with explicit nearest/known geographic assignment. ZA station assignment is nearest representative point, not known supply-area membership.')
 js(folder/'assumptions.json',ass)
 js(folder/'config.json',dict(id=region,description=f'{region}: {len(nodes)} demand regions; paired greenfield and existing-generation brownfield. Actual source data plus explicit network/resource and operating assumptions.',cost_file='costs_za.json' if region=='south_africa' else 'costs_us.json',global_new_capacity_limits_GW=dict(wind=peak*3/1000,solar=peak*5/1000,nuclear=peak/1000),distribution=dist))
 for mode in ['greenfield','brownfield']:
  js(folder/mode/'mode.json',dict(region=region,mode=mode,shared_inputs=['../config.json','../assumptions.json','../demand.csv','../network.json','../profiles'],existing_assets='../inventory.json' if mode=='brownfield' else None))
 js(folder/'input_audit.json',dict(source_inventory_rows=len(records),retained_positive_capacity_rows=sum(r['capacity_MW']>0 for r in records),model_asset_groups=len(assets),all_capacity_MW=sum(r['capacity_MW'] for r in records),active_capacity_MW=sum(r['capacity_MW'] for r in records if r['active']),capacity_by_technology_MW={t:sum(r['capacity_MW'] for r in records if r['technology']==t and r['active']) for t in sorted({r['technology'] for r in records})},assignment_review_rows=sum(r['needs_review'] for r in records),demand_hours=len(frame),demand_regions=len(nodes)))
 print(region,len(nodes),len(records),len(assets),flush=True)

if __name__=='__main__':
 for region in ['miso','ercot','nyiso','south_africa']:prepare(region)
