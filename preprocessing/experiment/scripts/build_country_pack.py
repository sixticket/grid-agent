"""Build the Australia, New Zealand, Mexico and Brazil packs from locally archived
official files (see each region's raw/ folder and raw/SOURCES.json). Same pack
contract as build_inputs.py; existing-fleet brownfield is the campaign mode.

Usage: python scripts/build_country_pack.py [australia new_zealand mexico brazil]
"""
from pathlib import Path
import csv, json, math, re, sys, glob, io, collections, datetime, zoneinfo
import numpy as np
import pandas as pd
from tqdm import tqdm
import os
ROOT = Path(os.environ.get('GRID_DATA_ROOT', str(Path(__file__).resolve().parents[3] / 'data'))).expanduser().resolve() / 'experiment'
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_inputs import PARAMS, STORAGE, js, distance, ident  # shared modelling defaults

# ----------------------------------------------------------------------------- shared rules
# Per-circuit thermal ratings by voltage (ASSUMPTION, same rule family as the US/ZA packs).
RATING_MW = {765: 2500, 500: 1800, 400: 1200, 345: 1000, 330: 1000, 275: 600, 230: 450, 220: 450,
             161: 250, 138: 180, 115: 120, 110: 120, 85: 80, 69: 60, 66: 60}
COST_USD_PER_MW_KM_YR = 200.      # same km rule as the US/ZA packs
CRF = 0.075                        # annualization of a published capex (7%, 40 yr) ASSUMPTION
FX = {'AUD': 0.664, 'NZD': 0.614, 'MXN': 0.0564, 'BRL': 0.200}   # 2023 average USD per unit (ASSUMPTION, documented)


def hourly_from_local(frame, cols):
    """frame: time (naive local model clock) + columns; verifies 8760 rows."""
    frame = frame.sort_values('time').reset_index(drop=True)
    if len(frame) != 8760 or frame[cols].isna().any().any():
        raise ValueError(f'demand needs 8760 complete rows, got {len(frame)}')
    return frame


def new_limits(peak):
    return dict(wind=peak * 3, solar=peak * 5, nuclear=peak, battery=peak, electrolyzer=peak,
                fuel_cell=peak, h2_storage_MWh=peak * 2000)


def corridor(a, b, nodes, kv, existing_circuits, existing_fwd, existing_rev, source, note='', max_new=1, planned=None):
    km = distance((nodes[a]['latitude'], nodes[a]['longitude']), (nodes[b]['latitude'], nodes[b]['longitude']))
    rating = RATING_MW[kv]
    c = dict(id=f'{a}_{b}', bus0=a, bus1=b, length_km=round(km, 1), voltage_kV=kv, circuit_rating_MW=float(rating),
             max_new_circuits=max_new, existing_circuits=existing_circuits,
             new_annual_cost_per_circuit_usd=round(km * rating * COST_USD_PER_MW_KM_YR, 0),
             existing_annual_fixed_cost_per_circuit_usd=0, source=source)
    if existing_circuits:
        c['existing_capacity_MW'] = float(existing_fwd)
        c['existing_reverse_capacity_MW'] = float(existing_rev)
    if note:
        c['note'] = note
    if planned:
        c['planned_projects'] = planned
    return c


def project(pid, members, capex_musd=None, annual_usd=None, status='', source='', increments=None, note='', year=None):
    if annual_usd is None:
        annual_usd = capex_musd * 1e6 * CRF
    p = dict(id=pid, members=members, annual_cost_usd=round(annual_usd, 0), status=status, source=source)
    if increments:
        p['increments'] = increments
    if note:
        p['note'] = note
    if year:
        p['in_service_year'] = year
    return p


# ----------------------------------------------------------------------------- AUSTRALIA (NEM)
AU_NODES = {  # state capitals as representative points (location proxy; documented)
    'NSW1': ('New South Wales', -33.8688, 151.2093), 'QLD1': ('Queensland', -27.4705, 153.0260),
    'VIC1': ('Victoria', -37.8136, 144.9631), 'SA1': ('South Australia', -34.9285, 138.6007),
    'TAS1': ('Tasmania', -42.8826, 147.3257)}


def au_demand(folder):
    frames = []
    for region in AU_NODES:
        parts = [pd.read_csv(f) for f in sorted(glob.glob(str(folder / f'raw/PRICE_AND_DEMAND_2023*_{region}.csv')))]
        d = pd.concat(parts)
        t = pd.to_datetime(d.SETTLEMENTDATE)  # interval END, AEST (UTC+10, no DST)
        d['time'] = (t - pd.Timedelta(minutes=5)).dt.floor('h')
        h = d.groupby('time').TOTALDEMAND.mean().rename(region)
        frames.append(h)
    frame = pd.concat(frames, axis=1).reset_index()
    frame = frame[(frame.time >= '2023-01-01') & (frame.time < '2024-01-01')]
    # South Australia's operational demand is negative in a few hours of 2023 (rooftop PV exceeds load and the
    # region exports at the distribution level). The model has no rooftop PV, so those hours are floored at zero.
    neg = int((frame[list(AU_NODES)] < 0).sum().sum())
    frame[list(AU_NODES)] = frame[list(AU_NODES)].clip(lower=0.)
    frame.attrs['negative_hours_floored'] = neg
    return hourly_from_local(frame, list(AU_NODES))


def au_fleet(folder, nodes):
    import openpyxl
    wb = openpyxl.load_workbook(folder / 'raw/nem-registration-and-exemption-list.xlsx', read_only=True, data_only=True)
    rows = list(wb['PU and Scheduled Loads'].iter_rows(values_only=True))
    hi = next(i for i, r in enumerate(rows) if r and any(str(x).strip() == 'DUID' for x in r if x))
    hdr = [str(x).strip() if x else '' for x in rows[hi]]
    df = pd.DataFrame(rows[hi + 1:], columns=hdr)
    df = df[df['DUID'].notna() & df['Region'].isin(AU_NODES)]
    # 2023-12-31 snapshot: DUIDs registered in the MMSDM DUDETAILSUMMARY of December 2023 (START_DATE <= 2023-12-31 < END_DATE)
    du = pd.read_csv(folder / 'raw/PUBLIC_DVD_DUDETAILSUMMARY_202312010000.CSV', skiprows=1)
    du = du[du.iloc[:, 0] == 'D']
    du['start'] = pd.to_datetime(du.START_DATE); du['end'] = pd.to_datetime(du.END_DATE)
    active = set(du[(du.start <= '2023-12-31') & (du.end > '2023-12-31')].DUID)
    recs = []
    skipped = []
    for _, r in df.iterrows():
        if r['Dispatch Type'] == 'Load':
            continue
        duid = str(r['DUID'])
        stem = re.sub(r'\d+$', '', duid)
        # batteries were registered in 2023 as separate generator/load DUIDs (e.g. BALBG1/BALBL1); the current
        # list carries one bidirectional DUID (BALB1), so match on the DUID stem for storage
        registered = duid in active or (str(r['Fuel Source - Primary']) == 'Battery Storage' and any(a.startswith(stem) for a in active))
        if not registered:
            skipped.append(duid); continue
        fuel = str(r['Fuel Source - Primary']); desc = str(r['Fuel Source - Descriptor']).lower(); tech = str(r['Technology Type - Descriptor']).lower()
        try:
            cap = float(r['Reg Cap generation (MW)'])
        except (TypeError, ValueError):
            try:
                cap = float(r['Max Cap generation (MW)'])
            except (TypeError, ValueError):
                cap = 0.
        if fuel == 'Battery Storage' or 'battery' in tech:
            t = 'battery'
        elif 'pump' in tech:
            t = 'pumped_storage'
        elif fuel == 'Solar':
            t = 'solar'
        elif fuel == 'Wind':
            t = 'wind'
        elif fuel == 'Hydro':
            t = 'hydro'
        elif fuel.startswith('Renewable'):
            t = 'waste' if ('landfill' in desc or 'waste' in desc or 'sewage' in desc) else 'biomass'
        elif fuel == 'Fossil':
            if 'coal' in desc:
                t = 'coal'
            elif 'diesel' in desc or 'fuel oil' in desc or 'kerosene' in desc:
                t = 'oil'
            else:
                t = 'gas'
        else:
            t = 'other'
        energy = None
        if t in STORAGE:
            e = r.get('Maximum storage capacity')
            try:
                energy = float(e) if e not in (None, '', '-') else None
            except Exception:
                energy = None
            if energy is not None and not math.isfinite(energy):
                energy = None
        if not math.isfinite(cap):
            cap = 0.
        recs.append(dict(name=r['Station Name'], duid=r['DUID'], bus=r['Region'], technology=t, capacity_MW=cap,
                         energy_MWh=energy, active=True, source='AEMO NEM Registration and Exemption List (PU and Scheduled Loads) filtered to DUIDs registered on 2023-12-31 in MMSDM DUDETAILSUMMARY 2023-12',
                         needs_review=False, note=''))
    js(folder / 'raw/duids_not_registered_2023.json', dict(count=len(skipped), duids=skipped))
    return recs


def au_network(nodes, peak):
    src_ic = 'AEMO Interconnector Capabilities (2024, congestion-information), raw/interconnector-capabilities-2024.pdf'
    src_a5 = 'AEMO 2024 ISP Appendix 5 Network Investments, raw/2024-isp-appendix-5.pdf'
    cors = [
        corridor('NSW1', 'QLD1', nodes, 330, 2, 745 + 107, 1170 + 210, src_ic,
                 'QNI (NSW->QLD 745 MW summer typical, QLD->NSW 1170 MW) plus Terranora/Directlink (107 / 210 MW) lumped as one corridor; existing circuits = 2 paths',
                 planned=['QNI Connect']),
        corridor('NSW1', 'VIC1', nodes, 330, 2, 1900, 1700, src_ic,
                 'VIC-NSW interconnector: NSW->VIC 400-1900 MW, VIC->NSW 400-1700 MW depending on conditions; upper bound of the published range used (ASSUMPTION)',
                 planned=['VNI West']),
        corridor('VIC1', 'SA1', nodes, 275, 2, 600 + 220, 550 + 200, src_ic,
                 'Heywood (VIC->SA 600, SA->VIC 550 testing limits) plus Murraylink DC (220 / 200) lumped'),
        corridor('VIC1', 'TAS1', nodes, 500, 1, 478, 594, src_ic,
                 'Basslink HVDC: VIC->TAS 478 MW, TAS->VIC 594 MW; rating rule for a rebuild uses 500 kV class (ASSUMPTION)',
                 planned=['Marinus Link Stage 1', 'Marinus Link Stage 2']),
        corridor('NSW1', 'SA1', nodes, 330, 0, 0, 0, src_a5,
                 'no interconnector in the 2023 base year; Project EnergyConnect (800 MW SA-NSW) enters as an official project',
                 planned=['Project EnergyConnect']),
    ]
    aud = FX['AUD']
    projects = [
        project('VNI_West', [dict(corridor='NSW1_VIC1', capacity_MW=1669., reverse_capacity_MW=1935.)], capex_musd=3614 * aud,
                status='Actionable ISP project; proponent timing December 2029', source=src_a5 + ' p.45: North 1,935 MW, South 1,669 MW; AEMO modelled cost $3,614 million (real 2023 AUD)',
                note='south = NSW->VIC increment; capex converted at 0.664 USD/AUD and annualized at CRF 0.075 (ASSUMPTIONS)', year=2029),
        project('QNI_Connect', [dict(corridor='NSW1_QLD1', capacity_MW=1260., reverse_capacity_MW=1700.)], capex_musd=2518 * aud,
                status='Actionable ISP project; earliest March 2033', source=src_a5 + ' p.47: North 1,260 MW, South 1,700 MW, expected cost $2,518 million (Class 5, +/-50%)',
                note='capex converted at 0.664 USD/AUD and annualized at CRF 0.075 (ASSUMPTIONS)', year=2033),
        project('Marinus_Stage_1', [dict(corridor='VIC1_TAS1', capacity_MW=750., reverse_capacity_MW=750.)], capex_musd=3840 * aud,
                status='Actionable ISP project; full capacity December 2030', source=src_a5 + ' p.37: 750 MW HVDC monopole in both directions, expected cost $3,840 million (Stage 1 total)',
                note='capex converted at 0.664 USD/AUD and annualized at CRF 0.075 (ASSUMPTIONS)', year=2030),
        project('Marinus_Stage_2', [dict(corridor='VIC1_TAS1', capacity_MW=750., reverse_capacity_MW=750.)], capex_musd=2735 * aud,
                status='Actionable ISP project (staged); full capacity December 2032', source=src_a5 + ' p.37: second 750 MW HVDC monopole, expected cost $2,735 million (Stage 2 total)',
                note='identical to Stage 1 on the same corridor, so the symmetry ordering applies; capex conversion and CRF are ASSUMPTIONS', year=2032),
        project('Project_EnergyConnect', [dict(corridor='NSW1_SA1', capacity_MW=800., reverse_capacity_MW=800.)], capex_musd=2280 * aud,
                status='Under construction 2023-2026 (stage 1 150 MW energised late 2023); treated entirely as a project for the 2023 base year',
                source='AEMO 2024 ISP (raw/2024-isp.pdf) and Transgrid/ElectraNet PEC: 800 MW SA-NSW 330 kV interconnector, approved cost about $2.28 billion',
                note='cost figure from the RIT-T/AER determination (approx.); capex conversion and CRF are ASSUMPTIONS', year=2026),
    ]
    return dict(schema_version=2, scenario='australia',
                provenance=dict(kind='reported', source='NEM five-region topology with AEMO published interconnector transfer capabilities (2024 congestion information) and 2024 ISP Appendix 5 actionable projects with published capacities and costs; corridor lengths are capital-to-capital distances; new-circuit ratings and the km cost rule are assumptions'),
                voltage_kV=330, flow_model='transport', greenfield_policy='rebuildable',
                nodes=[], corridors=cors, interfaces=[], zone_limits={}, projects=projects)


# ----------------------------------------------------------------------------- NEW ZEALAND
NZ_NODES = {'UNI': ('Upper North Island', -36.8485, 174.7633), 'CNI': ('Central North Island', -37.7870, 175.2793),
            'LNI': ('Lower North Island', -41.2865, 174.7762), 'USI': ('Upper South Island', -43.5321, 172.6362),
            'LSI': ('Lower South Island', -45.8788, 170.5028)}


class _PocZone(dict):
    """POC -> EMI zone from the current NSP table, with a substation-prefix fallback for
    historical POCs that no longer appear in the table (e.g. HAM0331)."""
    def __init__(self, table):
        super().__init__(table)
        self.prefix = collections.defaultdict(collections.Counter)
        for k, v in table.items():
            self.prefix[str(k)[:3]][v] += 1
    MANUAL = {'HAM': 'CNI'}   # Hamilton (Waikato): historical POC absent from the current table
    def __missing__(self, key):
        pre = self.prefix.get(str(key)[:3])
        if not pre:
            if str(key)[:3] in self.MANUAL:
                return self.MANUAL[str(key)[:3]]
            raise KeyError(key)
        return pre.most_common(1)[0][0]
    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default


def nz_poc_zone(folder):
    n = pd.read_csv(folder / 'raw/NetworkSupplyPointsTable.csv')
    n = n[n['Zone'].notna()].sort_values('Current flag', ascending=False).drop_duplicates('POC code')
    return _PocZone(n.set_index('POC code')['Zone'].to_dict())


def nz_tp_to_utc(trading_date, tp):
    tz = zoneinfo.ZoneInfo('Pacific/Auckland')
    midnight = datetime.datetime.combine(trading_date, datetime.time(0), tzinfo=tz)
    return (midnight.astimezone(datetime.timezone.utc) + datetime.timedelta(minutes=30 * (tp - 1))).replace(tzinfo=None)


def nz_demand(folder):
    zone = nz_poc_zone(folder)
    acc = collections.defaultdict(float)
    files = sorted(glob.glob(str(folder / 'raw/2023*_Grid_export.csv')))
    for f in tqdm(files, desc='NZ grid export'):
        g = pd.read_csv(f)
        tps = [c for c in g.columns if c.startswith('TP')]
        g['zone'] = [zone.get(p) for p in g.POC]
        missing = g[g.zone.isna()].POC.unique()
        if len(missing):
            raise ValueError(f'unmapped POCs {missing[:5]}')
        for _, r in g.iterrows():
            d = datetime.date.fromisoformat(str(r.Trading_Date)[:10])
            for i, c in enumerate(tps, 1):
                v = r[c]
                if pd.isna(v):
                    continue
                utc = nz_tp_to_utc(d, i)
                local = (utc + datetime.timedelta(hours=12)).replace(minute=0)   # NZST fixed model clock
                acc[(local, r.zone)] += float(v) / 1000.  # kWh per half hour -> MWh per hour when summed
    frame = pd.Series(acc).unstack().reset_index().rename(columns={'index': 'time'})
    frame = frame[(frame.time >= '2023-01-01') & (frame.time < '2024-01-01')]
    # the NZDT-to-NZST conversion leaves the last NZST hour of the year without a trading period; fill from the neighbour
    frame = frame.set_index('time').reindex(pd.date_range('2023-01-01', periods=8760, freq='h')).interpolate(limit_direction='both').rename_axis('time').reset_index()
    return hourly_from_local(frame, list(NZ_NODES))


def nz_fleet(folder, nodes):
    zone = nz_poc_zone(folder)
    f = pd.read_csv(folder / 'raw/DispatchedGenerationPlant.csv')
    recs = []
    for _, r in f.iterrows():
        com = pd.to_datetime(r.DateCommissioned, dayfirst=True, errors='coerce')
        dec = pd.to_datetime(r.DateDecommissioned, dayfirst=True, errors='coerce') if str(r.DateDecommissioned) not in ('null', 'nan') else pd.NaT
        if pd.isna(com) or com > pd.Timestamp('2023-12-31') or (not pd.isna(dec) and dec < pd.Timestamp('2023-01-01')):
            continue
        fuel = str(r.Fuel)
        t = {'Hydro': 'hydro', 'Geothermal': 'geothermal', 'Gas': 'gas', 'Coal/Gas': 'coal', 'Diesel': 'oil', 'Wind': 'wind',
             'Solar': 'solar', 'Electrical energy': 'battery', 'Wood': 'biomass'}.get(fuel, 'other')
        bus = zone.get(r.PointOfConnectionCode)
        review = bus is None
        if bus is None:
            bus = 'CNI' if r.PointOfConnectionCode.startswith('KAW') else 'UNI'
        recs.append(dict(name=r.PlantName, duid=r.UnitCode, bus=bus, technology=t, capacity_MW=float(r.NameplateMegawatts),
                         energy_MWh=None, active=True, source='EMI Existing generation plant (DispatchedGenerationPlant, 2026-07-02 file) filtered to units commissioned by 2023-12-31',
                         needs_review=review, note='' if not review else 'POC not in the NSP table; zone assigned by substation code'))
    return recs


def nz_hydro_factors(folder, nodes):
    """Actual 2023 hydro capacity factor by zone from EMI generation_MD (kWh per trading period)."""
    zone = nz_poc_zone(folder)
    energy = collections.defaultdict(float)
    for f in sorted(glob.glob(str(folder / 'raw/2023*_Generation_MD.csv'))):
        g = pd.read_csv(f)
        g = g[g.Fuel_Code == 'Hydro']
        tps = [c for c in g.columns if c.startswith('TP')]
        g['zone'] = [zone.get(p, 'CNI') for p in g.POC_Code]
        s = g.groupby('zone')[tps].sum().sum(axis=1) / 1e6  # GWh
        for z, v in s.items():
            energy[z] += v
    return dict(energy)


def nz_network(nodes, peak):
    tpr = 'Transpower Transmission Planning Report 2023, raw/TPR2023.pdf (HVDC north transfer 1,200 MW total possible capacity, section 5; south transfer and North Island backbone pairwise limits not published as single numbers)'
    proxy = 'no published single pairwise limit; non-binding proxy 50,000 MW (ASSUMPTION, same convention as MISO/ERCOT unpublished pairs)'
    cors = [
        corridor('UNI', 'CNI', nodes, 220, 4, 50000, 50000, tpr, proxy),
        corridor('CNI', 'LNI', nodes, 220, 4, 50000, 50000, tpr, proxy),
        corridor('LNI', 'USI', nodes, 500, 1, 850, 1200, tpr,
                 'HVDC inter-island: north (USI->LNI, bus1->bus0) 1,200 MW; south (LNI->USI) 850 MW per Transpower HVDC page (reactive plant upgrade); rating rule 500 kV class for a rebuild (ASSUMPTION)',
                 planned=['HVDC_1400']),
        corridor('USI', 'LSI', nodes, 220, 4, 50000, 50000, tpr, proxy),
    ]
    projects = [
        project('HVDC_1400', [dict(corridor='LNI_USI', capacity_MW=150., reverse_capacity_MW=200.)], annual_usd=distance((nodes['LNI']['latitude'], nodes['LNI']['longitude']), (nodes['USI']['latitude'], nodes['USI']['longitude'])) * 200 * COST_USD_PER_MW_KM_YR,
                status='Transpower HVDC cable replacement with an additional cable: capacity to 1,400 MW north (TPR 2023 section 5; Transpower HVDC upgrade programme)',
                source=tpr + '; https://www.transpower.co.nz/hvdc-upgrade', note='increments 200 MW north (1,200->1,400) and 150 MW south (ASSUMPTION for south); cost by the km rule because no capex is published', year=2030),
    ]
    return dict(schema_version=2, scenario='new_zealand',
                provenance=dict(kind='derived', source='EMI five-zone topology (UNI, CNI, LNI, USI, LSI) with the published HVDC inter-island capability; North and South Island backbone pairwise limits are not published as single numbers and are non-binding proxies; corridor lengths are between representative cities'),
                voltage_kV=220, flow_model='transport', greenfield_policy='rebuildable',
                nodes=[], corridors=cors, interfaces=[], zone_limits={}, projects=projects)


# ----------------------------------------------------------------------------- MEXICO
MX_NODES = {'BCA': ('Baja California', 32.6245, -115.4523, -8), 'BCS': ('Baja California Sur', 24.1426, -110.3128, -7),
            'NOR': ('Noroeste', 29.0729, -110.9559, -6), 'NTE': ('Norte', 25.5691, -103.4960, -6), 'NES': ('Noreste', 25.6866, -100.3161, -6),
            'OCC': ('Occidental', 20.6597, -103.3496, -6), 'CEN': ('Central', 19.4326, -99.1332, -6), 'ORI': ('Oriental', 19.0414, -98.2063, -6),
            'PEN': ('Peninsular', 20.9674, -89.5926, -6)}
MX_STATE_GCR = {  # ASSUMPTION: state -> Gerencia de Control Regional (control-area seats and PRODESEN region map)
    'Baja California': 'BCA', 'Baja California Sur': 'BCS', 'Sonora': 'NOR', 'Sinaloa': 'NOR', 'Chihuahua': 'NTE', 'Durango': 'NTE',
    'Coahuila de Zaragoza': 'NES', 'Nuevo León': 'NES', 'Tamaulipas': 'NES', 'Texas, EE.UU.': 'NES',
    'Jalisco': 'OCC', 'Nayarit': 'OCC', 'Colima': 'OCC', 'Michoacán de Ocampo': 'OCC', 'Aguascalientes': 'OCC', 'Zacatecas': 'OCC',
    'San Luis Potosí': 'OCC', 'Guanajuato': 'OCC', 'Querétaro': 'OCC',
    'Ciudad de México': 'CEN', 'Estado de México': 'CEN', 'Hidalgo': 'CEN', 'Morelos': 'CEN',
    'Puebla': 'ORI', 'Tlaxcala': 'ORI', 'Veracruz de Ignacio de la Llave': 'ORI', 'Oaxaca': 'ORI', 'Chiapas': 'ORI', 'Tabasco': 'ORI', 'Guerrero': 'ORI',
    'Yucatán': 'PEN', 'Campeche': 'PEN', 'Quintana Roo': 'PEN'}


def mx_demand(folder):
    files = sorted(glob.glob(str(folder / 'raw/demanda_real/demanda_real_balance_2023-*.csv')))
    if len(files) != 365:
        raise ValueError(f'expected 365 daily CENACE files, found {len(files)}')
    rows = []
    for f in tqdm(files, desc='CENACE daily files'):
        txt = open(f, encoding='latin-1').read().split('\n')
        hi = next(i for i, l in enumerate(txt) if l.startswith('"Sistema"'))
        df = pd.read_csv(io.StringIO('\n'.join(txt[hi:])), skipinitialspace=True)
        df.columns = [c.strip() for c in df.columns]
        day = datetime.date.fromisoformat(Path(f).stem[-10:])
        for _, r in df.iterrows():
            area = 'BCS' if r['Sistema'] == 'BCS' else r['Area'].strip()
            # report hours count elapsed hours from local midnight of the system clock; Baja California follows
            # US Pacific daylight saving (one 23-hour and one 25-hour day), the SIN and BCS have no DST in 2023.
            tz = zoneinfo.ZoneInfo({'BCA': 'America/Tijuana', 'BCS': 'Etc/GMT+7'}.get(r['Sistema'], 'Etc/GMT+6'))
            utc = datetime.datetime.combine(day, datetime.time(0), tzinfo=tz).astimezone(datetime.timezone.utc).replace(tzinfo=None) + datetime.timedelta(hours=int(r['Hora']) - 1)
            local = utc + datetime.timedelta(hours=MX_NODES[area][3])   # fixed model clock of the node
            rows.append((local, area, float(r['Estimacion de Demanda por Balance (MWh)'])))
    frame = pd.DataFrame(rows, columns=['time', 'area', 'MW']).pivot_table(index='time', columns='area', values='MW', aggfunc='sum').reset_index()
    frame = frame[(frame.time >= '2023-01-01') & (frame.time < '2024-01-01')]
    # the DST conversion moves one BCA hour outside the year at each end; fill with the neighbouring hour
    frame = frame.set_index('time').reindex(pd.date_range('2023-01-01', periods=8760, freq='h')).interpolate(limit_direction='both').rename_axis('time').reset_index()
    return hourly_from_local(frame, list(MX_NODES))


def _mx_table(lines, start_pat, ncols):
    """Positional parse of a PRODESEN layout table: rows = states (names may wrap over up to three
    lines, numbers may sit on a line of their own), columns located from the TOTAL row."""
    txt = lines
    s = next(i for i, l in enumerate(txt) if re.search(start_pat, l))
    e = next(i for i in range(s + 1, len(txt)) if re.match(r'\s*TOTAL', txt[i]))
    total = txt[e]
    cols = [m.end() for m in re.finditer(r'[\d,]+', total)][-ncols:]
    def norm(n):
        n = re.sub(r'\s+', ' ', n).strip()
        return {'Veracruz Ignacio de la Llave': 'Veracruz de Ignacio de la Llave', 'Texas EE.UU.': 'Texas, EE.UU.'}.get(n, n)
    body = []
    for l in txt[s + 1:e]:
        letters = re.sub(r'[^A-Za-zÁÉÍÓÚÑáéíóúñ]', '', l)
        if not l.strip() or (letters and letters.isupper()):
            continue
        name = re.sub(r'\d[\d,]*|/', '', l).strip().rstrip(',').strip()
        nums = [(m.end(), float(m.group().replace(',', ''))) for m in re.finditer(r'\d[\d,]*', l)]
        body.append((name, nums))
    out = {}
    i = 0
    while i < len(body):
        frag, vals = [], None
        j = i
        while j < len(body):
            name, nums = body[j]
            if name:
                frag.append(name)
            if nums:
                vals = nums
            j += 1
            joined = norm(' '.join(frag))
            if vals is not None and joined in MX_STATE_GCR:
                # a longer name may still follow (Baja California -> Baja California Sur)
                while j < len(body) and body[j][0] and not body[j][1] and norm(joined + ' ' + body[j][0]) in MX_STATE_GCR:
                    joined = norm(joined + ' ' + body[j][0]); j += 1
                break
        if vals is None or joined not in MX_STATE_GCR:
            raise ValueError(f'could not parse rows near {frag!r}')
        row = [0.] * ncols
        for pos, v in vals:
            row[int(np.argmin([abs(pos - c) for c in cols]))] += v
        out[joined] = row
        i = j
    return out


def mx_fleet(folder, nodes):
    import subprocess
    txt = subprocess.run(['pdftotext', '-layout', str(folder / 'raw/prodesen24-38A1.pdf'), '-'], capture_output=True, text=True).stdout.splitlines()
    fix = {'Baja California': 'Baja California', 'Baja California Sur': 'Baja California Sur', 'Veracruz Ignacio de la Llave': 'Veracruz de Ignacio de la Llave',
           'Veracruz de Ignacio de la Llave': 'Veracruz de Ignacio de la Llave', 'Michoacán de Ocampo': 'Michoacán de Ocampo', 'Quintana Roo': 'Quintana Roo',
           'Coahuila de Zaragoza': 'Coahuila de Zaragoza', 'Estado de México': 'Estado de México', 'Ciudad de México': 'Ciudad de México', 'San Luis Potosí': 'San Luis Potosí'}
    def norm(n):
        n = re.sub(r'\s+', ' ', n).strip()
        return fix.get(n, n)
    recs = []
    def add(table, techs, source):
        for state, vals in table.items():
            state = norm(state)
            if state not in MX_STATE_GCR:
                raise ValueError(f'unmapped state {state!r} in {source}')
            for t, v in zip(techs, vals[:-1]):
                if t and v > 0:
                    recs.append(dict(name=f'{state} {t} ({source})', duid='', bus=MX_STATE_GCR[state], technology=t, capacity_MW=v, energy_MWh=None,
                                     active=True, source=f'PRODESEN 2024-2038 Anexo 1 {source}, 31 Dec 2023, state totals mapped to control regions', needs_review=False, note=''))
    # A1.8.1 CFE: carbo, CC, comb interna, eolica, FV, geo, hidro, nuclear, termo conv, turbogas, baterias, total
    add(_mx_table(txt, r'CUADRO A1\.8\.1', 12), ['coal', 'gas', 'oil', 'wind', 'solar', 'geothermal', 'hydro', 'nuclear', 'oil', 'gas', 'battery'], 'Cuadro A1.8.1 (CFE)')
    add(_mx_table(txt, r'CUADRO A1\.8\.2', 3), ['gas', 'wind'], 'Cuadro A1.8.2 (PIE)')
    add(_mx_table(txt, r'CUADRO A1\.8\.3', 4), ['gas', 'oil', 'gas'], 'Cuadro A1.8.3 (PEMEX)')
    add(_mx_table(txt, r'CUADRO A1\.8\.4', 5), ['gas', 'oil', 'oil', 'gas'], 'Cuadro A1.8.4 (permisionarios termicos)')
    # A1.8.5 (wind) and A1.8.6 (PV) are printed side by side; split at column 55
    s = next(i for i, l in enumerate(txt) if 'CUADRO A1.8.5' in l); e = next(i for i in range(s, len(txt)) if 'CUADRO A1.8.7' in txt[i])
    left = [l[:56] for l in txt[s:e]]; right = [l[56:] for l in txt[s:e]]
    add(_mx_table(left, r'CUADRO A1\.8\.5', 2), ['wind'], 'Cuadro A1.8.5 (eolica permisionarios)')
    add(_mx_table(right, r'CUADRO A1\.8\.6', 2), ['solar'], 'Cuadro A1.8.6 (fotovoltaica permisionarios)')
    add(_mx_table(txt, r'CUADRO A1\.8\.7', 6), ['biomass', 'gas', 'geothermal', 'hydro', 'battery'], 'Cuadro A1.8.7 (otros permisionarios)')
    # Termoeléctrica convencional in Mexico is mostly fuel oil (combustóleo): mapped to oil (ASSUMPTION); cogeneración eficiente to gas.
    return recs


def mx_links(folder):
    """Cross-control-region links from PRODESEN Anexo 1 Cuadro A1.6 (circuits and kV between the 53 transmission regions)."""
    import subprocess
    txt = subprocess.run(['pdftotext', '-layout', str(folder / 'raw/prodesen24-38A1.pdf'), '-'], capture_output=True, text=True).stdout.splitlines()
    s = next(i for i, l in enumerate(txt) if 'CUADRO A1.6' in l); e = next(i for i in range(s, len(txt)) if 'CUADRO A1.7' in txt[i])
    gcr_of = {'01-CENTRAL': 'CEN', '02-ORIENTAL': 'ORI', '03-OCCIDENTAL': 'OCC', '04-NOROESTE': 'NOR', '05-NORTE': 'NTE', '06-NORESTE': 'NES',
              '07-BAJA CALIFORNIA': 'BCA', '07-BAJA CALIFORNIA SUR': 'BCS'}
    region_gcr = {}; pairs = []; current = None; pair = None
    for l in txt[s + 1:e]:
        h = l.strip()
        if h in gcr_of:
            current = gcr_of[h]; continue
        m = re.findall(r'([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ .\-]*?) \((\d{1,2})\)', l)
        if len(m) == 2 and current:
            (n1, r1), (n2, r2) = m
            region_gcr.setdefault(int(r1), current)
            pair = (int(r1), int(r2), n1.strip(), n2.strip()); continue
        if pair and re.search(r'\b(400|230|161|138|115|85|69)\b', l) and re.search(r'[A-Za-z]', l) and not re.search(r'\(\d{1,2}\)', l):
            kvs = [int(x) for x in re.findall(r'\b(400|230|161|138|115|85|69)\b', l)]
            pairs.append((pair, kvs[-1]))
    # The table lists cross-links under the home section with either region first, so section order is not a
    # reliable membership rule. PRODESEN numbers the 53 transmission regions by control region; that numbering is
    # the authority here (Noroeste 1-9, Norte 10-18, Noreste 19-28, Occidental 29-39, Central 40-44, Oriental 45-63,
    # Peninsular 64-76, Baja California 77-84, Baja California Sur 85-98).
    ranges = [(1, 9, 'NOR'), (10, 18, 'NTE'), (19, 28, 'NES'), (29, 39, 'OCC'), (40, 44, 'CEN'), (45, 63, 'ORI'), (64, 76, 'PEN'), (77, 84, 'BCA'), (85, 98, 'BCS')]
    region_gcr = {r: g for lo, hi, g in ranges for r in range(lo, hi + 1)}
    links = []
    for (r1, r2, n1, n2), kv in pairs:
        g1, g2 = region_gcr.get(r1), region_gcr.get(r2)
        if g1 and g2 and g1 != g2:
            links.append(dict(a=g1, b=g2, kv=kv, regions=f'{n1} ({r1}) - {n2} ({r2})'))
    return links, region_gcr


def mx_network(nodes, peak, folder):
    links, region_gcr = mx_links(folder)
    by = collections.defaultdict(list)
    for L in links:
        a, b = sorted([L['a'], L['b']]); by[(a, b)].append(L)
    src = 'PRODESEN 2024-2038 Anexo 1 Cuadro A1.6 (enlaces entre regiones al 31 de diciembre de 2023: circuits and voltage per link), raw/prodesen24-38A1.pdf; MW capacity per circuit by the voltage rating rule (ASSUMPTION: 400 kV 1200, 230 kV 450, 161 kV 250, 115 kV 120 MW)'
    cors = []
    for (a, b), Ls in sorted(by.items()):
        cap = sum(RATING_MW[L['kv']] for L in Ls)
        kv = max(L['kv'] for L in Ls)
        c = corridor(a, b, nodes, kv, len(Ls), cap, cap, src,
                     f"{len(Ls)} inter-region circuits: " + '; '.join(f"{L['regions']} {L['kv']} kV" for L in Ls)[:900])
        cors.append(c)
    return dict(schema_version=2, scenario='mexico',
                provenance=dict(kind='derived', source='Nine control regions (seven SIN gerencias plus the isolated Baja California and Baja California Sur systems); inter-region circuits and voltages from PRODESEN 2024-2038 Anexo 1 Cuadro A1.6 with capacity by the per-voltage rating rule (PRODESEN 2024-2038 publishes no MW link capacities); BCA/BCS have no synchronous link to the SIN and none to each other'),
                voltage_kV=400, flow_model='transport', greenfield_policy='rebuildable',
                nodes=[], corridors=cors, interfaces=[], zone_limits={}, projects=[]), region_gcr


# ----------------------------------------------------------------------------- BRAZIL
BR_NODES = {'N': ('Norte', -1.4558, -48.4902), 'NE': ('Nordeste', -8.0476, -34.8770), 'SE': ('Sudeste/Centro-Oeste', -23.5505, -46.6333), 'S': ('Sul', -25.4284, -49.2733)}


def br_demand(folder):
    d = pd.read_csv(folder / 'raw/CURVA_CARGA_2023.csv', sep=';')
    d['time'] = pd.to_datetime(d.din_instante)
    frame = d.pivot_table(index='time', columns='id_subsistema', values='val_cargaenergiahomwmed').reset_index()
    return hourly_from_local(frame, list(BR_NODES))


def br_fleet(folder, nodes):
    c = pd.read_csv(folder / 'raw/CAPACIDADE_GERACAO.csv', sep=';')
    c['ent'] = pd.to_datetime(c.dat_entradaoperacao, errors='coerce'); c['des'] = pd.to_datetime(c.dat_desativacao, errors='coerce')
    c = c[(c.ent <= '2023-12-31') & (c.des.isna() | (c.des >= '2023-01-01'))]
    recs = []
    for _, r in c.iterrows():
        tipo, comb = str(r.nom_tipousina).strip(), str(r.nom_combustivel).strip().upper()
        if tipo == 'HIDROELÉTRICA':
            t = 'hydro'
        elif tipo == 'EOLIELÉTRICA':
            t = 'wind'
        elif tipo == 'FOTOVOLTAICA':
            t = 'solar'
        elif tipo == 'NUCLEAR':
            t = 'nuclear'
        elif 'CARV' in comb:
            t = 'coal'
        elif 'GÁS' in comb and 'DIESEL' not in comb:
            t = 'gas'
        elif 'BIOMASSA' in comb:
            t = 'biomass'
        elif 'RESÍDUO' in comb:
            t = 'waste'
        elif 'ÓLEO' in comb or 'DIESEL' in comb:
            t = 'oil'
        else:
            t = 'other'
        bus = str(r.id_subsistema).strip()
        note = ''
        if bus == 'PY':
            bus = 'SE'; note = 'Itaipu Paraguayan-side units (ONS subsystem PY): Brazil purchases most of this energy, so they are placed in SE (ASSUMPTION)'
        recs.append(dict(name=str(r.nom_usina).strip(), duid=str(r.cod_equipamento).strip(), bus=bus, technology=t, capacity_MW=float(r.val_potenciaefetiva),
                         energy_MWh=None, active=True, source='ONS open data CAPACIDADE_GERACAO (unit-level effective power, subsystem, entry/exit dates) filtered to units in operation during 2023',
                         needs_review=bus == 'PY', note=note))
    return recs


def br_hydro_energy(folder):
    energy = collections.defaultdict(float)
    for f in tqdm(sorted(glob.glob(str(folder / 'raw/GERACAO_USINA-2_2023_*.csv'))), desc='ONS generation'):
        g = pd.read_csv(f, sep=';', usecols=['id_subsistema', 'nom_tipousina', 'val_geracao'])
        g = g[g.nom_tipousina == 'HIDROELÉTRICA']
        s = g.groupby('id_subsistema').val_geracao.sum() / 1000.  # GWh
        for z, v in s.items():
            energy['SE' if z == 'PY' else z] += v
    return dict(energy)


def br_network(nodes, peak):
    pen = 'ONS PEN 2023 presentation, slide "Principais limites de intercâmbio entre subsistemas" (2023 row), raw/PEN2023_apresentacao.pdf'
    obs = 'ONS open data INTERCAMBIO_NACIONAL_2023 (hourly measured interchange), raw/INTERCAMBIO_NACIONAL_2023.csv'
    cors = [
        corridor('N', 'NE', nodes, 500, 4, 11000, 13000, pen, 'no single published pairwise limit; corridor capacities set to the receiving-NE (11,000) and exporting-NE (13,000) interface values as non-binding proxies; interfaces below bind'),
        corridor('N', 'SE', nodes, 500, 4, 4460, 4200, pen, 'Imperatriz->SE 4,460 MW (N->SE) and SE->Xingu 4,200 MW (SE->N) from the PEN 2023 table'),
        corridor('NE', 'SE', nodes, 500, 4, 13000, 5000, pen, 'SE->Imperatriz 5,000 MW (SE->NE); NE->SE bounded by the EXPNE interface (13,000) as a proxy'),
        corridor('SE', 'S', nodes, 500, 4, 11846, 6846, pen, 'Recebimento SUL 11,846 MW (SE->S) and Exportação SUL 6,846 MW (S->SE), 2023 row'),
    ]
    interfaces = [
        dict(id='RSE_NNE', members=[dict(corridor='N_SE', direction=1), dict(corridor='NE_SE', direction=1)], limit_MW=14159., source=pen, note='Recebimento SE-NNE 2023: 14,159 MW', cut=True),
        dict(id='RNE', members=[dict(corridor='N_NE', direction=1), dict(corridor='NE_SE', direction=-1)], limit_MW=11000., source=pen, note='RNE 2023: 11,000 MW received by NE from N and SE', cut=True),
        dict(id='EXPNE', members=[dict(corridor='N_NE', direction=-1), dict(corridor='NE_SE', direction=1)], limit_MW=13000., source=pen, note='EXPNE 2023: 13,000 MW exported by NE to N and SE', cut=True),
    ]
    km = distance((nodes['NE']['latitude'], nodes['NE']['longitude']), (nodes['SE']['latitude'], nodes['SE']['longitude']))
    projects = [
        project('Graca_Aranha_Silvania_HVDC', [dict(corridor='NE_SE', capacity_MW=4000., reverse_capacity_MW=4000.)], annual_usd=km * 4000 * COST_USD_PER_MW_KM_YR,
                status='+/-800 kV HVDC bipole Graça Aranha (MA) - Silvânia (GO), auctioned 2023, expected 2029', source='ANEEL transmission auction 01/2023 lot 1; EPE PDE 2032',
                increments={'interfaces': {'RSE_NNE': 4000., 'EXPNE': 4000., 'RNE': 4000.}}, note='4,000 MW bipole; cost by the km rule and interface increments equal to the bipole capacity (ASSUMPTIONS)', year=2029),
    ]
    return dict(schema_version=2, scenario='brazil',
                provenance=dict(kind='reported', source='Four ONS subsystems with the 2023 interchange limits published in the ONS PEN 2023 (pairwise where stated, cut-set interfaces for receiving/exporting limits); measured 2023 hourly interchange archived for verification; corridor lengths between representative cities'),
                voltage_kV=500, flow_model='transport', greenfield_policy='rebuildable',
                nodes=[], corridors=cors, interfaces=interfaces, zone_limits={}, projects=projects)


# ----------------------------------------------------------------------------- pack assembly
COUNTRIES = {
    'australia': dict(nodes=AU_NODES, tz=10, demand=au_demand, fleet=au_fleet, network=au_network, feeder_kv=22., line_km=3.0, feeder_cost=40000.,
                      demand_kind='AEMO NEMWEB aggregated price and demand (5-minute TOTALDEMAND, regional operational demand) averaged to hourly',
                      time_basis='AEST (UTC+10, no daylight saving in the NEM market clock); settlement timestamps mark interval end and are mapped to the interval-start hour',
                      fleet_date='2023-12-31 (registration list filtered by the MMSDM DUDETAILSUMMARY December 2023 snapshot)', currency='AUD'),
    'new_zealand': dict(nodes=NZ_NODES, tz=12, demand=nz_demand, fleet=nz_fleet, network=nz_network, feeder_kv=11., line_km=1.5, feeder_cost=40000.,
                        demand_kind='EMI Grid_export metered offtake by point of connection (kWh per trading period), mapped to EMI zones via the Network Supply Points table and summed to hourly',
                        time_basis='NZST (UTC+12) fixed model clock; trading periods converted through Pacific/Auckland daylight-saving rules',
                        fleet_date='2023-12-31 by commissioning and decommissioning dates', currency='NZD'),
    'mexico': dict(nodes=MX_NODES, tz=-6, demand=mx_demand, fleet=mx_fleet, network=None, feeder_kv=13.8, line_km=2.0, feeder_cost=40000.,
                   demand_kind='CENACE Estimación de la Demanda Real por Balance, daily settlement reports (latest settlement), hourly MWh by control area',
                   time_basis='Report hour 1-24 taken as the system clock: SIN areas UTC-6, BCA UTC-8, BCS UTC-7 (no daylight saving in 2023 except the border; ASSUMPTION that reports use standard time)',
                   fleet_date='2023-12-31 (PRODESEN Anexo 1 state totals mapped to control regions)', currency='MXN'),
    'brazil': dict(nodes=BR_NODES, tz=-3, demand=br_demand, fleet=br_fleet, network=br_network, feeder_kv=13.8, line_km=2.0, feeder_cost=40000.,
                   demand_kind='ONS open data CURVA_CARGA_2023 (hourly load per subsystem, MWmed)',
                   time_basis='Brasília time (UTC-3, no daylight saving in 2023)', fleet_date='2023 operating units (entry <= 2023-12-31, exit >= 2023-01-01)', currency='BRL'),
}


def prepare(region):
    spec = COUNTRIES[region]; folder = ROOT / region
    for name in ['profiles', 'greenfield', 'brownfield', 'results']:
        (folder / name).mkdir(exist_ok=True)
    nodes = {k: dict(id=k, latitude=v[1], longitude=v[2], location_basis=f'{v[0]}: representative city used as LOCATION PROXY for weather and corridor length; not an electrical boundary',
                     utc_offset_hours=(v[3] if len(v) > 3 else spec['tz'])) for k, v in spec['nodes'].items()}
    frame = spec['demand'](folder)
    records = spec['fleet'](folder, nodes)
    extra = {}
    if region == 'new_zealand':
        extra['hydro_energy_GWh'] = nz_hydro_factors(folder, nodes)
    if region == 'brazil':
        extra['hydro_energy_GWh'] = br_hydro_energy(folder)
    frame.to_csv(folder / 'demand.csv', index=False)
    pd.DataFrame(nodes.values()).to_csv(folder / 'nodes.csv', index=False)
    pd.DataFrame(records).to_csv(folder / 'inventory_audit.csv', index=False)
    group = collections.defaultdict(list)
    for r in records:
        if r['capacity_MW'] > 0:
            group[(r['bus'], r['technology'], r['active'])].append(r)
    assets = []
    for (bus, tech, active), rs in group.items():
        cap = sum(x['capacity_MW'] for x in rs); params = PARAMS.get(tech, {})
        a = dict(id=bus + '_' + tech + ('_active' if active else '_inactive'), bus=bus, technology=tech, capacity_MW=cap, annual_fixed_cost_usd=cap * params.get('fixed', 10000))
        if tech in STORAGE:
            e = sum(float(x['energy_MWh']) for x in rs if x['energy_MWh'] is not None and math.isfinite(float(x['energy_MWh'])))
            if e <= 0:
                e = cap * STORAGE[tech][0]
            a.update(energy_MWh=round(e, 1), b1_fraction=.4, efficiency_store=STORAGE[tech][1], efficiency_dispatch=STORAGE[tech][1], availability=1 if active else 0)
        else:
            a.update(availability=params['availability'] if active else 0, marginal_cost_usd_per_MWh=params['marginal'], emissions_t_per_MWh=params['emissions'])
            if 'energy_factor' in params:
                cf = params['energy_factor']
                if tech == 'hydro' and 'hydro_energy_GWh' in extra and bus in extra['hydro_energy_GWh']:
                    cf = min(1., extra['hydro_energy_GWh'][bus] * 1000 / (cap * 8760))   # measured 2023 zonal hydro energy / capacity (documented in assumptions.json)
                a['annual_capacity_factor'] = round(cf, 4) if active else 0
            if tech == 'nuclear':
                a['p_min_pu'] = 0.
        assets.append(a)
    peak = float(frame[list(nodes)].sum(axis=1).max()); area_peak = {k: float(frame[k].max()) for k in nodes}
    limits = {k: new_limits(peak) for k in nodes}
    netnodes = [dict(id=k, demand_share=float(frame[k].sum() / frame[list(nodes)].to_numpy().sum()), profiles_csv='profiles/' + k + '.csv', new_capacity_limits_MW=limits[k]) for k in nodes]
    if region == 'mexico':
        net, region_gcr = mx_network(nodes, peak, folder)
        js(folder / 'raw/prodesen_region_gcr.json', {str(k): v for k, v in sorted(region_gcr.items())})
    else:
        net = spec['network'](nodes, peak)
    net['nodes'] = netnodes
    js(folder / 'network.json', net)
    js(folder / 'inventory.json', dict(schema_version=1, scenario=region, provenance=dict(kind='derived', source='See raw sources and inventory_audit.csv. ' + spec['fleet_date'], year=2023), assets=assets))
    dist = dict(feeder_kv=spec['feeder_kv'], feeder_peak_kW=5000., line_r1_ohm_km=.25, line_x1_ohm_km=.33, line_km=spec['line_km'], v_band_pu=[.95, 1.05], volt_var_pf=.95,
                volt_var_deadband_pu=1.03, line_upgrade_impedance_factor=.5, line_upgrade_cost_per_feeder_yr=spec['feeder_cost'], line_normal_amps={'l1': 400., 'l2': 400.}, line_upgrade_normal_amps={'l1': 600., 'l2': 600.})
    ass = dict(demand_source_type=spec['demand_kind'] + (f"; {frame.attrs['negative_hours_floored']} negative operational-demand hours floored at zero" if frame.attrs.get('negative_hours_floored') else ''), time_basis=spec['time_basis'], base_demand_year=2023, fleet_date=spec['fleet_date'],
               network='zonal transport model with published inter-area limits where the operator publishes them; see network.json provenance and per-corridor sources',
               line_parameter_basis=f'ASSUMED per-voltage circuit ratings {RATING_MW} and {COST_USD_PER_MW_KM_YR} USD/MW-km-year; published project capex annualized at CRF {CRF} after conversion at {FX[spec["currency"]]} USD per {spec["currency"]} (2023 average)',
               candidate_site_basis='one resource point per demand region; no parcel-level suitability claim; broad capacity bounds are assumptions',
               distribution_basis=f'ASSUMED two-section representative feeder at {spec["feeder_kv"]} kV; section length calibrated so the bare peak load sits inside the voltage band; 400A normal, 600A reinforced ratings',
               existing_operating_parameters=PARAMS, storage_duration_fallback_hours={k: v[0] for k, v in STORAGE.items()}, storage_boundary='cyclic SOC; no free initial/final energy',
               voll_usd_per_MWh=50000, unserved_fraction_limit=1e-4, co2_limit_Mt=1000., co2_policy='Permissive baseline; edit this fixed assumption for a policy scenario. Agents cannot relax the cap.',
               imports='No externally fixed import entitlement; model is a domestic/regionally isolated supply counterfactual' + ('; BCA (WECC ties) and BCS are isolated islands in this model' if region == 'mexico' else '') + ('; Itaipu Paraguayan-side units are placed in SE' if region == 'brazil' else ''),
               fuel_model='continuous dispatch with fixed availability factors, no unit commitment; hydro energy-limited' + (' with measured 2023 zonal hydro energy' if region in ('new_zealand', 'brazil') else ' with the default annual factor'),
               spatial_uncertainty='fleet assigned to zones by the source file (region, POC zone, subsystem or state-to-control-region mapping); representative-city weather points',
               costs='costs_us.json (NREL ATB based) used for new build in USD; local cost data not applied (ASSUMPTION)', hydro_energy_GWh_2023=extra.get('hydro_energy_GWh'))
    js(folder / 'assumptions.json', ass)
    js(folder / 'config.json', dict(id=region, description=f'{region}: {len(nodes)} demand regions; existing-generation brownfield campaign (greenfield retained as a deterministic baseline). Actual operator data plus explicit network/resource and operating assumptions.',
                                    cost_file='costs_us.json', global_new_capacity_limits_GW=dict(wind=peak * 3 / 1000, solar=peak * 5 / 1000, nuclear=peak / 1000), distribution=dist))
    for mode in ['greenfield', 'brownfield']:
        js(folder / mode / 'mode.json', dict(region=region, mode=mode, shared_inputs=['../config.json', '../assumptions.json', '../demand.csv', '../network.json', '../profiles'], existing_assets='../inventory.json' if mode == 'brownfield' else None))
    js(folder / 'input_audit.json', dict(source_inventory_rows=len(records), retained_positive_capacity_rows=sum(r['capacity_MW'] > 0 for r in records), model_asset_groups=len(assets),
                                         all_capacity_MW=sum(r['capacity_MW'] for r in records), active_capacity_MW=sum(r['capacity_MW'] for r in records if r['active']),
                                         capacity_by_technology_MW={t: round(sum(r['capacity_MW'] for r in records if r['technology'] == t and r['active']), 1) for t in sorted({r['technology'] for r in records})},
                                         assignment_review_rows=sum(bool(r['needs_review']) for r in records), demand_hours=len(frame), demand_regions=len(nodes), peak_MW=peak, area_peak_MW=area_peak))
    print(region, len(nodes), len(records), len(assets), 'peak', round(peak), flush=True)


if __name__ == '__main__':
    for region in (sys.argv[1:] or ['australia', 'new_zealand', 'brazil', 'mexico']):
        prepare(region)
