#!/usr/bin/env python3
"""Extract full baseline demand forecasts and build 8 regions x 2030/2035 inputs.

Extraction needs openpyxl, pypdf and pyxlsb. Building from saved JSON needs stdlib only.
No model/API/solver execution is performed here.
"""
import argparse
import csv
import io
import json
import math
from pathlib import Path
import re
import sys
import zipfile

import os
SOURCE_DIR = Path(__file__).resolve().parent
ROOT = Path(os.environ.get('GRID_DATA_ROOT', str(SOURCE_DIR.parents[1] / 'data'))).expanduser().resolve()
HERE = ROOT / 'prediction_local'
DOCS = ROOT / 'prediction_documents'
sys.path.insert(0, str(SOURCE_DIR.parent / 'prediction'))
from scenarios import REGIONS, build, dump, read_csv, sha, verify
from prepare_nyiso import extract as extract_nyiso

YEARS = (2030, 2035)
SELECTION = {'miso': 'current', 'ercot': 'ltlf2025_ercot_adjusted_hourly',
             'australia': 'step_change', 'new_zealand': 'medium', 'mexico': 'planeacion_2024',
             'brazil': 'pde_2035_referencia', 'south_africa': 'REF'}


def baseline(region):
    network = json.loads((ROOT / f'experiment/{region}/network.json').read_text())
    frames = {n['id']: read_csv(ROOT / 'experiment' / region / n['profiles_csv']) for n in network['nodes']}
    return frames, {z: math.fsum(float(r['demand_MW']) for r in rows) / 1000 for z, rows in frames.items()}


def original_reference(path):
    for index in DOCS.glob('*/sources.json'):
        def walk(x):
            if isinstance(x, dict):
                if x.get('filename') == path.name and 'url' in x:
                    yield x
                for v in x.values():
                    yield from walk(v)
            elif isinstance(x, list):
                for v in x:
                    yield from walk(v)
        matches = list(walk(json.loads(index.read_text())))
        if matches:
            r = matches[0]
            return {k: r.get(k) for k in ('publisher', 'title', 'year', 'url')}
    raise ValueError(f'Missing citation for {path}')


def evidence(region, path, values, details, method, kind='published_annual_energy'):
    return dict(region=region, source_file=str(path.relative_to(ROOT)), source_sha256=sha(path),
                reference=original_reference(path), annual_GWh=values, details=details,
                method=method, input_kind=kind)


def extract_other_regions():
    from openpyxl import load_workbook
    from pypdf import PdfReader
    from pyxlsb import open_workbook
    out = {}
    p = DOCS / 'miso/miso_ltlf-workshop-supplemental-regional_2026.xlsx'
    w = load_workbook(p, read_only=True, data_only=True)
    rows = list(w['Data'].iter_rows(values_only=True)); header = rows[0]
    groups = {'0001': [1], '0004': [4], '0006': [6], '0027': [2, 7], '0035': [3, 5], '8910': [8, 9, 10]}
    vals, detail = {}, {}
    for year in YEARS:
        c = header.index(str(year)); lrz, locations = {}, {}
        for i, r in enumerate(rows[1:], 2):
            if r[:2] == ('Net Load', 'Current Trajectory') and r[3] == 'Annual_Energy_TWh' and r[4] is None:
                lrz[r[2]] = float(r[c]) * 1000
                locations[r[2]] = f'Data row {i}, year {year}, Annual_Energy_TWh x 1000'
        vals[year] = {z: sum(lrz[f'LRZ{n:02d}'] for n in ns) for z, ns in groups.items()}
        if abs(sum(vals[year].values()) - lrz['MISO']) > .02:
            raise ValueError('MISO regional total mismatch')
        detail[year] = dict(group_mapping=groups, cells=locations, published_total_GWh=lrz['MISO'])
    w.close()
    out['miso'] = evidence('miso', p, vals, detail, '2026 LTLF Current Trajectory net-load annual energy, LRZs aggregated to the six existing model groups. All drivers already included; 2023 shape retained.')

    p = DOCS / 'ercot/ercot_adjusted-hourly-forecast_2025.xlsb'
    zone_cols = dict(COAS='coast_net', EAST='east_net', FWES='fwest_net', NCEN='ncent_net',
                     NRTH='north_net', SCEN='scent_net', SOUT='south_net', WEST='west_net')
    sums = {y: {z: 0. for z in zone_cols} for y in YEARS}; counts = {y: set() for y in YEARS}
    row_counts = {y: 0 for y in YEARS}; repeated_labels = {y: [] for y in YEARS}
    totals = {y: 0. for y in YEARS}
    with open_workbook(str(p)) as w:
        with w.get_sheet('Sheet1') as sheet:
            rows = iter(sheet.rows()); header = [c.v for c in next(rows)]
            indexes = {z: header.index(col) for z, col in zone_cols.items()}; total_idx = header.index('ercot_net')
            for row in rows:
                v = [c.v for c in row]; year = int(v[0])
                if year not in YEARS:
                    continue
                stamp = tuple(v[:4])
                if stamp in counts[year]:
                    repeated_labels[year].append(stamp)
                counts[year].add(stamp)
                row_counts[year] += 1
                for z, i in indexes.items():
                    sums[year][z] += v[i] / 1000
                totals[year] += v[total_idx] / 1000
    for year in YEARS:
        if row_counts[year] != 8760 or len(repeated_labels[year]) > 1 or abs(sum(sums[year].values()) - totals[year]) > 1e-5:
            raise ValueError('ERCOT annual aggregation mismatch')
    out['ercot'] = evidence('ercot', p, sums, {y: dict(columns=zone_cols, source_hours=row_counts[y], repeated_local_clock_labels=repeated_labels[y], source_total_GWh=totals[y]) for y in YEARS},
        '2025 ERCOT-adjusted hourly forecast net columns summed to annual GWh; includes all published load additions and PV effects. 2023 weather/shape retained for common model inputs; source hourly shape not replayed. 2025 vintage, not the preliminary 2026 forecast.')

    p = DOCS / 'australia_nz/aemo_esoo-results-workbook_2026.xlsx'
    w = load_workbook(p, read_only=True, data_only=True)
    _, base = baseline('australia'); vals = {y: {} for y in YEARS}; detail = {y: {} for y in YEARS}
    for short in ('NSW', 'QLD', 'SA', 'TAS', 'VIC'):
        zone = short + '1'; sheet = w['Energy Forecasts - ' + short]
        if sheet['B19'].value != 'Delivered annual consumption outlook':
            raise ValueError('AEMO consumption definition changed')
        e0 = sum(float(sheet.cell(r, 6).value or 0) for r in range(22, 29)) * 1000
        for year, col in ((2030, 13), (2035, 18)):
            if sheet.cell(21, col).value != f'{year-1}-{str(year)[2:]}':
                raise ValueError('AEMO fiscal-year mismatch')
            e1 = sum(float(sheet.cell(r, col).value or 0) for r in range(22, 29)) * 1000
            vals[year][zone] = base[zone] * e1 / e0
            detail[year][zone] = dict(source_base_GWh=e0, source_future_GWh=e1, multiplier=e1/e0,
                input_base_GWh=base[zone], cell=f'{sheet.title}!rows 22:28, column {col}; base column 6', fiscal_year=sheet.cell(21, col).value)
    w.close()
    out['australia'] = evidence('australia', p, vals, detail,
        'AEMO 2026 ESOO Step Change: FY2022-23 to target FY delivered-consumption growth ratio applied to existing calendar-2023 TOTALDEMAND baseline. Keeps baseline definition offset fixed. Fiscal-year growth mapped to a representative 8760-hour calendar template; resulting level is a model-derived annual input, not published target-year TOTALDEMAND.', 'definition_adjusted_growth')

    p = DOCS / 'australia_nz/transpower_sosa-supplementary-data_2026.xlsx'
    w = load_workbook(p, read_only=True, data_only=True); frames, base = baseline('new_zealand')
    islands = {'NI': ['UNI', 'CNI', 'LNI'], 'SI': ['USI', 'LSI']}
    winter = {i: math.fsum(float(r['demand_MW']) for z in zs for r in frames[z] if 4 <= int(r['time'][5:7]) <= 9) / 1000 for i, zs in islands.items()}
    vals, detail = {}, {}
    for rownum, row in enumerate(w['Winter energy demand'].iter_rows(values_only=True), 1):
        if row[0] in YEARS and row[1:3] == ('Medium Demand', 'NZ_WEM'):
            year = int(row[0]); native = dict(SI=row[3], NI=row[9])
            vals[year] = {z: base[z] * native[i] / winter[i] for i, zs in islands.items() for z in zs}
            detail[year] = dict(island_zones=islands, source_winter_GWh=native, baseline_winter_GWh=winter,
                               cells=f'Winter energy demand!D{rownum}, J{rownum}; April-September GXP offtake')
    w.close()
    out['new_zealand'] = evidence('new_zealand', p, vals, detail,
        'Transpower SOSA 2026 Medium Demand island winter GXP offtake divided by matching April-September 2023 input. Island winter growth is applied to every hour/zone of that island. Annualisation proxy, NOT a published annual zonal forecast. DataGrid and other step loads are already in the selected forecast.', 'winter_growth_annualisation_proxy')

    p = DOCS / 'mexico/sener_prodesen-2024-2038-cap3-demanda_2024.pdf'
    text = PdfReader(p).pages[30].extract_text()
    if 'CUADRO 3.5' not in text:
        raise ValueError('Mexico source table moved')
    order = ['CEN', 'ORI', 'OCC', 'NOR', 'NTE', 'NES', 'PEN', 'BCA', 'BCS']
    vals, detail = {}, {}
    for year in YEARS:
        line = next(s for s in text.splitlines() if s.startswith(str(year) + ' '))
        numbers = [int(s.replace(',', '')) for s in line.split()[1:]]
        if len(numbers) != 12:
            raise ValueError('Unexpected Mexico columns')
        vals[year] = dict(zip(order, numbers[:9]))
        detail[year] = dict(source_row=line, source_table='Cuadro 3.5, printed p.65, PDF page 31',
                            excluded_SIMUL_GWh=numbers[9], published_SEN_GWh=numbers[11])
    out['mexico'] = evidence('mexico', p, vals, detail,
        'SENER PRODESEN 2024-2038 planning-scenario regional net-consumption levels. This is an older official forecast vintage selected for a directly published numeric table, not the latest PLADESE. Model covers nine regions and excludes SIMUL/Mulege, so its sum is not the full SEN total.')

    p = DOCS / 'brazil/epe_pde-2035-relatorio-final_2026.pdf'
    text = PdfReader(p).pages[52].extract_text().split('Tabela 2-3')[1]
    vals, detail = {}, {}
    for year in YEARS:
        line = next(s for s in text.splitlines() if s.strip().startswith(str(year) + ' '))
        nums = [int(s.replace('.', '')) for s in line.split()[1:]]
        if len(nums) != 5 or sum(nums[:4]) != nums[4]:
            raise ValueError('Brazil source total mismatch')
        vals[year] = {z: v * 8.76 for z, v in zip(['N', 'NE', 'SE', 'S'], nums[:4])}
        detail[year] = dict(source_row=line, source_table='PDE 2035 Table 2-3, PDF/printed p.53', unit='MWmedio', conversion='x 8760 / 1000 = GWh')
    out['brazil'] = evidence('brazil', p, vals, detail,
        'EPE PDE 2035 reference scenario, Table 2-3 annual-average subsystem system load (carga de energia). Convert MWmedio to GWh using representative 8760 hours. Includes modelled demand drivers; no separate data-centre addition.')

    p = DOCS / 'south_africa/esrg-uct_spatialized-irp-demand-no-exports_2025.zip'
    mapping = dict(EC='Eastern Cape', FS='Free State', GP='Gauteng', HYDRA='Hydra Central', KZN='Kwazulu Natal', LP='Limpopo', MP='Mpumalanga', NC='Northern Cape', NW='North West', WC='Western Cape')
    vals = {y: {z: 0. for z in mapping} for y in YEARS}; counts = {y: set() for y in YEARS}
    with zipfile.ZipFile(p) as z:
        member = 'IRPDemandREF_provincial_no_exports.csv'
        with z.open(member) as f:
            for row in csv.DictReader(io.TextIOWrapper(f)):
                year = int(row['model_year'])
                if year not in YEARS:
                    continue
                stamp = row['overall_hour']
                if stamp in counts[year]:
                    raise ValueError('Duplicate South Africa source hour')
                counts[year].add(stamp)
                for zone, label in mapping.items():
                    vals[year][zone] += float(row[label + '_corrected']) / 1000
    if any(len(counts[y]) != 8760 for y in YEARS):
        raise ValueError('Unexpected South Africa source hours')
    out['south_africa'] = evidence('south_africa', p, vals,
        {y: dict(member=member, source_hours=len(counts[y]), source_columns=mapping) for y in YEARS},
        'ESRG/University of Cape Town spatialised IRP demand REF, no exports. Sum published model-year provincial hourly series; national demand has been spatially allocated by the source authors. Research scenario, not an independently forecast utility series for each model zone. Retain model 2023 weather/hourly shape.', 'spatialised_research_forecast')
    return out


def prepare(extract_sources=False, build_packs=False):
    audit_path = HERE / 'data/all_regions_source_audit.json'
    if extract_sources:
        dump(audit_path, extract_other_regions())
    audits = json.loads(audit_path.read_text())
    index = []
    for region in REGIONS:
        _, base = baseline(region)
        if region == 'nyiso':
            # Existing source-verified NYISO files are preserved.
            for year in YEARS:
                name = f'nyiso_{year}_official'
                spec = HERE / f'scenarios/{name}.json'
                target = json.loads((HERE / f'data/{name}.json').read_text())
                index.append(dict(region=region, year=year, name=name, annual_GWh=sum(target['annual_GWh'].values()), baseline_GWh=sum(base.values()),
                                  input_kind='published_annual_energy', spec=str(spec.relative_to(ROOT)), pack=f'prediction_local/packs/{name}'))
            continue
        a = audits[region]; source = ROOT / a['source_file']
        if sha(source) != a['source_sha256']:
            raise ValueError(f'Source changed since extraction: {source}')
        derived_path = DOCS / f'derived/{region}_growth.csv'
        derived = read_csv(derived_path)
        for year in YEARS:
            values = a['annual_GWh'][str(year)]
            if set(values) != set(base):
                raise ValueError(f'Network zone mismatch for {region}')
            # Independent check against the existing extraction, preserving its stated rounding.
            rows = [r for r in derived if r['scenario'] == SELECTION[region] and int(r['year']) == year and r['zone'] in base]
            if len(rows) != len(base) or {r['zone'] for r in rows} != set(base):
                raise ValueError('Missing or duplicate derived forecast records')
            errors = {}
            for row in rows:
                z = row['zone']
                if region == 'australia':
                    original = a['details'][str(year)][z]['source_future_GWh']
                else:
                    original = values[z]
                errors[z] = original - float(row['energy_GWh'])
            # NZ's legacy table rounded island source and denominator to whole GWh.
            tolerances = {z: .051 for z in errors}
            if region == 'new_zealand':
                d = a['details'][str(year)]
                for island, zones in d['island_zones'].items():
                    den = d['baseline_winter_GWh'][island]
                    num = d['source_winter_GWh'][island]
                    for z in zones:
                        tolerances[z] += base[z] * (.5 / den + (num + .5) * .5 / (den * (den - .5)))
            if any(abs(errors[z]) > tolerances[z] for z in errors):
                raise ValueError(f'Raw-source vs derived extraction mismatch: {region} {year}: {errors}')
            name = f'{region}_{year}_baseline'
            paths = {f'../../{a["source_file"]}': a['source_sha256'],
                     'all_regions_source_audit.json': sha(audit_path)}
            if region in ('australia', 'new_zealand'):
                for p in (ROOT / f'experiment/{region}/profiles').glob('*.csv'):
                    paths['../../' + str(p.relative_to(ROOT))] = sha(p)
            ref = a['reference']
            target = dict(region=region, year=year, growth='baseline', annual_GWh=values,
                          citation=f'{ref["publisher"]} ({ref["year"]}), {ref["title"]}; {ref["url"]}',
                          method=a['method'], source_files=paths, resolution=a['input_kind'],
                          input_kind=a['input_kind'], forecast_scenario=SELECTION[region], source_edition=ref['year'],
                          extraction=a['details'][str(year)], derived_crosscheck_error_GWh=errors, derived_crosscheck_tolerance_GWh=tolerances)
            dump(HERE / f'data/{name}.json', target)
            spec = HERE / f'scenarios/{name}.json'
            dump(spec, dict(name=name, region=region, year=year, growth='baseline', blocks=[], forecast_file=f'../data/{name}.json'))
            index.append(dict(region=region, year=year, name=name, annual_GWh=sum(values.values()), baseline_GWh=sum(base.values()),
                              input_kind=a['input_kind'], spec=str(spec.relative_to(ROOT)), pack=f'prediction_local/packs/{name}'))
    if build_packs:
        for item in index:
            pack = ROOT / item['pack']
            if pack.exists():
                verify(pack)
                actual = json.loads((pack / 'config.json').read_text())['prediction']['forecast']['annual_GWh']
                expected = json.loads((HERE / f'data/{item["name"]}.json').read_text())['annual_GWh']
                if actual != expected:
                    raise ValueError(f'Existing pack has different targets: {pack}')
            else:
                build(ROOT / item['spec'], pack)
                verify(pack)
    dump(HERE / 'data/all_regions_index.json', index)
    return index


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--extract', action='store_true', help='re-read original XLSX/XLSB/PDF/ZIP sources')
    p.add_argument('--build-packs', action='store_true')
    a = p.parse_args()
    print(json.dumps(prepare(a.extract, a.build_packs), indent=2))
