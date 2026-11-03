#!/usr/bin/env python3
"""Extract NYISO 2026 source cells and prepare annual demand scenarios, without an LLM."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
import zipfile

import os
SOURCE_DIR = Path(__file__).resolve().parent
ROOT = Path(os.environ.get('GRID_DATA_ROOT', str(SOURCE_DIR.parents[1] / 'data'))).expanduser().resolve()
HERE = ROOT / 'prediction_local'
sys.path.insert(0, str(SOURCE_DIR.parent / 'prediction'))
from scenarios import build, dump, verify

SOURCE = HERE / 'sources/nyiso_gold_book_2026_baseline.xlsx'
SOURCE_SHA256 = '2ece8baafffa9c152161d39d21488b312227ce22e12bd4eb80b2e93598d46fb4'
URL = 'https://www.nyiso.com/documents/20142/57858057/2026-Gold-Book-Baseline-Forecast-Tables.xlsx/c8895dee-3d9f-0b3e-52fb-6a9ea5fea535'
NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}


def sheet_cells(path, sheet_name):
    """Read published cached cell values from XLSX with Python's standard library."""
    with zipfile.ZipFile(path) as z:
        strings = []
        if 'xl/sharedStrings.xml' in z.namelist():
            strings = [''.join(t.itertext()) for t in ET.fromstring(z.read('xl/sharedStrings.xml'))]
        book = ET.fromstring(z.read('xl/workbook.xml'))
        sheet = next(s for s in book.find('s:sheets', NS) if s.attrib['name'] == sheet_name)
        rid = sheet.attrib['{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id']
        rels = ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
        target = next(r.attrib['Target'] for r in rels if r.attrib['Id'] == rid)
        target = target.lstrip('/') if target.startswith('/') else 'xl/' + target
        cells = {}
        for c in ET.fromstring(z.read(target)).findall('.//s:c', NS):
            v = c.find('s:v', NS)
            if c.attrib.get('t') == 'inlineStr':
                value = ''.join(c.find('s:is', NS).itertext())
            elif v is None:
                continue
            elif c.attrib.get('t') == 's':
                value = strings[int(v.text)]
            else:
                try:
                    value = float(v.text)
                except (ValueError, TypeError):
                    value = v.text
            cells[c.attrib['r']] = value
        return cells


def extract():
    if hashlib.sha256(SOURCE.read_bytes()).hexdigest() != SOURCE_SHA256:
        raise ValueError('NYISO workbook differs from the verified official download')
    energy, loads = sheet_cells(SOURCE, 'I-2'), sheet_cells(SOURCE, 'I-14')
    if 'Annual Energy' not in energy['C5'] or 'Annual Energy' not in loads['C47']:
        raise ValueError('Unexpected workbook section/units')
    if 'embedded' not in loads['C89']:
        raise ValueError('Missing evidence that large loads are included in the total forecast')
    records = []
    for year, erow, lrow, prow in [(2026, 18, 50, 8), (2030, 22, 54, 12), (2035, 27, 59, 17)]:
        if energy[f'C{erow}'] != year or loads[f'C{lrow}'] != year or loads[f'C{prow}'] != year:
            raise ValueError('Unexpected forecast year')
        zonal = {}
        for offset, letter in enumerate('ABCDEFGHIJK'):
            col = chr(ord('D') + offset)
            if energy[f'{col}7'] != letter or loads[f'{col}49'] != letter:
                raise ValueError('Unexpected zone order')
            zonal['ZON' + letter] = {
                'annual_GWh': energy[f'{col}{erow}'],
                'large_load_GWh': loads[f'{col}{lrow}'],
                'large_load_summer_peak_MW': loads[f'{col}{prow}'],
                'cells': {'annual_GWh': f"I-2!{col}{erow}", 'large_load_GWh': f"I-14!{col}{lrow}",
                          'large_load_summer_peak_MW': f"I-14!{col}{prow}"}}
        totals = {'annual_GWh': energy[f'O{erow}'], 'large_load_GWh': loads[f'O{lrow}'],
                  'large_load_summer_peak_MW': loads[f'O{prow}']}
        for key, value in totals.items():
            if abs(sum(z[key] for z in zonal.values()) - value) > 1e-8:
                raise ValueError(f'Zonal sum does not match NYCA: {year} {key}')
        records.append({'year': year, 'zones': zonal, 'NYCA': totals})
    return {'publisher': 'NYISO', 'edition': 2026, 'url': URL, 'sha256': SOURCE_SHA256,
            'checked_date': '2026-09-22', 'load_category': 'large_loads_not_datacenters_only',
            'embedded_in_total': True, 'records': records}


def prepare(build_packs=False):
    evidence = extract()
    (HERE / 'data').mkdir(exist_ok=True)
    (HERE / 'scenarios').mkdir(exist_ok=True)
    dump(HERE / 'data/nyiso_2026_extracted.json', evidence)
    prepared = []
    for record in evidence['records'][1:]:
        year, zones = record['year'], record['zones']
        name = f'nyiso_{year}_official'
        targets = {z: r['annual_GWh'] for z, r in zones.items()}
        method = ('Match published target-year zonal annual energy, including existing and future large loads. '
                  'Scale the 2023 hourly shape. Large-load component is recorded as evidence, not added again. '
                  'Published seasonal peaks and future hourly load shapes are not fitted.')
        target = dict(region='nyiso', year=year, growth='baseline', annual_GWh=targets,
                      citation=f'NYISO (2026), Gold Book, I-2 and I-14; {URL}', method=method,
                      resolution='published_zonal', source_files={'../sources/nyiso_gold_book_2026_baseline.xlsx': SOURCE_SHA256},
                      scenario_kind='official_annual_totals', extraction=record)
        dump(HERE / f'data/{name}.json', target)
        spec = dict(name=name, region='nyiso', year=year, growth='baseline',
                    forecast_file=f'../data/{name}.json', blocks=[])
        spec_path = HERE / f'scenarios/{name}.json'
        dump(spec_path, spec)
        if build_packs:
            pack = HERE / 'packs' / name
            build(spec_path, pack)
            verify(pack)
        prepared.append({'scenario': name, 'annual_GWh': sum(targets.values()),
                         'included_large_load_GWh': record['NYCA']['large_load_GWh']})
    dump(HERE / 'data/prepared_scenarios.json', prepared)
    return prepared


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-packs', action='store_true', help='also generate new 8760-hour input packs; refuses overwrite')
    args = parser.parse_args()
    print(json.dumps(prepare(args.build_packs), indent=2))
