#!/usr/bin/env python3
"""Independently audit NASA-derived artifacts; never invoke a solver or API.

No builder modules are imported. Re-read delivered CSVs, original NetCDF values,
recorded hashes, existing demand packs, and independently recomputed solar
geometry. Full default selection writes independent_audit.json and
annual_summary.csv in validation/. A filtered run uses *_partial filenames and
cannot claim full-bundle completeness.
"""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True
# Loading a sibling explicitly also supports importlib-based offline validators.
import importlib.util as _importlib_util
_spec = _importlib_util.spec_from_file_location("_grid_nasa_paths", Path(__file__).resolve().with_name("_paths.py"))
_paths = _importlib_util.module_from_spec(_spec)
_spec.loader.exec_module(_paths)
RELEASE_ROOT = _paths.RELEASE_ROOT
PROJECT = _paths.data_root()
BUNDLE = PROJECT / 'nasa_climate'

def configure_roots(data_root=None):
    global PROJECT, BUNDLE
    PROJECT = _paths.data_root(data_root)
    BUNDLE = PROJECT / 'nasa_climate'
    HASHES.clear(); VERIFIED.clear(); RAW_VERIFIED.clear(); SOURCE_PACKS_VERIFIED.clear()
    return PROJECT

def _load_science():
    global np, pd, netCDF4, pvlib
    import numpy as np
    import pandas as pd
    import netCDF4
    import pvlib
MODELS = ['ACCESS-CM2', 'MPI-ESM1-2-HR', 'MRI-ESM2-0']
YEARS = [2030, 2035]
REGIONS = ['miso', 'ercot', 'nyiso', 'south_africa', 'australia', 'new_zealand', 'mexico', 'brazil']
SCENARIO = 'ssp245'
KIND = 'synthetic_hourly_nasa_daily_with_era5_template'
VARIABLES = {'tasmin': ('tasmin_K', 'K'), 'tasmax': ('tasmax_K', 'K'),
             'rsds': ('rsds_W_m2', 'W m-2'), 'sfcWind': ('sfcWind_m_s', 'm s-1')}
HASHES = {}
VERIFIED = {}
RAW_VERIFIED = set()
SOURCE_PACKS_VERIFIED = set()


def require(condition, message):
    if not bool(condition):
        raise ValueError(message)


def read_json(path):
    return json.loads(Path(path).read_text())


def digest(path):
    path = Path(path).resolve()
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    if key not in HASHES:
        h = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                h.update(chunk)
        HASHES[key] = h.hexdigest()
    return HASHES[key]


def verify_hash(path, expected):
    path = _paths.resolve_reference(path, PROJECT)
    require(digest(path) == expected, f'SHA256 mismatch: {path}')
    VERIFIED[str(path)] = expected
    return path


def check_record(record, expected_path=None):
    path = verify_hash(record['path'], record['sha256'])
    if expected_path is not None:
        require(path == Path(expected_path).resolve(), f'Unexpected provenance path: {path}')
    return path


def finite(frame, columns):
    values = frame[columns].to_numpy(dtype=float)
    require(np.isfinite(values).all(), f'Missing/nonfinite values in {columns}')
    return values


def close(actual, expected, tolerance, name):
    a, e = np.asarray(actual, dtype=float), np.asarray(expected, dtype=float)
    require(a.shape == e.shape, f'{name}: shapes differ {a.shape}/{e.shape}')
    require(np.isfinite(a).all() and np.isfinite(e).all(), f'{name}: nonfinite values')
    error = float(np.max(np.abs(a - e))) if a.size else 0.0
    require(error <= tolerance, f'{name}: maximum error {error:g} > {tolerance:g}')
    return error


def node_table(region):
    return pd.read_csv(PROJECT / 'experiment' / region / 'nodes.csv', dtype={'id': str})


def source_pack(region, year):
    if (region, year) == ('south_africa', 2030):
        return PROJECT / 'corrections/20260925-v2/packs/south_africa_2030'
    suffix = 'official' if region == 'nyiso' else 'baseline'
    return PROJECT / 'prediction_local/packs' / f'{region}_{year}_{suffix}'


def verify_source_pack(pack):
    if str(pack) in SOURCE_PACKS_VERIFIED:
        return
    manifest = read_json(pack / 'prediction_manifest.json')
    for name, sha in manifest['files'].items():
        verify_hash(pack / name, sha)
    correction = pack / 'correction_manifest.json'
    if correction.exists():
        for name, sha in read_json(correction)['corrected_input_sha256'].items():
            verify_hash(pack / name, sha)
    SOURCE_PACKS_VERIFIED.add(str(pack))


def load_daily(model, year):
    """Trace every normalized value to its selected raw NetCDF grid cell."""
    path = BUNDLE / 'processed/daily' / model / SCENARIO / f'{year}.csv'
    meta = read_json(path.with_suffix('.provenance.json'))
    for key, expected in {'dataset': 'NASA NEX-GDDP-CMIP6', 'version': '2.0',
                          'model': model, 'scenario': SCENARIO, 'projection_year': year,
                          'member': 'r1i1p1f1', 'source_frequency': 'daily'}.items():
        require(meta.get(key) == expected, f'Daily provenance {key} mismatch: {path}')
    verify_hash(path, meta['daily_csv_sha256'])
    daily = pd.read_csv(path, dtype={'region': str, 'node': str})
    require(not daily.duplicated(['region', 'node', 'date']).any(), 'Duplicate normalized daily rows')
    numeric = ['latitude', 'longitude', 'nasa_latitude', 'nasa_longitude'] + [v[0] for v in VARIABLES.values()]
    finite(daily, numeric)
    expected_days = pd.date_range(f'{year - 1}-12-31', f'{year + 1}-01-01').strftime('%Y-%m-%d').tolist()
    mappings = {(r['region'], str(r['node'])): r for r in meta['node_mapping']}
    require(len(mappings) == len(meta['node_mapping']), 'Duplicate NASA node mappings')
    groups = {key: df.set_index('date').sort_index() for key, df in daily.groupby(['region', 'node'])}
    require(set(groups) == set(mappings), 'Daily rows and grid mapping node sets differ')
    for key, group in groups.items():
        require(group.index.tolist() == expected_days, f'Wrong NASA buffered calendar: {key}')
        mapping = mappings[key]
        require(mapping['model'] == model and mapping['year'] == year and mapping['scenario'] == SCENARIO,
                f'Wrong grid mapping identity: {key}')
        for name in ['latitude', 'longitude', 'nasa_latitude', 'nasa_longitude']:
            close(group[name], np.full(len(group), mapping[name]), 1e-9, f'Mapping {name} {key}')
        require(0 <= mapping['approx_distance_km'] <= 75, f'Invalid grid distance: {key}')
        require((group.tasmin_K <= group.tasmax_K).all(), f'tasmin > tasmax: {key}')
        require((group.rsds_W_m2 >= 0).all() and (group.sfcWind_m_s >= 0).all(), f'Negative raw target: {key}')
    raw_paths = [item['path'] for item in meta['raw_inputs']]
    require(len(set(raw_paths)) == len(raw_paths), 'Duplicate raw provenance paths')
    regions = sorted(set(daily.region))
    expected_raw = {f'raw/nasa/{model}/{SCENARIO}/{year}/{region}/{variable}_{segment}.nc'
                    for region in regions for variable in VARIABLES for segment in ['previous', 'annual', 'next']}
    require(set(raw_paths) == expected_raw, 'Incomplete/extraneous raw NetCDF provenance inventory')
    for item in meta['raw_inputs']:
        raw = verify_hash(BUNDLE / item['path'], item['sha256'])
        download = read_json(raw.with_suffix(raw.suffix + '.download.json'))
        require(download['sha256'] == item['sha256'] and download['bytes'] == raw.stat().st_size,
                f'Raw download record mismatch: {raw}')
        require('ds.nccs.nasa.gov/thredds/' in download['url'] and f'/{model}/{SCENARIO}/r1i1p1f1/' in download['url'],
                f'Wrong NASA download identity: {raw}')
        variable, segment = raw.stem.rsplit('_', 1)
        column, units = VARIABLES[variable]
        region = raw.parent.name
        with netCDF4.Dataset(raw) as dataset:
            value = dataset.variables[variable]
            time = dataset.variables['time']
            calendar = getattr(time, 'calendar', 'standard')
            require(calendar in ['standard', 'gregorian', 'proleptic_gregorian'], f'Unsupported raw calendar: {raw}')
            require(str(dataset.getncattr('version')) == '2.0' and getattr(dataset, 'frequency', '') == 'day', f'Wrong raw version/frequency: {raw}')
            require(getattr(value, 'units', '') == units and value.dimensions == ('time', 'lat', 'lon'), f'Wrong raw units/dimensions: {raw}')
            require(item['metadata']['variable_attributes']['units'] == units and item['metadata']['calendar'] == calendar,
                    f'Saved CF metadata mismatch: {raw}')
            title = str(getattr(dataset, 'title', ''))
            require(model in title and SCENARIO in title and 'r1i1p1f1' in title, f'Wrong raw model/member/scenario: {raw}')
            dates = [d.strftime('%Y-%m-%d') for d in netCDF4.num2date(time[:], time.units, calendar=calendar)]
            expected_segment = {'previous': expected_days[:1], 'annual': expected_days[1:-1], 'next': expected_days[-1:]}[segment]
            require(dates == expected_segment, f'Raw daily calendar mismatch: {raw}')
            lat = np.asarray(dataset.variables['lat'][:], dtype=float)
            lon = (np.asarray(dataset.variables['lon'][:], dtype=float) + 180) % 360 - 180
            data = np.ma.filled(value[:], np.nan).astype(float)
            for key, mapping in mappings.items():
                if key[0] != region:
                    continue
                iy = np.flatnonzero(np.isclose(lat, mapping['nasa_latitude'], atol=1e-9, rtol=0))
                ix = np.flatnonzero(np.isclose(lon, mapping['nasa_longitude'], atol=1e-9, rtol=0))
                require(len(iy) == len(ix) == 1, f'Selected cell absent from raw grid: {raw}/{key}')
                close(groups[key].loc[dates, column], data[:, iy[0], ix[0]], 1e-9, f'Raw selected values {raw}/{key}')
        RAW_VERIFIED.add(str(raw))
    return daily, path, meta


def audit_node(model, year, region, node, daily, daily_path):
    bus = str(node.id)
    stem = Path(model) / SCENARIO / str(year) / region / bus
    hourly_path = BUNDLE / 'hourly' / stem.with_suffix('.csv')
    profile_path = BUNDLE / 'profiles' / stem.with_suffix('.csv')
    provenance_path = profile_path.with_suffix('.provenance.json')
    pr = read_json(provenance_path)
    identity = {'status': 'validated', 'source_kind': KIND, 'model': model, 'scenario': SCENARIO,
                'projection_year': year, 'region': region, 'node': bus}
    for key, expected in identity.items():
        require(pr.get(key) == expected, f'Wrong hourly provenance {key}: {stem}')
    check_record(pr['hourly_file'], hourly_path)
    check_record(pr['profile_file'], profile_path)
    validation_path = check_record(pr['validation_file'], BUNDLE / 'validation' / stem.with_suffix('.json'))
    validation = read_json(validation_path)
    for key, expected in identity.items():
        require(validation.get(key) == expected, f'Wrong validation identity {key}: {stem}')
    require(pr['summary'] == validation['summary'] and pr['grid_selection'] == validation['grid_selection'], f'Provenance/validation disagree: {stem}')
    require(pr['source_files'] == validation['source_files'], f'Provenance source inventory disagreement: {stem}')
    sources = {}
    for record in pr['source_files']:
        path = check_record(record)
        require(record['role'] not in sources, f'Duplicate source role: {stem}')
        sources[record['role']] = path
    pack = source_pack(region, year)
    required_sources = {'NASA_normalized_daily': daily_path, 'NASA_daily_extraction_provenance': daily_path.with_suffix('.provenance.json'),
                        'representative_nodes': PROJECT / 'experiment' / region / 'nodes.csv',
                        'ERA5_original_hourly_template': PROJECT / 'experiment' / region / 'raw/weather' / f'{bus}.json',
                        'ERA5_10m_and_boundary_companion': BUNDLE / 'raw/era5_10m' / region / f'{bus}.json',
                        'future_demand': pack / 'demand.csv',
                        'NREL_wind_power_curve': PROJECT / 'experiment/shared/raw/NREL_Reference_4MW_150_powercurve.csv',
                        'processing_code': Path(__file__).resolve().with_name('build_hourly.py')}
    require(sources == {k: v.resolve() for k, v in required_sources.items()}, f'Wrong source files: {stem}')
    verify_source_pack(pack)
    raw_era = read_json(sources['ERA5_original_hourly_template'])
    companion = read_json(sources['ERA5_10m_and_boundary_companion'])
    for key in ['latitude', 'longitude', 'elevation', 'utc_offset_seconds']:
        require(raw_era.get(key) == companion.get(key), f'ERA5 original/companion metadata differ: {key}/{stem}')
    companion_download = read_json(sources['ERA5_10m_and_boundary_companion'].with_suffix('.json.download.json'))
    verify_hash(sources['ERA5_10m_and_boundary_companion'], companion_download['sha256'])
    h = pd.read_csv(hourly_path)
    p = pd.read_csv(profile_path)
    baseline = pd.read_csv(pack / 'profiles' / f'{bus}.csv')
    demand = pd.read_csv(pack / 'demand.csv')
    require(len(h) == len(p) == len(baseline) == len(demand) == 8760, f'Wrong hourly length: {stem}')
    require(list(p.columns) == ['time', 'demand_MW', 'solar_cf', 'wind_cf'], f'Wrong profile columns: {stem}')
    expected_local = pd.date_range('2023-01-01', periods=8760, freq='h')
    projection = pd.date_range(f'{year}-01-01', periods=8760, freq='h')
    raw_offset = getattr(node, 'utc_offset_hours', np.nan)
    offset = float(raw_offset) if pd.notna(raw_offset) else (2.0 if region == 'south_africa' else 0.0)
    require(offset == int(offset), f'Unsupported fractional UTC offset: {stem}')
    utc = projection - pd.Timedelta(hours=offset)
    source_clock = pd.DatetimeIndex([t.replace(year=t.year - year + 2023) for t in utc])
    for frame, key, expected in [(h, 'time', expected_local), (p, 'time', expected_local), (baseline, 'time', expected_local),
                                  (demand, 'time', expected_local), (h, 'projection_time', projection),
                                  (h, 'projection_utc_time', utc), (h, 'source_utc_time', source_clock)]:
        actual = pd.DatetimeIndex(pd.to_datetime(frame[key]))
        require(actual.equals(expected) and actual.is_unique, f'Wrong {key} clock: {stem}')
    require(h.nasa_date.tolist() == utc.strftime('%Y-%m-%d').tolist(), f'NASA UTC day labels differ: {stem}')
    point = pr['point']
    require(point['utc_offset_hours'] == offset and point['latitude'] == float(node.latitude) and point['longitude'] == float(node.longitude), f'Wrong representative point: {stem}')
    require(point['elevation_m'] == raw_era['elevation'], f'Wrong PV elevation: {stem}')
    columns = ['temperature_2m_C', 'wind_speed_10m_m_s', 'wind_speed_100m_m_s', 'ghi_W_m2', 'dni_W_m2', 'dhi_W_m2', 'solar_zenith_deg', 'toa_horizontal_W_m2']
    finite(h, columns)
    finite(p, ['demand_MW', 'solar_cf', 'wind_cf'])
    require((h[['wind_speed_10m_m_s', 'wind_speed_100m_m_s', 'ghi_W_m2', 'dni_W_m2', 'dhi_W_m2', 'toa_horizontal_W_m2']] >= -1e-9).all().all(), f'Negative physical quantity: {stem}')
    require(p.solar_cf.between(0, 1).all() and p.wind_cf.between(0, 1).all() and (p.demand_MW >= 0).all(), f'Invalid demand/CF bounds: {stem}')
    demand_error = max(close(p.demand_MW, baseline.demand_MW, 1e-8, f'Demand vs source profile {stem}'),
                       close(p.demand_MW, demand[bus], 1e-8, f'Demand vs source demand CSV {stem}'))
    midpoint = (utc + pd.Timedelta(minutes=30)).tz_localize('UTC')
    geom = pvlib.solarposition.get_solarposition(midpoint, float(node.latitude), float(node.longitude), altitude=point['elevation_m'])
    close(h.solar_zenith_deg, geom.zenith, 1e-7, f'Solar geometry {stem}')
    cosine = np.maximum(np.cos(np.deg2rad(geom.zenith.to_numpy())), 0)
    extra = np.asarray(pvlib.irradiance.get_extra_radiation(midpoint))
    cap = extra * cosine
    close(h.toa_horizontal_W_m2, cap, 1e-7, f'TOA geometry {stem}')
    require((h.ghi_W_m2.to_numpy() <= cap + 1e-6).all() and (h.dni_W_m2.to_numpy() <= extra + 1e-6).all(), f'Radiation exceeds TOA cap: {stem}')
    closure_error = close(h.ghi_W_m2, h.dhi_W_m2.to_numpy() + h.dni_W_m2.to_numpy() * cosine, 1e-6, f'Radiation closure {stem}')
    night = (geom.zenith.to_numpy() >= 90) | (h.ghi_W_m2.to_numpy() <= 1e-9)
    night_cf = float(p.solar_cf.to_numpy()[night].max()) if night.any() else 0
    require(night_cf <= 1e-9, f'Positive solar CF at night/zero GHI: {stem}')
    require((h.ghi_W_m2.to_numpy()[geom.zenith.to_numpy() >= 90] <= 1e-9).all(), f'Positive nighttime GHI: {stem}')
    curve = pd.read_csv(sources['NREL_wind_power_curve'])
    wind = np.interp(h.wind_speed_100m_m_s, curve.iloc[:, 0], curve.iloc[:, 1], left=0, right=0) / 4000
    close(p.wind_cf, wind, 1e-10, f'Wind curve {stem}')
    target = daily[(daily.region == region) & (daily.node == bus)].set_index('date')
    require(len(target) == 367, f'Wrong NASA node daily count: {stem}')
    checks_list = validation['daily_conservation']
    checks = {row['date']: row for row in checks_list}
    require(len(checks) == len(checks_list) and set(checks) == set(h.nasa_date), f'Wrong saved daily-check dates: {stem}')
    full_days, partial_days, flags = 0, 0, Counter()
    max_temp_error = max_wind_error = max_energy_error = 0.0
    capped_complete = 0
    for date, group in h.groupby('nasa_date', sort=True):
        nasa = target.loc[date]
        saved = checks[date]
        require(saved['source_date'] == pd.Timestamp(date).replace(year=pd.Timestamp(date).year - year + 2023).strftime('%Y-%m-%d'), f'Wrong saved template date: {stem}/{date}')
        close([saved['target_rsds_W_m2'], saved['target_sfcWind_m_s']], [nasa.rsds_W_m2, nasa.sfcWind_m_s], 1e-9, f'Saved NASA targets {stem}/{date}')
        require(abs(saved['temperature_min_error_C']) <= 1e-7 and abs(saved['temperature_max_error_C']) <= 1e-7 and abs(saved['wind_10m_mean_error_m_s']) <= 1e-7 and abs(saved['solar_daily_energy_error_Wh_m2']) <= 1e-5, f'Saved daily conservation fails: {stem}/{date}')
        flags.update(saved['flags'])
        if len(group) != 24:
            partial_days += 1
            continue
        full_days += 1
        observed = [float(group.temperature_2m_C.min()), float(group.temperature_2m_C.max()), float(group.ghi_W_m2.mean()), float(group.wind_speed_10m_m_s.mean())]
        wanted = [float(nasa.tasmin_K - 273.15), float(nasa.tasmax_K - 273.15), float(nasa.rsds_W_m2), float(nasa.sfcWind_m_s)]
        errors = np.abs(np.asarray(observed) - wanted)
        close(observed, wanted, 1e-7, f'Independent complete UTC-day constraints {stem}/{date}')
        max_temp_error = max(max_temp_error, errors[0], errors[1])
        max_wind_error = max(max_wind_error, errors[3])
        max_energy_error = max(max_energy_error, abs(float(group.ghi_W_m2.sum() - nasa.rsds_W_m2 * 24)))
        close([saved['reconstructed_rsds_W_m2'], saved['reconstructed_10m_wind_mean_m_s'], saved['reconstructed_temperature_mean_C'], saved['maximum_100m_wind_m_s']],
              [group.ghi_W_m2.mean(), group.wind_speed_10m_m_s.mean(), group.temperature_2m_C.mean(), group.wind_speed_100m_m_s.max()], 1e-7, f'Saved vs delivered aggregates {stem}/{date}')
        caps = int(((group.toa_horizontal_W_m2 > 0) & np.isclose(group.ghi_W_m2, group.toa_horizontal_W_m2, atol=1e-6, rtol=0)).sum())
        require(caps == saved['toa_cap_saturated_hours'], f'Saved TOA-cap count differs: {stem}/{date}')
        capped_complete += caps
    summary = pr['summary']
    close([summary['solar_cf_mean'], summary['wind_cf_mean'], summary['temperature_mean_C'], summary['ghi_annual_Wh_m2']],
          [p.solar_cf.mean(), p.wind_cf.mean(), h.temperature_2m_C.mean(), h.ghi_W_m2.sum()], 1e-6, f'Saved annual summary {stem}')
    require(summary['hours'] == 8760 and summary['complete_utc_days_validated'] == len(checks), f'Saved count mismatch: {stem}')
    require(summary['days_with_flags'] == sum(bool(c['flags']) for c in checks_list), f'Saved flag count differs: {stem}')
    return {'model': model, 'scenario': SCENARIO, 'projection_year': year, 'region': region, 'node': bus,
            'hours': 8760, 'utc_offset_hours': int(offset), 'independently_verified_complete_utc_days': full_days,
            'partial_boundary_utc_days': partial_days, 'demand_max_abs_error_MW': demand_error,
            'solar_cf_mean': float(p.solar_cf.mean()), 'baseline_solar_cf_mean': float(baseline.solar_cf.mean()),
            'solar_cf_change_percentage_points': float((p.solar_cf.mean() - baseline.solar_cf.mean()) * 100),
            'wind_cf_mean': float(p.wind_cf.mean()), 'baseline_wind_cf_mean': float(baseline.wind_cf.mean()),
            'wind_cf_change_percentage_points': float((p.wind_cf.mean() - baseline.wind_cf.mean()) * 100),
            'temperature_mean_C': float(h.temperature_2m_C.mean()), 'ghi_annual_Wh_m2': float(h.ghi_W_m2.sum()),
            'maximum_100m_wind_m_s': float(h.wind_speed_100m_m_s.max()), 'maximum_ghi_W_m2': float(h.ghi_W_m2.max()),
            'max_temperature_extrema_error_C': float(max_temp_error), 'max_daily_wind_mean_error_m_s': float(max_wind_error),
            'max_daily_radiation_energy_error_Wh_m2': float(max_energy_error), 'max_solar_closure_error_W_m2': closure_error,
            'maximum_night_or_zero_GHI_solar_cf': night_cf,
            'toa_capped_hours_delivered': int(((h.toa_horizontal_W_m2 > 0) & np.isclose(h.ghi_W_m2, h.toa_horizontal_W_m2, atol=1e-6, rtol=0)).sum()),
            'toa_capped_hours_complete_days': capped_complete,
            'flagged_days_including_complete_boundary_reconstruction': summary['days_with_flags'],
            'flag_counts': dict(flags), 'node_flags': summary['node_flags'],
            'maximum_daily_wind_scaling_ratio': max(c['daily_wind_scaling_ratio'] for c in checks_list),
            'minimum_daily_wind_scaling_ratio': min(c['daily_wind_scaling_ratio'] for c in checks_list)}


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n')
    temp.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--models', nargs='+', choices=MODELS, default=['MPI-ESM1-2-HR'])
    parser.add_argument('--years', nargs='+', type=int, choices=YEARS, default=YEARS)
    parser.add_argument('--regions', nargs='+', choices=REGIONS, default=REGIONS)
    parser.add_argument('--nodes', nargs='+')
    _paths.add_data_root(parser)
    args = parser.parse_args()
    configure_roots(args.data_root)
    if args.plan:
        print(json.dumps({"stage": "independent_audit", "selection": _paths.selection_json(args), "data_root": str(PROJECT), "climate_root": str(BUNDLE), "solver_executed": False, "model_api_executed": False, "files_written": False}, indent=2))
        return 0
    _load_science()
    all_regions_years = set(args.years) == set(YEARS) and set(args.regions) == set(REGIONS) and args.nodes is None
    paper = set(args.models) == {'MPI-ESM1-2-HR'} and all_regions_years
    full = set(args.models) == set(MODELS) and all_regions_years
    suffix = '_three_gcm' if full else '' if paper else '_partial'
    report_path = BUNDLE / 'validation' / f'independent_audit{suffix}.json'
    annual_path = BUNDLE / 'validation' / f'annual_summary{suffix}.csv'
    results, errors = [], []
    selected = {region: [node for node in node_table(region).itertuples() if not args.nodes or node.id in args.nodes] for region in args.regions}
    expected = len(set(args.models)) * len(set(args.years)) * sum(len(nodes) for nodes in selected.values())
    require(expected > 0, 'No requested nodes matched')
    if full or paper:
        require(sum(len(nodes) for nodes in selected.values()) == 58 and expected == (348 if full else 116), 'Unexpected full-project node inventory')
    for model in dict.fromkeys(args.models):
        for year in dict.fromkeys(args.years):
            try:
                daily, path, _ = load_daily(model, year)
            except Exception as exc:
                error = {'model': model, 'projection_year': year, 'stage': 'raw_daily_trace', 'error': str(exc)}
                errors.append(error)
                print(json.dumps(error), flush=True)
                continue
            for region, nodes in selected.items():
                for node in nodes:
                    try:
                        results.append(audit_node(model, year, region, node, daily, path))
                    except Exception as exc:
                        error = {'model': model, 'projection_year': year, 'region': region, 'node': str(node.id), 'stage': 'hourly_artifact', 'error': str(exc)}
                        errors.append(error)
                        print(json.dumps(error), flush=True)
            print(json.dumps({'audit_progress': f'{model}/{year}', 'nodes_passed': len(results), 'errors': len(errors)}), flush=True)
    passed = not errors and len(results) == expected
    flag_counts = Counter()
    for row in results:
        flag_counts.update(row['flag_counts'])
    report = {'schema_version': 1, 'status': 'passed' if passed else 'failed', 'created_utc': datetime.now(timezone.utc).isoformat(),
              'scope': 'all_348_annual_node_scenarios' if full else 'all_116_paper_annual_node_scenarios' if paper else 'selected_partial_bundle', 'selection': _paths.selection_json(args),
              'expected_annual_node_scenarios': expected, 'verified_annual_node_scenarios': len(results),
              'verified_hours': sum(row['hours'] for row in results), 'verified_raw_netcdf_files': len(RAW_VERIFIED),
              'verified_upstream_and_artifact_hashes': len(VERIFIED), 'verified_authoritative_source_packs': len(SOURCE_PACKS_VERIFIED),
              'complete_utc_days_independently_reaggregated': sum(row['independently_verified_complete_utc_days'] for row in results),
              'partial_boundary_utc_days': sum(row['partial_boundary_utc_days'] for row in results),
              'comparison_scope': 'Differences versus the ERA5-2023 baseline combine climate-model realization, data-product differences, synthetic disaggregation and any climate signal; they do not isolate the effect of climate change.',
              'boundary_validation_scope': 'Partial UTC-day segments are excluded from independent daily conservation. Their saved full-day claims, NASA targets and tolerances are checked; only delivered hours are independently inspected.',
              'checks': ['All normalized daily values match selected raw NetCDF cells; source version, calendar, units, model/member/scenario and download hashes verified.',
                         'All delivered timestamps, projection clocks, source template clocks and fixed UTC offsets verified.',
                         'All original inputs match recorded upstream hashes and authoritative source-pack manifests; demand compared to both original profiles and demand CSVs.',
                         'Every complete delivered UTC day independently conserves NASA temperature extrema, mean radiation and near-surface wind.',
                         'Solar geometry independently recalculated; nonnegative radiation, TOA caps, decomposition closure, nighttime PV, CF bounds and wind-curve application verified.'],
              'flags': dict(flag_counts), 'toa_capped_hours_delivered': sum(row['toa_capped_hours_delivered'] for row in results),
              'maximum_100m_wind_m_s': max((row['maximum_100m_wind_m_s'] for row in results), default=None),
              'errors': errors, 'node_results': results, 'verified_file_sha256': VERIFIED,
              'audit_code': {'path': str(Path(__file__).resolve()), 'sha256': digest(__file__)},
              'software': {'numpy': np.__version__, 'pandas': pd.__version__, 'netCDF4': netCDF4.__version__, 'pvlib': pvlib.__version__},
              'solver_executed': False, 'model_api_executed': False}
    if passed:
        rows = [{**row, 'flag_counts': json.dumps(row['flag_counts'], sort_keys=True), 'node_flags': json.dumps(row['node_flags'])} for row in results]
        annual_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = annual_path.with_name(annual_path.name + f'.{os.getpid()}.tmp')
        pd.DataFrame(rows).to_csv(temporary, index=False)
        temporary.replace(annual_path)
        report['annual_summary'] = {'path': str(annual_path), 'sha256': digest(annual_path)}
    atomic_json(report_path, report)
    print(json.dumps({'status': report['status'], 'scope': report['scope'], 'nodes': len(results), 'expected': expected,
                      'raw_netcdf': len(RAW_VERIFIED), 'errors': len(errors), 'report': str(report_path)}), flush=True)
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
