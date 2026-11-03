"""Calibrate the representative feeder section length of a pack so that the bare peak
load (no PV, no battery) sits inside the voltage band at about the target per-unit
voltage, following the convention of the US (0.96 pu at 12.47 kV, 2 km) and South
African (0.98 pu at 22 kV, 3 km) packs. Writes config.json in place.

Usage: python scripts/calibrate_feeders.py australia new_zealand mexico brazil [--target 0.96]
"""
from pathlib import Path
import sys, json, argparse
import numpy as np, pandas as pd
from types import SimpleNamespace as NS
from tqdm import tqdm
import os
ROOT = Path(os.environ.get('GRID_DATA_ROOT', str(Path(__file__).resolve().parents[3] / 'data'))).expanduser().resolve() / 'experiment'
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'models/sol/runtime_original/engine'))
from distribution import feeder_sweep


def proxy_network(peak):
    idx = pd.date_range('2023-01-01', periods=4, freq='h')
    g = pd.DataFrame({'bus': ['n'], 'p_nom_opt': [0.]}, index=['solar::n'])
    gt = NS(p=pd.DataFrame({'solar::n': np.zeros(4)}, index=idx))
    s = pd.DataFrame({'bus': ['n'], 'p_nom_opt': [0.], 'max_hours': [4.]}, index=['battery::n'])
    st = NS(p=pd.DataFrame({'battery::n': np.zeros(4)}, index=idx), state_of_charge=pd.DataFrame({'battery::n': np.zeros(4)}, index=idx))
    return NS(snapshots=idx, meta={'planning_assets': {}}, generators=g, generators_t=gt, storage_units=s, storage_units_t=st), np.array([peak, peak * .9, peak * .5, peak * .3])


def worst_low(dist, km, design):
    dist = dict(dist, line_km=km)
    net, demand = proxy_network(1000.)
    return feeder_sweep(net, demand, dist, design, 0.01)['worst_low_pu']


def calibrate(region, target):
    cfg_path = ROOT / region / 'config.json'
    cfg = json.loads(cfg_path.read_text()); dist = cfg['distribution']
    design = dict(dist_battery_b1_fraction=.4, dist_line_upgrade=False, dist_volt_var=False)
    lo, hi = 0.2, 12.0
    for _ in tqdm(range(30), desc=f'{region} feeder'):
        mid = (lo + hi) / 2
        v = worst_low(dist, mid, design)
        if v > target:
            lo = mid
        else:
            hi = mid
    km = round((lo + hi) / 2, 2)
    v = worst_low(dist, km, design)
    dist['line_km'] = km
    dist['calibration'] = dict(target_bare_peak_pu=target, achieved_bare_peak_pu=round(float(v), 4), method='bisection on section length with bare peak load, no PV or battery')
    cfg['distribution'] = dist
    cfg_path.write_text(json.dumps(cfg, indent=2) + '\n')
    print(region, dist['feeder_kv'], 'kV ->', km, 'km, bare peak', round(float(v), 4), 'pu', flush=True)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(); ap.add_argument('regions', nargs='+'); ap.add_argument('--target', type=float, default=0.96)
    a = ap.parse_args()
    for r in a.regions:
        calibrate(r, a.target)
