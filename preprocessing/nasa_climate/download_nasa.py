#!/usr/bin/env python3
"""Archive NASA NEX-GDDP-CMIP6 v2 regional subsets and extract daily node data.
All download URLs, response hashes, grid selections and CF metadata are retained.
No existing project files are changed. No credentials or paid service is used.
"""
from __future__ import annotations
import argparse, concurrent.futures as cf, csv, datetime as dt, hashlib, json, math, os
from pathlib import Path
import sys, time, urllib.request, urllib.parse, xml.etree.ElementTree as ET
# Loading a sibling explicitly also supports importlib-based offline validators.
import importlib.util as _importlib_util
_spec = _importlib_util.spec_from_file_location("_grid_nasa_paths", Path(__file__).resolve().with_name("_paths.py"))
_paths = _importlib_util.module_from_spec(_spec)
_spec.loader.exec_module(_paths)
RELEASE_ROOT = _paths.RELEASE_ROOT
ROOT=_paths.data_root()
OUT=ROOT/'nasa_climate'

def configure_roots(data_root=None):
 global ROOT, OUT
 ROOT=_paths.data_root(data_root);OUT=ROOT/'nasa_climate'
 return ROOT

def _load_science():
 global np, netCDF4
 import numpy as np
 import netCDF4
REGIONS=['miso','ercot','nyiso','south_africa','australia','new_zealand','mexico','brazil']
MODELS=['ACCESS-CM2','MPI-ESM1-2-HR','MRI-ESM2-0']
VARS=['tasmin','tasmax','rsds','sfcWind']
MEMBER='r1i1p1f1'
BASE='https://ds.nccs.nasa.gov/thredds/'
COLUMNS={'tasmin':'tasmin_K','tasmax':'tasmax_K','rsds':'rsds_W_m2','sfcWind':'sfcWind_m_s'}

def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def dump(p,d):
 p=Path(p);p.parent.mkdir(parents=True,exist_ok=True)
 q=p.with_suffix(p.suffix+'.tmp');q.write_text(json.dumps(d,indent=2,ensure_ascii=False,allow_nan=False)+'\n');q.replace(p)
def nodes(regions=None):
 result={}
 for r in (regions or REGIONS):
  with (ROOT/'experiment'/r/'nodes.csv').open() as f: rows=list(csv.DictReader(f))
  for row in rows:
   row['latitude']=float(row['latitude']);row['longitude']=float(row['longitude'])
  result[r]=rows
 return result

def fetch(url,path,timeout=240,retries=4):
 path=Path(path);side=path.with_suffix(path.suffix+'.download.json')
 if path.exists() and side.exists():
  meta=json.loads(side.read_text())
  if meta.get('url')==url and meta.get('sha256')==sha(path):return {'cached':True,'bytes':path.stat().st_size}
 error=None
 for attempt in range(retries):
  try:
   req=urllib.request.Request(url,headers={'User-Agent':'grid-agent-climate-research/1.0'})
   with urllib.request.urlopen(req,timeout=timeout) as response:
    payload=response.read();headers=dict(response.headers);resolved=response.url
   if not payload:raise ValueError('empty response')
   if path.suffix=='.nc' and not (payload.startswith(b'CDF') or payload.startswith(b'\x89HDF')):raise ValueError('Expected NetCDF, got '+repr(payload[:160]))
   if path.suffix=='.json':json.loads(payload)
   path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_suffix(path.suffix+'.partial');tmp.write_bytes(payload);tmp.replace(path)
   dump(side,{'url':url,'resolved_url':resolved,'retrieved_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'bytes':len(payload),'sha256':sha(path),'response_headers':{k:v for k,v in headers.items() if k.lower() in ['content-type','content-length','last-modified','etag']}})
   return {'cached':False,'bytes':len(payload)}
  except Exception as exc:
   error=str(exc)
   if attempt<retries-1:time.sleep(min(20,2**attempt*2))
 raise RuntimeError(f'{url}: {error}')

def catalogue(model,var,scenario):
 url=BASE+f'catalog/AMES/NEX/GDDP-CMIP6/{model}/{scenario}/{MEMBER}/{var}/catalog.xml'
 path=OUT/'raw/catalogs'/model/scenario/f'{var}.xml'
 fetch(url,path)
 tree=ET.fromstring(path.read_bytes());found={}
 for e in tree.iter():
  p=e.attrib.get('urlPath','');name=e.attrib.get('name','')
  if p and name.endswith('_v2.0.nc'):
   for yr in [2029,2030,2031,2034,2035,2036]:
    if f'_{yr}_v2.0.nc' in name:found[yr]=p
 return found

def nasa_jobs(ns,models,years,regions,scenario):
 jobs=[]
 for model in models:
  for var in VARS:
   cat=catalogue(model,var,scenario)
   for year in years:
    segments=[(year,'annual',f'{year}-01-01',f'{year}-12-31'),(year-1,'previous',f'{year-1}-12-31',f'{year-1}-12-31'),(year+1,'next',f'{year+1}-01-01',f'{year+1}-01-01')]
    for source_year,segment,start,end in segments:
     if source_year not in cat:raise ValueError(f'No v2.0 file for {model}/{var}/{source_year}')
     source=cat[source_year]
     for region in regions:
      rows=ns[region];lat=[r['latitude'] for r in rows];lon=[r['longitude'] for r in rows]
      # Padding permits exact nearest-grid matching and a documented coastal-land fallback.
      query={'var':var,'north':max(lat)+.5,'south':min(lat)-.5,'west':min(lon)-.5,'east':max(lon)+.5,'horizStride':1,'time_start':start+'T00:00:00Z','time_end':end+'T23:59:59Z','accept':'netcdf4','addLatLon':'true'}
      url=BASE+'ncss/grid/'+source+'?'+urllib.parse.urlencode(query)
      path=OUT/'raw/nasa'/model/scenario/str(year)/region/f'{var}_{segment}.nc'
      jobs.append(dict(url=url,path=str(path),model=model,var=var,year=year,source_year=source_year,region=region,segment=segment,source_path=source))
 return jobs

def companion_jobs(ns,regions):
 fields='temperature_2m,wind_speed_10m,wind_speed_100m,shortwave_radiation,direct_normal_irradiance,diffuse_radiation'
 jobs=[]
 for region in regions:
  for n in ns[region]:
   q={'latitude':n['latitude'],'longitude':n['longitude'],'start_date':'2022-12-30','end_date':'2024-01-02','hourly':fields,'models':'era5','wind_speed_unit':'ms','timezone':'UTC'}
   jobs.append({'url':'https://archive-api.open-meteo.com/v1/archive?'+urllib.parse.urlencode(q),'path':str(OUT/'raw/era5_10m'/region/(n['id']+'.json')),'region':region,'node':n['id']})
 return jobs

def run_jobs(jobs,workers,label):
 errors=[];done=0;totalbytes=0;start=time.time()
 print(json.dumps({'stage':label,'jobs':len(jobs),'workers':workers}),flush=True)
 def run(j):return fetch(j['url'],j['path'])
 with cf.ThreadPoolExecutor(max_workers=workers) as pool:
  pending={pool.submit(run,j):j for j in jobs}
  for fut in cf.as_completed(pending):
   j=pending[fut];done+=1
   try:r=fut.result();totalbytes+=r['bytes']
   except Exception as exc:errors.append({'job':j,'error':str(exc)});print(json.dumps({'error':str(exc),'path':j['path']}),flush=True)
   if done%8==0 or done==len(jobs):
    print(json.dumps({'stage':label,'done':done,'total':len(jobs),'MB':round(totalbytes/1e6,1),'elapsed_s':round(time.time()-start),'errors':len(errors)}),flush=True)
    dump(OUT/'work'/f'{label}_progress.json',{'done':done,'total':len(jobs),'MB':totalbytes/1e6,'elapsed_s':time.time()-start,'errors':errors})
 if errors:raise RuntimeError(f'{len(errors)} {label} downloads failed; rerun resumes verified files')

def decode(path,var):
 with netCDF4.Dataset(path) as d:
  t=d.variables['time'];cal=getattr(t,'calendar','standard')
  if cal not in ['standard','gregorian','proleptic_gregorian']:raise ValueError(f'{path}: calendar {cal} unsupported; do not relabel')
  dates=[x.strftime('%Y-%m-%d') for x in netCDF4.num2date(t[:],t.units,calendar=cal)]
  lat=np.asarray(d.variables['lat'][:]);lon=np.asarray(d.variables['lon'][:]);lon=(lon+180)%360-180
  v=d.variables[var];a=np.ma.filled(v[:],np.nan).astype(float)
  if v.dimensions!=('time','lat','lon'):raise ValueError(f'{path}: unexpected dimensions {v.dimensions}')
  attrs={k:d.getncattr(k) for k in d.ncattrs()};va={k:v.getncattr(k) for k in v.ncattrs()}
  version=str(attrs.get('version',''))
  if version not in ['2.0','v2.0']:raise ValueError(f'wrong or missing NASA version: {path} {version}')
  expected_units={'tasmin':'K','tasmax':'K','rsds':'W m-2','sfcWind':'m s-1'}
  if va.get('units') != expected_units[var]:raise ValueError(f'{path}: unexpected units {va.get("units")}')
  parts=path.relative_to(OUT/'raw/nasa').parts
  expected_attrs={'cmip6_source_id':parts[0],'scenario':parts[1],'variant_label':MEMBER,'frequency':'day','activity':'NEX-GDDP-CMIP6'}
  for key,value in expected_attrs.items():
   if attrs.get(key)!=value:raise ValueError(f'{path}: wrong {key}: {attrs.get(key)}')
  return dates,lat,lon,a,{'calendar':cal,'time_units':t.units,'global_attributes':attrs,'variable_attributes':va}

def jsonable(x):
 if isinstance(x,dict):return {k:jsonable(v) for k,v in x.items()}
 if isinstance(x,(list,tuple)):return [jsonable(v) for v in x]
 if isinstance(x,np.ndarray):return x.tolist()
 if isinstance(x,np.generic):return x.item()
 return x

def normalize(ns,models,years,regions,scenario):
 mapping=[]
 for model in models:
  for year in years:
   records=[];inputs=[]
   expected=[(dt.date(year,1,1)+dt.timedelta(days=i)).isoformat() for i in range(-1,366)]
   for region in regions:
    data={};meta={};grid=None
    for var in VARS:
     chunks=[];dates=[]
     for segment in ['previous','annual','next']:
      p=OUT/'raw/nasa'/model/scenario/str(year)/region/f'{var}_{segment}.nc'
      ds,lat,lon,a,attrs=decode(p,var)
      if grid is None:grid=(lat,lon)
      elif not (np.array_equal(lat,grid[0]) and np.array_equal(lon,grid[1])):raise ValueError('NASA subset grids differ between variables/segments')
      chunks.append(a);dates+=ds
      inputs.append({'path':str(p.relative_to(OUT)),'sha256':sha(p),'metadata':jsonable(attrs)})
     if dates!=expected:raise ValueError(f'{model}/{year}/{region}/{var}: wrong daily calendar or count: {len(dates)}')
     data[var]=np.concatenate(chunks)
    lat,lon=grid
    finite=np.logical_and.reduce([np.isfinite(a).all(axis=0) for a in data.values()])
    for node in ns[region]:
     # Select one common finite cell for all four variables and all 367 dates.
     dist=(lat[:,None]-node['latitude'])**2+((lon[None,:]-node['longitude'])*np.cos(np.deg2rad(node['latitude'])))**2
     closest=np.unravel_index(np.argmin(dist),dist.shape)
     good=np.where(finite,dist,np.inf);iy,ix=np.unravel_index(np.argmin(good),good.shape)
     km=111.195*math.sqrt(float(good[iy,ix]))
     if not np.isfinite(km) or km>75:raise ValueError(f'{region}/{node["id"]}: no common land cell within75km')
     entry={'model':model,'scenario':scenario,'year':year,'region':region,'node':node['id'],'latitude':node['latitude'],'longitude':node['longitude'],'nasa_latitude':float(lat[iy]),'nasa_longitude':float(lon[ix]),'approx_distance_km':km,'nearest_cell_missing':bool(closest!=(iy,ix))}
     mapping.append(entry)
     vals={v:data[v][:,iy,ix] for v in VARS}
     if np.any(vals['tasmin']>vals['tasmax']):raise ValueError(f'tasmin>tasmax {entry}')
     if np.any(vals['rsds']<0) or np.any(vals['sfcWind']<0):raise ValueError(f'negative radiation/wind {entry}')
     for k,date in enumerate(expected):
      rec={key:entry[key] for key in ['region','node','latitude','longitude','nasa_latitude','nasa_longitude']};rec={'date':date,**rec}
      rec.update({COLUMNS[v]:float(vals[v][k]) for v in VARS});records.append(rec)
   p=OUT/'processed/daily'/model/scenario/f'{year}.csv';p.parent.mkdir(parents=True,exist_ok=True)
   with p.open('w',newline='') as f:
    w=csv.DictWriter(f,fieldnames=list(records[0]));w.writeheader();w.writerows(records)
   dump(p.with_suffix('.provenance.json'),{'dataset':'NASA NEX-GDDP-CMIP6','version':'2.0','model':model,'member':MEMBER,'scenario':scenario,'projection_year':year,'dates':'previous Dec31 + target year + next Jan1','source_frequency':'daily','selection':'nearest common finite land grid cell across4vars and367days; maximum75km; distances explicit','daily_csv_sha256':sha(p),'raw_inputs':inputs,'node_mapping':[m for m in mapping if m['model']==model and m['year']==year],'limitations':['An individual model realization, not a forecast of actual calendar-day weather.','Three GCMs are an analyst-selected convenience sample, not a complete or probability-weighted ensemble.','NASA daily values are retained without hourly interpretation; wind is near-surface, not100m.']})
   print(json.dumps({'daily':str(p),'rows':len(records)}),flush=True)
 dump(OUT/'validation/daily_extraction.json',{'passes':True,'models':models,'years':years,'scenario':scenario,'regions':regions,'mapping':mapping})

def main():
 p=argparse.ArgumentParser();p.add_argument('stage',choices=['download','normalize','companion','all']);p.add_argument('--models',nargs='+',default=['MPI-ESM1-2-HR'],choices=MODELS);p.add_argument('--years',nargs='+',type=int,default=[2030,2035]);p.add_argument('--regions',nargs='+',default=REGIONS,choices=REGIONS);p.add_argument('--scenario',default='ssp245');p.add_argument('--workers',type=int,default=4);_paths.add_data_root(p);a=p.parse_args()
 configure_roots(a.data_root)
 if a.workers < 1:p.error('--workers must be positive')
 if any(y not in [2030,2035] for y in a.years):p.error('This registered dataset supports projection years 2030 and 2035 only.')
 if a.scenario != 'ssp245':p.error('This registered baseline uses ssp245; create a separate reviewed specification for other SSPs.')
 if a.plan:
  print(json.dumps({'stage':a.stage,'selection':_paths.selection_json(a),'data_root':str(ROOT),'output':str(OUT),'node_inputs':[str(ROOT/'experiment'/r/'nodes.csv') for r in a.regions],'nasa_subset_requests':12*len(a.models)*len(a.years)*len(a.regions),'network_executed':False,'files_written':False},indent=2));return
 _load_science()
 ns=nodes(a.regions);dump(OUT/'work/node_inventory.json',ns)
 if a.stage in ['download','all']:
  jobs=nasa_jobs(ns,a.models,a.years,a.regions,a.scenario);dump(OUT/'work/nasa_download_jobs.json',jobs);run_jobs(jobs,a.workers,'nasa')
 if a.stage in ['companion','all']:run_jobs(companion_jobs(ns,a.regions),min(a.workers,2),'era5_companion')
 if a.stage in ['normalize','all']:normalize(ns,a.models,a.years,a.regions,a.scenario)
if __name__=='__main__':main()
