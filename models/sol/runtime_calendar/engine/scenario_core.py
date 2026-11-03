"""Load only files contained in the portable experiment directory."""
from pathlib import Path
from types import SimpleNamespace
import os,json,hashlib,copy
import numpy as np
import pandas as pd
ROOT=Path(__file__).resolve().parents[1]

def planning_year_for(folder, cfg):
    """Read the planning calendar before the transmission design space is built.

    Profile timestamps remain in 2023. They must not determine the availability
    of projects in a future-demand scenario.
    """
    prediction = cfg.get('prediction')
    manifest_path = Path(folder) / 'prediction_manifest.json'
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        if not isinstance(manifest, dict) or not isinstance(manifest.get('scenario'), dict):
            raise ValueError('Prediction manifest requires a scenario object')
        recorded = manifest['scenario']
        if not isinstance(prediction, dict) or prediction != recorded:
            raise ValueError('Config prediction and manifest scenario must match')
    if prediction is None:
        return 2023
    if not isinstance(prediction, dict) or prediction.get('region') != cfg.get('id'):
        raise ValueError('Prediction region must match the pack id')
    year = prediction.get('planning_year')
    if isinstance(year, bool) or not isinstance(year, int) or not 1 <= year <= 9999:
        raise ValueError('Prediction planning_year must be an integer calendar year')
    if prediction.get('weather_year') != 2023:
        # NASA daily weather may represent the planning year while its demand
        # clock and ERA5 intraday template remain 2023. Project eligibility
        # continues to use the same planning_year returned below.
        climate = prediction.get('climate')
        required = {'source_kind': 'synthetic_hourly_nasa_daily_with_era5_template',
                    'projection_year': year, 'weather_year': year,
                    'time_axis_year': 2023, 'weather_template_year': 2023}
        if (not isinstance(climate, dict)
                or any(climate.get(key) != value for key, value in required.items())
                or prediction.get('weather_year') != year
                or prediction.get('time_axis_year') != 2023
                or prediction.get('weather_template_year') != 2023):
            raise ValueError('Future weather requires explicit NASA climate metadata and the 2023 model clock')
        climate_path = Path(folder) / 'climate_manifest.json'
        if not climate_path.exists():
            raise ValueError('Future weather requires a validated climate manifest')
        climate_record = json.loads(climate_path.read_text())
        if (climate_record.get('status') != 'validated'
                or climate_record.get('region') != cfg.get('id')
                or climate_record.get('projection_year') != year
                or climate_record.get('model') != climate.get('model')
                or climate_record.get('scenario') != climate.get('scenario')):
            raise ValueError('Climate manifest and planning metadata disagree')
    return year

def load_pack(name, policy="baseline"):
    folder=(ROOT/name).resolve() if not Path(name).is_absolute() else Path(name).resolve()
    cfg=json.loads((folder/'config.json').read_text())
    planning_year=planning_year_for(folder,cfg)
    hours=int(os.environ.get('GRID_EXPERIMENT_HOURS','8760'));start=int(os.environ.get('GRID_EXPERIMENT_START','0'))
    if hours<2 or start<0 or start+hours>8760:raise ValueError('Select at least two contiguous hours within the archived year')
    network=json.loads((folder/'network.json').read_text())
    frames={}
    for node in network['nodes']:
        df=pd.read_csv(folder/node['profiles_csv'],parse_dates=['time'])
        if len(df)!=8760:raise ValueError('Expected complete 8760-row prepared profiles')
        frames[node['id']]=df.iloc[start:start+hours].copy()
    idx=pd.DatetimeIndex(next(iter(frames.values())).time)
    if any(not pd.DatetimeIndex(x.time).equals(idx) for x in frames.values()):raise ValueError('Misaligned node profiles')
    costs=json.loads((ROOT/'shared'/cfg['cost_file']).read_text())
    costs={k:{a:b for a,b in v.items() if not a.startswith('_')} for k,v in costs.items() if not k.startswith('_')}
    ass=json.loads((folder/'assumptions.json').read_text())
    from policy import load_policy
    electricity_policy=load_policy(cfg['id'],policy)
    scenario=dict(year=2023,planning_year=planning_year,demand_MW=sum(x.demand_MW.to_numpy() for x in frames.values()),
                  solar_cf=np.mean([x.solar_cf for x in frames.values()],axis=0),wind_cf=np.mean([x.wind_cf for x in frames.values()],axis=0),
                  costs=costs,voll=ass['voll_usd_per_MWh'],distribution=cfg['distribution'],assumptions=ass,electricity_policy=electricity_policy,
                  objective_weight=8760/hours,solver_time_limit=float(os.environ.get('GRID_SOLVER_TIME_LIMIT','86400')))
    cap=cfg['global_new_capacity_limits_GW']
    ds={t:dict(type='bool',doc='Permit new '+t+' construction; zero build is allowed') for t in ['wind','solar','nuclear','battery','hydrogen']}
    defaults=dict(wind=True,solar=True,nuclear=True,battery=True,hydrogen=True,battery_hours=4.,dist_volt_var=False,dist_line_upgrade=False,dist_battery_b1_fraction=.4)
    ds['battery_hours']=dict(type='float',min=1,max=24,doc='NEW battery energy-to-power ratio in hours')
    ds['dist_volt_var']=dict(type='bool',doc='Enable representative PV reactive absorption')
    ds['dist_line_upgrade']=dict(type='bool',doc='Reinforce representative feeder; costs included')
    ds['dist_battery_b1_fraction']=dict(type='float',min=0,max=1,doc='NEW battery allocation at upstream representative bus')
    # CO2 is a world/policy assumption, not an agent-relaxable design choice.
    for t in ['wind','solar','nuclear']:
        ds[t+'_max_GW']=dict(type='float',min=0,max=cap[t],doc='Upper bound on total NEW '+t+' capacity')
        defaults[t+'_max_GW']=cap[t]
    for node in network['nodes']:
        bus=node['id']
        for t in ['wind','solar','nuclear','battery']:
            k=f'site_{bus}_{t}_max_MW';v=node['new_capacity_limits_MW'][t]
            ds[k]=dict(type='float',min=0,max=v,doc=f'NEW {t} capacity bound at {bus}; explore local siting constraints')
            defaults[k]=v
        for x in ['charge','discharge']:
            k=f'storage_{bus}_max_{x}_fraction';ds[k]=dict(type='float',min=0,max=1,doc='NEW battery dispatch envelope for local network constraints');defaults[k]=1.
        for key in ['dist_battery_b1_fraction','dist_line_upgrade','dist_volt_var']:
            k=key+'_'+bus;ds[k]=copy.deepcopy(ds[key]);defaults[k]=defaults[key]
    def validate(changes):
        if not isinstance(changes,dict):return ['design changes must be an object']
        err=[]
        for k,v in changes.items():
            spec=ds.get(k)
            if not spec:err.append('unknown design variable '+k)
            elif spec['type']=='bool' and not isinstance(v,bool):err.append(k+' must be boolean')
            elif spec['type']=='float' and (isinstance(v,bool) or not isinstance(v,(int,float)) or not np.isfinite(v) or not spec['min']<=v<=spec['max']):err.append(k+' outside fixed scenario bounds')
        return err
    summary=cfg['description']+'\n'+json.dumps(ass,sort_keys=True)+'\n'+f'{hours} chronological hours; '+('annual study.' if hours==8760 else 'SHORT-WINDOW VALIDATION, not annual reliability evidence.')
    summary+=f' Planning year: {planning_year}; profile/weather year: 2023. Official projects require a documented in-service year no later than the planning year; unknown dates are unavailable.'
    summary+='\nFixed electricity policy: '+json.dumps(electricity_policy,sort_keys=True)+'. Baseline reports target attainment without enforcing it; published enforces the target. The target year labels the source policy, not a forecast of demand or fleet.'
    summary+=f' Unmet-energy acceptance <= {ass["unserved_fraction_limit"]*scenario["demand_MW"].sum()/1000:.4f} GWh for this horizon, plus representative distribution validation. Modelled South African demand is not raw observation. Explain choices by citing simulation IDs and counterfactual checks; retain uncertainty and do not claim construction approval.'
    return SimpleNamespace(NAME=cfg['id'],DATA_DIR=folder,HOURS=hours,SNAPSHOTS=idx,SCENARIO=scenario,SCENARIO_SUMMARY=summary,DESIGN_SPACE=ds,DEFAULT_DESIGN=defaults,validate_changes=validate)
