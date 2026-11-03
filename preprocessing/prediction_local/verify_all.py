#!/usr/bin/env python3
"""Validate every prepared forecast against its source targets and archived hourly inputs."""
import json
import math
from pathlib import Path
import sys

import os
SOURCE_DIR = Path(__file__).resolve().parent
ROOT = Path(os.environ.get('GRID_DATA_ROOT', str(SOURCE_DIR.parents[1] / 'data'))).expanduser().resolve()
HERE = ROOT / 'prediction_local'
sys.path.insert(0, str(SOURCE_DIR.parent / 'prediction'))
from scenarios import REGIONS, read_csv, verify, dump


def check_all():
    index = json.loads((HERE / 'data/all_regions_index.json').read_text())
    expected = {(r, y) for r in REGIONS for y in (2030, 2035)}
    if len(index) != 16 or {(i['region'], i['year']) for i in index} != expected:
        raise ValueError('Expected exactly 8 regions x 2 forecast years')
    results = []
    for item in index:
        region, year = item['region'], item['year']
        pack = ROOT / item['pack']; manifest = verify(pack)
        meta = manifest['scenario']
        target = json.loads((HERE / f'data/{item["name"]}.json').read_text())
        if meta['planning_year'] != year or meta['region'] != region or meta['blocks'] != []:
            raise ValueError('Wrong year/region or unexpected separate load additions')
        if meta['forecast'] != target:
            raise ValueError('Pack forecast differs from current audited target')
        for file, digest in target['source_files'].items():
            from scenarios import sha
            if sha((HERE / 'data' / file).resolve()) != digest:
                raise ValueError(f'Source hash changed: {file}')
        network = json.loads((pack / 'network.json').read_text())
        zone_errors = {}; peak = 0.; hourly_total = [0.] * 8760
        for node in network['nodes']:
            z = node['id']; after = read_csv(pack / node['profiles_csv'])
            before = read_csv(ROOT / f'experiment/{region}' / node['profiles_csv'])
            if len(after) != 8760 or len({r['time'] for r in after}) != 8760:
                raise ValueError('Invalid hourly index')
            old_energy = math.fsum(float(r['demand_MW']) for r in before) / 1000
            mult = target['annual_GWh'][z] / old_energy
            for i, (old, new) in enumerate(zip(before, after)):
                if {k: v for k, v in old.items() if k != 'demand_MW'} != {k: v for k, v in new.items() if k != 'demand_MW'}:
                    raise ValueError('Time/weather/non-demand inputs changed')
                v = float(new['demand_MW'])
                if not math.isfinite(v) or v < 0 or not math.isclose(v, float(old['demand_MW']) * mult, rel_tol=1e-10, abs_tol=1e-8):
                    raise ValueError('Hourly demand does not match the source growth factor')
                hourly_total[i] += v
            actual = math.fsum(float(r['demand_MW']) for r in after) / 1000
            zone_errors[z] = actual - target['annual_GWh'][z]
        if max(abs(v) for v in zone_errors.values()) > 1e-6:
            raise ValueError('Annual input does not match target')
        combined = read_csv(pack / 'demand.csv')
        for i, row in enumerate(combined):
            if not math.isclose(sum(float(row[n['id']]) for n in network['nodes']), hourly_total[i], rel_tol=1e-12):
                raise ValueError('Combined demand table differs from zonal profiles')
        results.append(dict(region=region, year=year, zones=len(zone_errors), hours=8760,
                            annual_GWh=sum(target['annual_GWh'].values()), peak_MW=max(hourly_total),
                            max_annual_error_GWh=max(abs(v) for v in zone_errors.values()),
                            input_kind=item['input_kind'], passes=True))
    summary = dict(packs=16, zone_years=sum(r['zones'] for r in results), passes=True,
                   scope='Source targets, hourly transformation, provenance and unchanged weather; not solver feasibility or annual adequacy.', results=results)
    dump(HERE / 'validation/all_regions_inputs.json', summary)
    return summary


if __name__ == '__main__':
    result = check_all()
    print(json.dumps({k: v for k, v in result.items() if k != 'results'}, indent=2))
