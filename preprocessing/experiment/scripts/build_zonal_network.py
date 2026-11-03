"""Assemble schema_version 2 zonal networks (real adjacency, published limits) from the archived
topology drafts in <region>/raw/topology/. The full-mesh assumption file is kept as network_fullmesh.json.

Every number that is NOT taken from a cited document is an ASSUMPTION declared below and written into
each corridor's `note`/`source` so agents and readers can see it. Run with --dry to print without writing."""
from pathlib import Path
import argparse, json, math, csv, copy, re
import os
ROOT = Path(os.environ.get('GRID_DATA_ROOT', str(Path(__file__).resolve().parents[3] / 'data'))).expanduser().resolve() / 'experiment'

ASSUMPTIONS = {
    # Per-circuit thermal rating used for NEW candidate circuits when the source states none (typical planning values).
    "new_circuit_rating_MW_by_kV": {765: 2500., 500: 1800., 400: 1200., 345: 1000., 275: 600.},
    # Existing pairwise capacity when the operator publishes only zonal (not pairwise) limits: a deliberately
    # non-binding proxy so that the PUBLISHED zone/interface limits are the binding constraint.
    "unpublished_existing_capacity_MW": 50000.,
    "new_line_cost_usd_per_MW_km_year": 200.,
    "reverse_rating_equals_forward_unless_stated": True,
    "candidates": "one new circuit per real adjacency (expansion along the existing route); 765 kV rating where a 765 kV project is planned on that pair, otherwise the region's standard voltage",
}

def distance(a, b):
    la, lo, lb, lob = map(math.radians, [a[0], a[1], b[0], b[1]])
    return 6371 * 2 * math.asin(min(1, math.sqrt(math.sin((la - lb) / 2) ** 2 + math.cos(la) * math.cos(lb) * math.sin((lo - lob) / 2) ** 2)))

def load_region(region):
    folder = ROOT / region
    for name in ("network_fullmesh_dc_v1.json", "network_fullmesh.json", "network.json"):
        if (folder / name).exists():
            v1 = json.loads((folder / name).read_text())
            if v1.get("schema_version") == 1:
                break
    assert v1["schema_version"] == 1, "expected a schema-version-1 full-mesh file as the node base"
    nodes = {r["id"]: r for r in csv.DictReader((folder / "nodes.csv").open())}
    return folder, v1, nodes

def corridor(a, b, nodes, kv, existing_circuits, source, *, existing_capacity=None, existing_reverse=None, max_new=1, rating=None, length_km=None, note=None, planned=None):
    km = length_km or distance((float(nodes[a]["latitude"]), float(nodes[a]["longitude"])), (float(nodes[b]["latitude"]), float(nodes[b]["longitude"])))
    rating = rating or ASSUMPTIONS["new_circuit_rating_MW_by_kV"][kv]
    c = dict(id=f"{a}_{b}", bus0=a, bus1=b, length_km=round(km, 1), voltage_kV=kv, circuit_rating_MW=rating,
             max_new_circuits=max_new, existing_circuits=existing_circuits,
             new_annual_cost_per_circuit_usd=round(km * rating * ASSUMPTIONS["new_line_cost_usd_per_MW_km_year"], 0),
             existing_annual_fixed_cost_per_circuit_usd=0, source=source)
    if existing_capacity is not None:
        c["existing_capacity_MW"] = existing_capacity
        if existing_reverse is not None:
            c["existing_reverse_capacity_MW"] = existing_reverse
    if note: c["note"] = note
    if planned: c["planned_projects"] = planned
    return c

ZONE_DIR = re.compile(r"\b([A-K])\s*->\s*([A-K])\b")

def _letters(text):
    return set(re.findall(r"\b([A-K])\b", text or ""))

def pick_multi(values, into_zones):
    """Forward and reverse picks for a cut-set interface. A value is reverse when the zone letters left of '->'
    lie inside the interface's destination zones (e.g. 'K->(I+J)' for ConEd-LIPA, '(F+G)->E' for Central East)."""
    def is_reverse(v):
        d = v.get("direction", "") or ""
        if "->" not in d:
            return False
        lhs, rhs = d.split("->", 1)
        return bool(_letters(lhs)) and _letters(lhs) <= into_zones and not (_letters(rhs) and _letters(rhs) <= into_zones)
    def pick(vals):
        for season in ("Summer 2025", "Summer 2024 and Summer 2025", "Summer 2024"):
            for v in vals:
                if "normal thermal" in v["type"] and v.get("season") == season and v.get("limit_mw"):
                    return v
        for v in vals:
            if v.get("limit_mw") and "mars" in v["type"].lower() and "2030" not in str(v.get("season")):
                return v
        return None
    fwd = [v for v in values if not is_reverse(v)]; rev = [v for v in values if is_reverse(v)]
    return pick(fwd), pick(rev)


def directional_limits(values):
    """Split an interface's values by direction (zone letters parsed from the 'direction' text) and pick, per
    direction, the latest-season normal thermal limit, else the MARS emergency limit. Operational TTC and PAR
    schedule figures are different quantities and are ignored. Ties within a season take the larger value
    (different operating conditions) and record it."""
    out = {}
    for v in values:
        if not v.get("limit_mw"):
            continue
        t = v["type"].lower()
        if "operational" in t or "ttc" in t or "par schedule" in t or "assistance" in t:
            continue
        m = ZONE_DIR.search(v.get("direction", "") or "")
        if not m:
            continue
        key = (f"ZON{m.group(1)}", f"ZON{m.group(2)}")
        rank = (2 if "normal" in t else 1 if "mars" in t else 0, 2 if "2025" in str(v.get("season")) else 1 if "2024" in str(v.get("season")) else 0, v["limit_mw"])
        if key not in out or rank > out[key][0]:
            out[key] = (rank, v)
    return {k: v for k, (r, v) in out.items()}

# ---------------------------------------------------------------- NYISO
def build_nyiso():
    folder, v1, nodes = load_region("nyiso")
    top = folder / "raw/topology"
    adj = json.loads((top / "adjacency_draft.json").read_text())
    ifs = json.loads((top / "interfaces_draft.json").read_text())["interfaces"]
    pairs = [(e["from"], e["to"], e["corridor_name"]) for e in adj["edges"]]
    limits, multi = {}, []
    for x in ifs:
        zp = [tuple(p) for p in x["corridors_zone_pairs"]]
        if len(zp) == 1:
            for (a, b), v in directional_limits(x["values"]).items():
                cite = f"{x['interface']}: {v['limit_mw']} MW {v['type']} ({v.get('season')}), {v['source'].get('doc')} p.{v['source'].get('pdf_page')}" + (f", condition: {v['condition']}" if v.get("condition") else "")
                cur = limits.get((a, b))
                # thermal normal beats MARS; among equals keep the more restrictive
                better = cur is None or ("normal" in v["type"] and "normal" not in cur[1]["type"]) or (("normal" in v["type"]) == ("normal" in cur[1]["type"]) and v["limit_mw"] < cur[1]["limit_mw"])
                if better:
                    limits[(a, b)] = (cite, v)
        else:
            into = {b[-1] for a, b in zp}
            v, rv = pick_multi(x["values"], into)
            if v is not None:
                multi.append((x, zp, v, f"{x['interface']}: {v['limit_mw']} MW {v['type']} ({v.get('season')}), {v['source'].get('doc')} p.{v['source'].get('pdf_page')}", rv))
    corridors = []
    for a, b, name in pairs:
        f = limits.get((a, b)); r = limits.get((b, a))
        cap = f[1]["limit_mw"] if f else ASSUMPTIONS["unpublished_existing_capacity_MW"]
        src = f"{name} corridor; adjacency from {adj['primary_source']['doc']} {adj['primary_source']['figure']} p.{adj['primary_source']['pdf_page']}; forward ({a}->{b}) capacity from " + (f[0] if f else "NO PUBLISHED VALUE (assumption: non-binding proxy)")
        corridors.append(corridor(a, b, nodes, 345, 1, src, existing_capacity=cap, existing_reverse=(r[1]["limit_mw"] if r else None),
                                  note=(f"reverse ({b}->{a}) capacity from " + r[0]) if r else f"reverse ({b}->{a}) capacity assumed equal to forward (ASSUMPTION: no published value)",
                                  planned=(["Propel NY (LI PPTN Alt. 5): ~1,300-1,400 MW into ZONK, target May 2030, PSC certificate not confirmed"] if b == "ZONK" else None)))
    interfaces = []
    for x, zp, v, cite, rv in multi:
        members = []
        for a, b in zp:
            cid = f"{a}_{b}" if any(p[:2] == (a, b) for p in pairs) else f"{b}_{a}"
            members.append(dict(corridor=cid, direction=1 if cid == f"{a}_{b}" else -1))
        entry = dict(id=re.sub(r"[^A-Za-z0-9_-]+", "_", x["interface"]).strip("_"), members=members, limit_MW=float(v["limit_mw"]), source=cite, note=f"{x['sides']['from']} -> {x['sides']['to']}; sum over listed corridors")
        if rv is not None:
            entry["reverse_limit_MW"] = float(rv["limit_mw"])
            entry["note"] += f"; reverse limit {rv['limit_mw']} MW {rv['type']} ({rv.get('season')}), {rv['source'].get('doc')} p.{rv['source'].get('pdf_page')}"
        else:
            entry["note"] += "; no published reverse limit: reverse flow bounded only by corridor capacities"
        interfaces.append(entry)
    net = dict(schema_version=2, scenario="nyiso",
               provenance=dict(kind="reported", source="Zonal adjacency from NYISO 2024 RNA Appendices (MARS topology, Fig. 18); existing inter-zonal capacities from NYISO approved Summer 2024/2025 Operating Study transfer limits (Table 1.a/1.b) and RNA MARS emergency limits; interface cut-set limits kept as interfaces. Candidate circuits and costs are declared assumptions. Drafts and PDFs in raw/topology/."),
               voltage_kV=345, flow_model="transport", greenfield_policy="rebuildable", nodes=v1["nodes"], corridors=corridors, interfaces=interfaces, zone_limits={})
    return folder, net

# ---------------------------------------------------------------- MISO
def build_miso():
    folder, v1, nodes = load_region("miso")
    top = folder / "raw/topology"
    adj = json.loads((top / "adjacency_draft.json").read_text())
    z = json.loads((top / "zone_limits_draft.json").read_text())
    planned = json.loads((top / "planned_draft.json").read_text())["group_edge_summary"]
    pra = z["planning_year_2025_2026"]["pra_final"]; season = "Summer 2025"
    groups = {"0001": ["LRZ1"], "0004": ["LRZ4"], "0006": ["LRZ6"], "0027": ["LRZ2", "LRZ7"], "0035": ["LRZ3", "LRZ5"], "8910": ["LRZ8", "LRZ9", "LRZ10"]}
    ns = z["north_south_transfer"]["planning_year_2025_2026"]
    corridors = []
    for e in adj["group_adjacency"]:
        a, b = e["a"], e["b"]; key = f"{a}-{b}"
        proj = planned.get(key) or planned.get(f"{b}-{a}") or []
        kv = 765 if any("765" in p for p in proj) else 345
        if e["kind"] == "contiguous":
            corridors.append(corridor(a, b, nodes, kv, 1, f"Adjacent LRZ groups ({', '.join(e['from_lrz_pairs'])}) per {adj['map_source'] if isinstance(adj['map_source'], str) else json.dumps(adj['map_source'])[:120]}; MISO publishes no pairwise interface rating: existing capacity is a NON-BINDING PROXY (ASSUMPTION), binding constraints are the published zone import/export limits",
                                      existing_capacity=ASSUMPTIONS["unpublished_existing_capacity_MW"], planned=proj or None))
        else:
            corridors.append(corridor(a, b, nodes, 345, 1, f"MISO Midwest<->South Regional Directional Transfer path; limits N->S {ns['north_to_south']['rdt_limit_MW']} MW, S->N {ns['south_to_north']['rdt_limit_MW']} MW (RASC 2025-02-26 SRIC/SREC posting p.4); attachment to 0035 is a modelling choice (path physically runs through AECI/SPP/TVA)",
                                      existing_capacity=float(ns["north_to_south"]["rdt_limit_MW"]), existing_reverse=float(ns["south_to_north"]["rdt_limit_MW"]),
                                      note="firm TSR deduction (600 MW S->N) not applied: physical limit used", planned=None))
    zone_limits = {}
    for g, members in groups.items():
        if g == "8910":
            zone_limits[g] = dict(import_MW=float(ns["north_to_south"]["rdt_limit_MW"]), export_MW=float(ns["south_to_north"]["rdt_limit_MW"]), source="RDT limits used as the South sub-region import/export limit (RASC 2025-02-26 p.4)")
            continue
        cil = sum(pra["CIL"][season][m] for m in members); cel = sum(pra["CEL"][season][m] for m in members)
        note = "single LRZ: value as published" if len(members) == 1 else f"ASSUMPTION: sum of member LRZ limits ({'+'.join(members)}), an upper bound because intra-group flows cancel; bracket [max member, sum] = [{max(pra['CIL'][season][m] for m in members)}, {cil}] import"
        zone_limits[g] = dict(import_MW=float(cil), export_MW=float(cel), source=f"PRA 2025/26 final CIL/CEL {season} ({pra['source']} p.{pra['pages'][season]}); {note}")
    net = dict(schema_version=2, scenario="miso",
               provenance=dict(kind="derived", source="Group adjacency from the MISO LRZ map (PY2025-26 LOLE report Fig. ES-2); zonal Capacity Import/Export Limits from the 2025/26 PRA final posting; Midwest-South RDT limits from RASC posting; MISO publishes no pairwise corridor ratings, so existing corridor capacities are non-binding proxies (assumption). LRTP Tranche 1/2.1 lines recorded as planned projects per pair. Drafts and PDFs in raw/topology/."),
               voltage_kV=345, flow_model="transport", greenfield_policy="rebuildable", nodes=v1["nodes"], corridors=corridors, interfaces=[], zone_limits=zone_limits)
    return folder, net

# ---------------------------------------------------------------- ERCOT
def build_ercot():
    folder, v1, nodes = load_region("ercot")
    top = folder / "raw/topology"
    adj = json.loads((top / "adjacency_draft.json").read_text())
    ifs = {r["id"]: r for r in json.loads((top / "interfaces_draft.json").read_text())["records"]}
    plans = json.loads((top / "planned_draft.json").read_text())["records"]
    pairs = [tuple(e["pair"]) for e in adj["edges"]]
    # 765 kV projects with both endpoints mapped to weather zones (PUCT-approved import paths and STEP eastern backbone)
    p765 = {}
    for r in plans:
        if r["voltage_kv"] != 765: continue
        for seg in r["segments"]:
            a, b = seg.get("from_wz"), seg.get("to_wz")
            if a and b and a != b:
                key = tuple(sorted((a, b))); p765.setdefault(key, []).append(f"{r['id']} {seg['from']} - {seg['to']} ({seg.get('miles')} mi, {seg.get('rating_mva')} MVA); {r['status'][:70]}")
    corridors = []
    for a, b in pairs:
        key = tuple(sorted((a, b))); kv = 765 if key in p765 else 345
        corridors.append(corridor(a, b, nodes, kv, 1, f"Weather-zone adjacency read from the official ERCOT weather zone map ({adj['source']['url']}); ERCOT publishes no pairwise zone capacities: existing capacity is a NON-BINDING PROXY (ASSUMPTION), binding constraints are the published GTC interface limits below",
                                  existing_capacity=ASSUMPTIONS["unpublished_existing_capacity_MW"], rating=(7600. if kv == 765 else 3000.), planned=p765.get(key),
                                  note="new 765 kV circuit rating 7,600 MW per PUCT-approved import path rating (7,602 MVA)" if kv == 765 else "new 345 kV circuit rating 3,000 MW per Permian plan double-circuit rating (2,988 MVA)"))
    # candidate-only corridors created by the approved 765 kV import paths (not adjacent weather zones today)
    for key, proj in p765.items():
        if key not in {tuple(sorted(p)) for p in pairs}:
            a, b = key
            corridors.append(corridor(a, b, nodes, 765, 0, "No existing weather-zone adjacency; candidate created by PUCT-approved 765 kV import path(s): " + " | ".join(proj), rating=7600., planned=proj))
    ids = {c["id"] for c in corridors}
    def cid(a, b):
        return (f"{a}_{b}", 1) if f"{a}_{b}" in ids else (f"{b}_{a}", -1)
    interfaces = []
    w = ifs["WESTEX"]; val = next(v for v in w["values"] if v.get("mw") and "2025 RTP" in v.get("basis", "")) if any("2025 RTP" in v.get("basis", "") for v in w["values"] if v.get("mw")) else next(v for v in w["values"] if v.get("mw"))
    members = [dict(corridor=c, direction=d) for c, d in (cid("WEST", "NCEN"), cid("WEST", "SCEN"), cid("WEST", "NRTH"), cid("FWES", "NCEN"), cid("FWES", "SCEN"))]
    interfaces.append(dict(id="WESTEX", members=members, limit_MW=float(val["mw"]), source=f"West Texas Export GTC: {val['mw']} MW, {val['basis']} ({val['source'].get('file')} p.{val['source'].get('page')})", note=w["wz_mapping"]["best"]))
    h = ifs["N_TO_H"]; val = next(v for v in h["values"] if v.get("mw") and "2025 RTP" in v.get("basis", "")) if any("2025 RTP" in v.get("basis", "") for v in h["values"] if v.get("mw")) else next(v for v in h["values"] if v.get("mw"))
    interfaces.append(dict(id="N_TO_H", members=[dict(corridor=c, direction=d) for c, d in (cid("NCEN", "COAS"), cid("EAST", "COAS"))] if f"NCEN_COAS" in ids or f"COAS_NCEN" in ids else [dict(corridor=c, direction=d) for c, d in (cid("EAST", "COAS"),)],
                           limit_MW=float(val["mw"]), source=f"North-to-Houston GTC: {val['mw']} MW, {val['basis']} ({val['source'].get('file')} p.{val['source'].get('page')})", note=h["wz_mapping"]["best"] + "; NCEN-COAS are not adjacent weather zones so only EAST->COAS is a member"))
    net = dict(schema_version=2, scenario="ercot",
               provenance=dict(kind="derived", source="Weather-zone adjacency from the official ERCOT weather zone map (visual reading); West Texas Export and North-to-Houston GTC limits from ERCOT RTP planning documents as interface caps over mapped zone pairs; Panhandle, Valley and other sub-zonal GTCs omitted (not representable at weather-zone resolution); pairwise capacities unpublished (non-binding proxies). PUCT-approved 765 kV import paths and STEP eastern backbone as candidates. Drafts and PDFs in raw/topology/."),
               voltage_kV=345, flow_model="transport", greenfield_policy="rebuildable", nodes=v1["nodes"], corridors=corridors, interfaces=interfaces, zone_limits={})
    return folder, net

# ---------------------------------------------------------------- South Africa
def build_za():
    folder, v1, nodes = load_region("south_africa")
    top = folder / "raw/topology"
    tdp = json.loads((top / "corridors_draft.json").read_text())["corridors"]
    capd = json.loads((top / "capacity_draft_pypsa_rsa.json").read_text())
    cap = {tuple(sorted((r["area_a"], r["area_b"]))): r for r in capd["corridors"]}
    tdp_pairs = {}
    for c in tdp:
        key = tuple(sorted((c["area_a"], c["area_b"])))
        tdp_pairs.setdefault(key, []).append(c)
    corridors = []
    for key in sorted(set(cap) | set(tdp_pairs)):
        a, b = key
        r = cap.get(key); items = tdp_pairs.get(key, [])
        if r is not None and float(r["s_nom_MW"]) <= 0:
            r = None  # pair listed but no crossing line in the GIS (e.g. FS-KZN): no existing corridor
        existing_mw = float(r["s_nom_MW"]) if r else 0.0
        planned = [f"{c['name']} ({c['voltage_kv']} kV, {c['status']}{', ' + str(c['tdp_expected_year']) if c.get('tdp_expected_year') else ''}; TDP pdf p.{c['pages_pdf']})" for c in items if c["status"] != "existing"]
        existing_named = [f"{c['name']} (TDP pdf p.{c['pages_pdf']})" for c in items if c["status"] == "existing"]
        if existing_mw <= 0 and not planned:
            continue  # no existing line and nothing planned: not a corridor
        kv = 765 if any(c["voltage_kv"] == 765 and c["status"] != "existing" for c in items) else 400
        circuits = int(sum(r["circuits_by_voltage"].values())) if r else 0
        if r:
            src = (f"Existing capacity {existing_mw:.0f} MW = PyPSA-RSA method (MeridianEconomics/pypsa-rsa HEAD 89872c1) replicated on the NTCSA GCCA-2022 Existing_Lines GIS: "
                   f"per-voltage thermal ratings 275/400/765 kV = 921/1788/5512 MW, St Clair length derating, N-1 (drop largest line or x0.7); circuits by kV {r['circuits_by_voltage']}; GIS vintage 2016-2018. "
                   f"Adjacency corroborated by NTCSA TDP 2024: {'; '.join(existing_named) if existing_named else 'no named existing line in TDP text'}")
        else:
            src = "No existing inter-area line in the NTCSA GIS; candidate created by TDP 2024 planned line(s): " + " | ".join(planned)
        corridors.append(corridor(a, b, nodes, kv, 1 if r else 0, src, existing_capacity=existing_mw if r else None, length_km=(r["length_km"] / 1.25 if r else None),
                                  planned=planned or None, note=("existing circuits counted from GIS; " if r else "") + f"new circuit rating by voltage is an ASSUMPTION ({ASSUMPTIONS['new_circuit_rating_MW_by_kV'][kv]:.0f} MW at {kv} kV)"))
    net = dict(schema_version=2, scenario="south_africa",
               provenance=dict(kind="derived", source="Supply-area adjacency and planned corridors from the NTCSA Transmission Development Plan 2024 (raw/tdp.pdf, page refs per corridor); existing inter-area capacities from the PyPSA-RSA topology method applied to the NTCSA GCCA-2022 existing-lines GIS (derived numbers only; the GIS itself is not redistributed). TDP states no transfer limits; GCCA connection-capacity figures deliberately not used. Candidate circuits and costs are declared assumptions. Drafts in raw/topology/."),
               voltage_kV=400, flow_model="transport", greenfield_policy="rebuildable", nodes=v1["nodes"], corridors=corridors, interfaces=[], zone_limits={})
    return folder, net

BUILDERS = dict(nyiso=build_nyiso, miso=build_miso, ercot=build_ercot, south_africa=build_za)

def summarize(net):
    print(f"== {net['scenario']}: {len(net['corridors'])} corridors, {len(net['interfaces'])} interfaces, {len(net['zone_limits'])} zone limits")
    for c in net["corridors"]:
        ex = c.get("existing_capacity_MW"); rv = c.get("existing_reverse_capacity_MW", ex)
        print(f"  {c['id']:14s} {c['voltage_kV']}kV existing={c['existing_circuits']} cap={ex} rev={rv} new<={c['max_new_circuits']}x{c['circuit_rating_MW']} {c['length_km']}km" + (" PLANNED:" + str(len(c['planned_projects'])) if c.get('planned_projects') else ""))
    for i in net["interfaces"]:
        print(f"  IF {i['id']}: <= {i['limit_MW']} over {[m['corridor'] + ('' if m['direction'] == 1 else '(rev)') for m in i['members']]}")
    for z, l in net["zone_limits"].items():
        print(f"  ZONE {z}: import<={l.get('import_MW')} export<={l.get('export_MW')}")

# ================================================================ projects, cut checks, full-mesh ablation
def corridor_id(net, a, b):
    ids = {c["id"] for c in net["corridors"]}
    return f"{a}_{b}" if f"{a}_{b}" in ids else f"{b}_{a}" if f"{b}_{a}" in ids else None

def project(net, pid, members, annual_cost_usd, status, source, increments=None, note=None):
    net.setdefault("projects", []).append(dict(id=pid, members=members, annual_cost_usd=round(annual_cost_usd, 0), status=status, source=source,
                                               **({"increments": increments} if increments else {}), **({"note": note} if note else {})))

def km_cost(net, cid, capacity, usd_per_mw_km=None):
    c = next(x for x in net["corridors"] if x["id"] == cid)
    return c["length_km"] * capacity * (usd_per_mw_km or ASSUMPTIONS["new_line_cost_usd_per_MW_km_year"])

def apply_cut_check(net, iface, from_zones, to_zones):
    """Interface limits are cut-set limits. Recompute the members as every corridor crossing from `from_zones`
    to `to_zones` and record what the draft had missed (bypass check, item 4)."""
    crossing = []
    for c in net["corridors"]:
        if c["bus0"] in from_zones and c["bus1"] in to_zones:
            crossing.append(dict(corridor=c["id"], direction=1))
        elif c["bus1"] in from_zones and c["bus0"] in to_zones:
            crossing.append(dict(corridor=c["id"], direction=-1))
    before = {(m["corridor"], m["direction"]) for m in iface["members"]}
    after = {(m["corridor"], m["direction"]) for m in crossing}
    iface["cut"] = dict(from_zones=sorted(from_zones), to_zones=sorted(to_zones), added=sorted(c for c, d in after - before), removed=sorted(c for c, d in before - after))
    iface["members"] = crossing
    if after != before:
        print(f"  cut check {iface['id']}: added {sorted(c for c, d in after - before)} removed {sorted(c for c, d in before - after)}")

def add_projects_and_cuts(region, net):
    kv_cap = ASSUMPTIONS["new_circuit_rating_MW_by_kV"]
    if region == "nyiso":
        zones = [n["id"] for n in net["nodes"]]
        ifs = {i["id"]: i for i in net["interfaces"]}
        apply_cut_check(net, ifs["Total_East"], {"ZONA", "ZONB", "ZONC", "ZOND", "ZONE"}, {"ZONF", "ZONG", "ZONH", "ZONI", "ZONJ", "ZONK"})
        apply_cut_check(net, ifs["Central_East_Group_E_to_F_G"], {"ZONA", "ZONB", "ZONC", "ZOND", "ZONE"}, {"ZONF", "ZONG"})
        apply_cut_check(net, ifs["UPNY-SENY"], {"ZONA", "ZONB", "ZONC", "ZOND", "ZONE", "ZONF"}, {"ZONG", "ZONH", "ZONI", "ZONJ", "ZONK"})
        apply_cut_check(net, ifs["Dunwoodie_Y49_Y50_Group"], set(zones) - {"ZONJ", "ZONK"}, {"ZONJ", "ZONK"})
        apply_cut_check(net, ifs["ConEd_-_LIPA_Long_Island_import_from_NY"], set(zones) - {"ZONK"}, {"ZONK"})
        cid = corridor_id(net, "ZONI", "ZONK")
        cable = 2000.0  # USD/MW-km-year: ASSUMPTION for submarine/underground cable, ten times the overhead rule
        project(net, "Propel_NY", [dict(corridor=cid, capacity_MW=1300., reverse_capacity_MW=1300.)], km_cost(net, cid, 1300., cable),
                status="Propel NY Solutions (LI PPTN Alt. 5): firm plan, Article VII filed 2024-07-31, target in service May 2030; PSC certificate not confirmed in the drafts",
                source="NYISO Long Island PPTN selection; raw/topology/planned_draft.json; interface delta ~1,300-1,400 MW into Zone K",
                increments=dict(interfaces={"ConEd_-_LIPA_Long_Island_import_from_NY": 1300., "Dunwoodie_Y49_Y50_Group": 1300.}),
                note="capacity modelled on the I-K corridor; annual cost is an ASSUMPTION (cable cost rule), not the project's published capex")
    elif region == "miso":
        planned = json.loads((ROOT / "miso/raw/topology/planned_draft.json").read_text())["group_edge_summary"]
        bundles = {}
        for key, lines in planned.items():
            a, b = key.split("-"); cid = corridor_id(net, a, b)
            for line in lines:
                token = line.split(" ")[0]  # e.g. T2.1#40: the same project can have segments on several pairs
                bundles.setdefault(token, dict(lines=[], members=[], zones=set(), kv=765 if "765" in line else 345))
                bundles[token]["lines"].append(line); bundles[token]["members"].append(cid); bundles[token]["zones"] |= {a, b}
        for token, bd in bundles.items():
            cap = kv_cap[bd["kv"]]
            pid = re.sub(r"[^A-Za-z0-9_-]+", "_", token).strip("_")
            project(net, pid, [dict(corridor=c, capacity_MW=cap, reverse_capacity_MW=cap) for c in bd["members"]], sum(km_cost(net, c, cap) for c in bd["members"]),
                    status=("MTEP24 LRTP Tranche 2.1, approved 2024-12-12" if token.startswith("T2.1") else "MTEP21 LRTP Tranche 1, approved 2022-07"),
                    source="; ".join(bd["lines"]) + "; raw/topology/planned_draft.json; thermal rating not published",
                    increments=dict(zone_limits={z: dict(import_MW=cap, export_MW=cap) for z in sorted(bd["zones"])}),
                    note=f"ASSUMPTION: capacity {cap:.0f} MW ({bd['kv']} kV rating rule) per segment, all segments one build decision, zone import/export increments equal to that capacity; annual cost by the km rule")
    elif region == "ercot":
        paths = [(corridor_id(net, "FWES", "NCEN"), 442 * 1.609), (corridor_id(net, "WEST", "NCEN"), 443 * 1.609), (corridor_id(net, "FWES", "SCEN"), 370 * 1.609)]
        cost = sum(km * 7600. * ASSUMPTIONS["new_line_cost_usd_per_MW_km_year"] for _, km in paths)
        project(net, "PB765_import_paths", [dict(corridor=c, capacity_MW=7600., reverse_capacity_MW=7600.) for c, _ in paths], cost,
                status="PUCT approved 765 kV Permian Basin import paths, 2025-04-24; CCN stage",
                source="raw/topology/planned_draft.json (PB765_IP1/IP2/IP3, 7,602 MVA per circuit, 442/443/370 mi); WESTEX increase 12,700 -> 16,200 MW with the 765 kV plan per 2024 RTP p.24",
                increments=dict(interfaces={"WESTEX": 3500.}),
                note="bundled: the published West Texas Export increase applies to the plan as a whole; annual cost by the km rule on stated mileage")
        loop = [corridor_id(net, "NCEN", "EAST"), corridor_id(net, "EAST", "COAS"), corridor_id(net, "SOUT", "COAS"), corridor_id(net, "SCEN", "SOUT"), corridor_id(net, "NCEN", "SCEN")]
        cost = 1109 * 1.609 * 7600. * ASSUMPTIONS["new_line_cost_usd_per_MW_km_year"]
        project(net, "STEP_eastern_backbone", [dict(corridor=c, capacity_MW=7600., reverse_capacity_MW=7600.) for c in loop], cost,
                status="ERCOT Board endorsed 2025-12-09 (STEP Core Plan, Tier 1); CCNs required; in service 2030-32",
                source="raw/topology/planned_draft.json (STEP_EAST, 1,109 mi, 7,603 MVA per line); North-to-Houston increase: ASSUMPTION 3,750 MW = 2024 RTP DFW->Houston incremental transfer capability",
                increments=dict(interfaces={"N_TO_H": 3750.}), note="bundled loop; increment is an assumption tied to a published RTP transfer-capability figure, vary in sensitivity runs")
        ifs = {i["id"]: i for i in net["interfaces"]}
        apply_cut_check(net, ifs["WESTEX"], {"FWES", "WEST"}, {"NRTH", "NCEN", "SCEN", "SOUT", "EAST", "COAS"})
        apply_cut_check(net, ifs["N_TO_H"], {"NRTH", "NCEN", "EAST", "WEST", "FWES"}, {"COAS"})
        ifs["N_TO_H"]["note"] = (ifs["N_TO_H"].get("note", "") + " | bypass note: SCEN->COAS and SOUT->COAS are physical south-side paths outside this GTC and remain uncapped by it (only the proxy corridor capacity applies)").strip(" |")
    elif region == "south_africa":
        tdp = json.loads((ROOT / "south_africa/raw/topology/corridors_draft.json").read_text())["corridors"]
        for c in tdp:
            if c["status"] == "existing":
                continue
            cid = corridor_id(net, c["area_a"], c["area_b"])
            if cid is None:
                continue
            kv = 765 if c["voltage_kv"] == 765 else 400; cap = kv_cap[kv]
            pid = re.sub(r"[^A-Za-z0-9_-]+", "_", c["id"]).strip("_")
            project(net, pid, [dict(corridor=cid, capacity_MW=cap, reverse_capacity_MW=cap)], km_cost(net, cid, cap),
                    status=f"NTCSA TDP 2024: {c['status']}" + (f", expected {c['tdp_expected_year']}" if c.get("tdp_expected_year") else "") + f"; mapping confidence {c.get('mapping_confidence')}",
                    source=f"{c['name']}, TDP pdf p.{c['pages_pdf']}: \"{c['quote'][:80]}\"",
                    note=f"ASSUMPTION: capacity {cap:.0f} MW ({kv} kV rating rule); TDP states no MW; annual cost by the km rule")
    return net

def build_fullmesh(region):
    """Ablation network: every pair connected, no existing circuits, no published limits, same transport model,
    same rating and cost rules as the zonal file. Keeps only the 'assumption' world for comparison."""
    folder, v1, nodes = load_region(region)
    kv = 400 if region == "south_africa" else 345
    ids = [n["id"] for n in v1["nodes"]]
    corridors = []
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            corridors.append(corridor(a, b, nodes, kv, 0, "FULL-MESH ASSUMPTION: every pair is a candidate route; no existing circuit; no published limits", rating=(3000. if region == "ercot" else None)))
    return folder, dict(schema_version=2, scenario=region, provenance=dict(kind="synthetic", source="Full-mesh candidate network under the same transport flow model, rating and cost rules as network.json; ablation only"),
                        voltage_kV=kv, flow_model="transport", greenfield_policy="rebuildable", nodes=v1["nodes"], corridors=corridors, interfaces=[], zone_limits={})

def _main():
    ap = argparse.ArgumentParser(); ap.add_argument("--regions", nargs="+", default=list(BUILDERS)); ap.add_argument("--dry", action="store_true"); a = ap.parse_args()
    for region in a.regions:
        folder, net = BUILDERS[region]()
        add_projects_and_cuts(region, net)
        summarize(net)
        print(f"  projects: {[p['id'] for p in net.get('projects', [])]}")
        _, mesh = build_fullmesh(region)
        if a.dry: continue
        legacy = folder / "network_fullmesh_dc_v1.json"
        if not legacy.exists():
            src = folder / "network_fullmesh.json" if (folder / "network_fullmesh.json").exists() else folder / "network.json"
            legacy.write_bytes(src.read_bytes())
        (folder / "network.json").write_text(json.dumps(net, indent=2, allow_nan=False) + "\n")
        (folder / "network_fullmesh.json").write_text(json.dumps(mesh, indent=2, allow_nan=False) + "\n")
        (folder / "raw/topology/ASSUMPTIONS.json").write_text(json.dumps(ASSUMPTIONS, indent=2) + "\n")
        print(f"  wrote network.json ({len(net['corridors'])} corridors, {len(net.get('projects', []))} projects) and network_fullmesh.json ({len(mesh['corridors'])} corridors); legacy DC v1 kept as network_fullmesh_dc_v1.json")

if __name__ == "__main__":
    _main()
