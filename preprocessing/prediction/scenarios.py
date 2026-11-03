"""Build isolated input packs; never mutate archived inputs or infer DC MW units."""
import copy
import csv
import hashlib
import json
import math
from pathlib import Path
import shutil
import tempfile

import os
ROOT = Path(os.environ.get('GRID_DATA_ROOT', str(Path(__file__).resolve().parents[2] / 'data'))).expanduser().resolve()
REGIONS = ('miso', 'ercot', 'nyiso', 'south_africa', 'australia', 'new_zealand', 'mexico', 'brazil')

def read_csv(path):
    with Path(path).open(newline='') as f:
        return list(csv.DictReader(f))

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def dump(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n')

def write_csv(path, rows):
    with Path(path).open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

def number(value, label):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
        raise ValueError(f'{label} must be finite and nonnegative')
    return float(value)

def transform(original, multiplier, blocks):
    """Included blocks replace annual energy; additional blocks add annual energy."""
    original = [number(v, 'demand') for v in original]
    multiplier = number(multiplier, 'multiplier')
    energy = sum(original)
    if energy <= 0:
        raise ValueError('Zero baseline annual energy')
    included = 0.0
    addition = [0.0] * len(original)
    for treatment, values in blocks:
        if treatment not in ('included_in_forecast', 'additional_to_forecast'):
            raise ValueError('Explicit block treatment is required')
        if len(values) != len(original):
            raise ValueError('Block profile length mismatch')
        values = [number(v, 'block MW') for v in values]
        if treatment == 'included_in_forecast':
            included += sum(values)
        addition = [a + b for a, b in zip(addition, values)]
    residual = multiplier - included / energy
    if residual < -1e-12:
        raise ValueError('Included block energy exceeds the entire zonal forecast')
    return [max(0.0, residual) * v + a for v, a in zip(original, addition)], max(0.0, residual)

def build(spec_path, output, project=ROOT):
    spec_path, output, project = Path(spec_path).resolve(), Path(output).resolve(), Path(project).resolve()
    spec = json.loads(spec_path.read_text())
    if set(spec) - {'name', 'region', 'year', 'growth', 'blocks', 'forecast_file'}:
        raise ValueError('Unknown scenario fields')
    region, year, growth = spec['region'], spec['year'], spec['growth']
    if region not in REGIONS or isinstance(year, bool) or not isinstance(year, int):
        raise ValueError('Invalid region/year')
    if not ((growth == 'historical' and year == 2023) or (growth in ('baseline', 'high') and year in (2030, 2035))):
        raise ValueError('Use historical/2023 or baseline|high/2030|2035')
    if output.exists():
        raise ValueError(f'Refusing to overwrite {output}')
    source = project / 'experiment' / region
    summary_path = project / 'prediction_documents/derived/summary_2030_2035.csv'
    summary_rows = [r for r in read_csv(summary_path) if r['region'] == region]
    summary = {r['zone']: r for r in summary_rows}
    network = json.loads((source / 'network.json').read_text())
    zones = [n['id'] for n in network['nodes']]
    if len(summary) != len(summary_rows) or set(summary) != set(zones):
        raise ValueError('Summary must contain exactly one row per network zone')
    sources = {str(spec_path): sha(spec_path), str(summary_path): sha(summary_path)}
    forecast = None
    if 'forecast_file' in spec:
        forecast_path = (spec_path.parent / spec['forecast_file']).resolve()
        forecast = json.loads(forecast_path.read_text())
        if growth == 'historical' or any(forecast.get(k) != spec[k] for k in ('region', 'year', 'growth')):
            raise ValueError('Forecast region/year/growth does not match scenario')
        if set(forecast.get('annual_GWh', {})) != set(zones):
            raise ValueError('Forecast must contain exactly every network zone')
        if not forecast.get('citation') or not forecast.get('method') or not forecast.get('source_files'):
            raise ValueError('Forecast requires citation, method and hashed source files')
        for value in forecast['annual_GWh'].values():
            number(value, 'forecast annual_GWh')
        for name, digest in forecast['source_files'].items():
            p = (forecast_path.parent / name).resolve()
            if sha(p) != digest:
                raise ValueError(f'Forecast source hash mismatch: {name}')
            sources[str(p)] = digest
        sources[str(forecast_path)] = sha(forecast_path)
    frames, times = {}, None
    for n in network['nodes']:
        p = source / n['profiles_csv']
        sources[str(p)] = sha(p)
        frame = read_csv(p)
        ts = [r['time'] for r in frame]
        if len(frame) != 8760 or len(set(ts)) != 8760 or (times is not None and ts != times):
            raise ValueError('Expected aligned, unique 8760-hour profiles')
        frames[n['id']], times = frame, ts
    blocks, ids = {z: [] for z in zones}, set()
    for block in spec.get('blocks', []):
        allowed = {'id', 'zone', 'treatment', 'source', 'average_MW', 'annual_GWh', 'profile_csv'}
        if set(block) - allowed or not {'id', 'zone', 'treatment', 'source'} <= set(block):
            raise ValueError('Blocks require id, zone, treatment, source and one load representation')
        if not isinstance(block['id'], str) or not block['id'] or block['id'] in ids:
            raise ValueError('Block IDs must be unique nonempty strings')
        ids.add(block['id'])
        if block['zone'] not in zones or not isinstance(block['source'], str) or not block['source'].strip():
            raise ValueError('Block requires a valid string zone and source/assumption rationale')
        representations = set(block) & {'average_MW', 'annual_GWh', 'profile_csv'}
        if len(representations) != 1:
            raise ValueError('Specify exactly one of average_MW, annual_GWh, profile_csv; rated/peak MW is not average MW')
        if 'profile_csv' in block:
            p = (spec_path.parent / block['profile_csv']).resolve()
            rows = read_csv(p)
            if not rows or set(rows[0]) != {'time', 'load_MW'} or [r['time'] for r in rows] != times:
                raise ValueError('Block CSV must have time,load_MW and exactly match the 2023 template timestamps')
            values = [number(float(r['load_MW']), 'load_MW') for r in rows]
            sources[str(p)] = sha(p)
        else:
            mw = number(block['average_MW'], 'average_MW') if 'average_MW' in block else number(block['annual_GWh'], 'annual_GWh') * 1000 / 8760
            values = [mw] * 8760
        blocks[block['zone']].append((block['treatment'], values))
    audit = []
    for z, frame in frames.items():
        row = summary[z]
        multiplier = 1.0 if growth == 'historical' else float(row[f'mult_{year}_{growth}'] or 'nan')
        old = [float(r['demand_MW']) for r in frame]
        if abs(sum(old) / 1000 - float(row['energy_2023_GWh'])) > .051 or abs(max(old) - float(row['peak_2023_MW'])) > .051:
            raise ValueError(f'{z}: summary baseline no longer matches engine profiles')
        if forecast is not None:
            multiplier = forecast['annual_GWh'][z] * 1000 / sum(old)
        new, residual = transform(old, multiplier, blocks[z])
        for r, value in zip(frame, new):
            r['demand_MW'] = value
        audit.append(dict(zone=z, multiplier=multiplier, residual_multiplier=residual,
                          baseline_GWh=sum(old)/1000, forecast_GWh=sum(old)*multiplier/1000,
                          scenario_GWh=sum(new)/1000, peak_MW=max(new),
                          source=forecast['citation'] if forecast else (row['mult_source'] if growth != 'historical' else 'archived 2023 profiles'),
                          resolution=forecast.get('resolution', 'specified_zonal_targets') if forecast else row['resolution'],
                          caution=forecast['method'] if forecast else row['caution']))
    total = sum(r['scenario_GWh'] for r in audit)
    if total <= 0:
        raise ValueError('Scenario has zero total demand')
    for n in network['nodes']:
        n['demand_share'] = next(r['scenario_GWh'] for r in audit if r['zone'] == n['id']) / total
        n['profiles_csv'] = f"profiles/{n['id']}.csv"
    fixed_files = ['config.json', 'assumptions.json', 'inventory.json', 'network.json']
    cfg = json.loads((source/'config.json').read_text())
    fixed_paths = [source/f for f in fixed_files] + [project/'experiment/shared'/cfg['cost_file'], project/'experiment/shared/electricity_policies.json']
    original_network = json.loads((source/'network.json').read_text())
    fixed_paths += [source/n['profiles_csv'] for n in original_network['nodes']]
    fixed = {str(p.relative_to(project/'experiment')): sha(p) for p in fixed_paths}
    sources.update({str(p): sha(p) for p in fixed_paths})
    context = hashlib.sha256(json.dumps(fixed, sort_keys=True).encode()).hexdigest()
    metadata = dict(name=spec.get('name', spec_path.stem), region=region, planning_year=year,
                    weather_year=2023, growth=growth, blocks=spec.get('blocks', []),
                    fixed_context_sha256=context,
                    assumptions=['2023 weather and hourly shape retained; 8760-hour representative year, including future leap years.',
                                 'Existing fleet retained without retirement; costs, policies and build bounds unchanged.',
                                 'Published DC/large-load MW is never added automatically.',
                                 'Included blocks replace forecast annual energy; additional blocks are user-declared increments.'])
    if forecast is not None:
        metadata['forecast'] = forecast
    cfg['prediction'] = metadata
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix='.prediction-', dir=output.parent))
    try:
        (temp/'profiles').mkdir()
        for name in ('assumptions.json', 'inventory.json'):
            shutil.copy2(source/name, temp/name)
        dump(temp/'config.json', cfg)
        dump(temp/'network.json', network)
        for z, frame in frames.items():
            write_csv(temp/f'profiles/{z}.csv', frame)
        write_csv(temp/'demand.csv', [dict(time=t, **{z: frames[z][i]['demand_MW'] for z in zones}) for i,t in enumerate(times)])
        write_csv(temp/'demand_audit.csv', audit)
        manifest = dict(schema_version=1, scenario=metadata, sources=sources, fixed_inputs=fixed,
                        files={str(p.relative_to(temp)): sha(p) for p in sorted(temp.rglob('*')) if p.is_file()})
        dump(temp/'prediction_manifest.json', manifest)
        temp.rename(output)
    except BaseException:
        shutil.rmtree(temp)
        raise
    return manifest

def verify(pack, project=ROOT):
    pack = Path(pack).resolve()
    manifest = json.loads((pack/'prediction_manifest.json').read_text())
    for name, digest in manifest['files'].items():
        if sha(pack/name) != digest:
            raise ValueError(f'Generated input changed: {name}')
    for name, digest in manifest['fixed_inputs'].items():
        if sha(Path(project)/'experiment'/name) != digest:
            raise ValueError(f'Fixed reference input changed; rebuild pack: {name}')
    return manifest
