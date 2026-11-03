"""Cache ERA5 at representative points and derive PVWatts/wind profiles."""
from pathlib import Path
import json,time,hashlib,urllib.request,urllib.parse,concurrent.futures
import numpy as np
import pandas as pd
import PySAM.Pvwattsv8 as pv
import os
ROOT = Path(os.environ.get('GRID_DATA_ROOT', str(Path(__file__).resolve().parents[3] / 'data'))).expanduser().resolve() / 'experiment'
FIELDS='temperature_2m,wind_speed_100m,shortwave_radiation,direct_normal_irradiance,diffuse_radiation'
curve=pd.read_csv(ROOT/'shared/raw/NREL_Reference_4MW_150_powercurve.csv')

def offset_hours(region,node):
 # Model clock offset from UTC for the node's demand series: US packs are UTC, South Africa SAST (+2);
 # newer packs carry the offset per node in nodes.csv (utc_offset_hours).
 if hasattr(node,'utc_offset_hours') and node.utc_offset_hours==node.utc_offset_hours:return int(node.utc_offset_hours)
 return 2 if region=='south_africa' else 0

def build(task):
 region,node=task;folder=ROOT/region;bus=str(node.id);off=offset_hours(region,node)
 rawdir=folder/'raw/weather';rawdir.mkdir(exist_ok=True)
 path=rawdir/(bus+'.json')
 params=dict(latitude=node.latitude,longitude=node.longitude,start_date='2022-12-30' if off>0 else '2023-01-01',end_date='2023-12-31' if off>0 else '2024-01-02',hourly=FIELDS,models='era5',wind_speed_unit='ms',timezone='UTC')
 url='https://archive-api.open-meteo.com/v1/archive?'+urllib.parse.urlencode(params)
 if not path.exists():
  for attempt in range(8):
   try:
    with urllib.request.urlopen(url,timeout=120) as response:data=json.load(response)
    if 'hourly' not in data:raise ValueError(str(data))
    path.write_text(json.dumps(data));break
   except Exception:
    if attempt==7:raise
    time.sleep(min(40,3*2**attempt))
 else:data=json.loads(path.read_text())
 h=pd.DataFrame(data['hourly']).set_index('time');h.index=pd.to_datetime(h.index)
 demand=pd.read_csv(folder/'demand.csv',dtype={bus:float});local=pd.DatetimeIndex(pd.to_datetime(demand.time));utc=local-pd.Timedelta(hours=off)
 # Radiation values are means for the preceding hour. Load timestamps label interval starts.
 rad=h.reindex(utc+pd.Timedelta(hours=1));met=h.reindex(utc)
 srd=dict(lat=float(node.latitude),lon=float(node.longitude),tz=off,elev=float(data['elevation']),year=local.year.tolist(),month=local.month.tolist(),day=local.day.tolist(),hour=local.hour.tolist(),minute=[30]*8760,dn=rad.direct_normal_irradiance.tolist(),df=rad.diffuse_radiation.tolist(),gh=rad.shortwave_radiation.tolist(),tdry=met.temperature_2m.tolist(),wspd=met.wind_speed_100m.tolist())
 for key in ['dn','df','gh','tdry','wspd']:
  if not np.isfinite(srd[key]).all():raise ValueError(f'{region}/{bus}: incomplete {key}')
 model=pv.default('PVWattsNone');model.SolarResource.solar_resource_data=srd
 model.SystemDesign.system_capacity=1000.;model.SystemDesign.tilt=abs(float(node.latitude));model.SystemDesign.azimuth=0 if node.latitude<0 else 180;model.SystemDesign.array_type=0;model.SystemDesign.dc_ac_ratio=1.;model.SystemDesign.losses=14.;model.execute()
 solar=np.clip(np.asarray(model.Outputs.ac)/1e6,0,1)
 wind=np.interp(met.wind_speed_100m,curve.iloc[:,0],curve.iloc[:,1],left=0,right=0)/4000
 frame=pd.DataFrame(dict(time=local,demand_MW=demand[bus],solar_cf=solar,wind_cf=wind))
 if len(frame)!=8760 or frame.isna().any().any():raise ValueError('invalid annual profiles')
 frame.to_csv(folder/'profiles'/(bus+'.csv'),index=False)
 manifest=dict(source_url=url,weather='ERA5 reanalysis, not direct site measurement',raw_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),radiation_alignment='preceding-hour average shifted to load interval start; PV evaluated at interval midpoint',wind='100 m ERA5 wind with NREL 4 MW / 150 m rotor reference power curve; no measured wake loss or hub-height correction',pv='PVWatts v8; 1 MW DC, DC/AC=1, fixed rack, tilt=absolute latitude, 14% losses; CF expressed per MW DC; ERA5 100m wind used as thermal-wind proxy',pv_system_design=model.SystemDesign.export(),weather_point=dict(latitude=data['latitude'],longitude=data['longitude']),capacity_factor_mean=dict(solar=float(solar.mean()),wind=float(wind.mean())))
 (folder/'profiles'/(bus+'.provenance.json')).write_text(json.dumps(manifest,indent=2))
 print(region,bus,round(solar.mean(),3),round(wind.mean(),3),flush=True)
if __name__=='__main__':
 import sys
 regions=sys.argv[1:] or ['miso','ercot','nyiso','south_africa']
 tasks=[(r,n) for r in regions for n in pd.read_csv(ROOT/r/'nodes.csv',dtype={'id':str}).itertuples()]
 with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(build,tasks))
