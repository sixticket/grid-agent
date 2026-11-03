"""Zonal transmission planning with two interchangeable flow models.

schema_version 1 (legacy, full-mesh assumption): every corridor is a candidate
circuit with a fixed rating and reactance, flows follow linearized DC physics
(angle constraints), unbuilt circuits are removed electrically.

schema_version 2 (public zonal data): corridors are declared from real
adjacency. `flow_model` is "dc" (as above) or "transport" (capacity and
direction only, no reactance: the form public inter-zonal transfer limits
take). Version 2 also expresses what public sources actually publish:
  - directional ratings per corridor (`reverse_rating_MW`),
  - `interfaces`: a limit on the summed flow across a *set* of corridors
    (NYISO-style interface limits; several paths, one number),
  - `zone_limits`: per-node import/export caps (MISO CIL/CEL style),
  - `greenfield_policy`: "bare" removes existing circuits in greenfield;
    "rebuildable" removes them but lets the same routes be built as candidates.
Neither version validates nonlinear AC, voltage stability, losses or N-1.
"""
import copy
import hashlib
import json
import pathlib
import re
from types import SimpleNamespace

import numpy as np
import pandas as pd

VERSION = "zonal-transmission-v2"
LEGACY_VERSION = "dc-candidate-circuits-v1"
TRANSPORT_WHEELING_COST_USD_PER_MWH = 0.1
IDENT = r"[a-zA-Z0-9_-]+"


def _positive(value, label, zero=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not np.isfinite(value) or value < 0 or (not zero and value == 0)):
        raise ValueError(f"{label} must be {'nonnegative' if zero else 'positive'} and finite")


def _count(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")


def _calendar_year(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 9999:
        raise ValueError(f"{label} must be an integer calendar year")
    return value


def project_eligibility(project, config):
    """Fixed input constraint; an agent's project permission cannot override it."""
    year = config.get("planning_year")
    in_service = project.get("in_service_year")
    if in_service is not None:
        _calendar_year(in_service, "project in_service_year")
    if "planning_year" not in config:
        return True, "Legacy scenario without a planning calendar"
    _calendar_year(year, "planning_year")
    if in_service is None:
        return False, "In-service year is unknown; unavailable under the fixed planning calendar"
    if in_service > year:
        return False, f"In-service year {in_service} is after planning year {year}"
    return True, f"In-service year {in_service} is no later than planning year {year}"


def load_network(source, scenario_name, scenario, snapshots):
    planning_year = scenario.get("planning_year", scenario.get("year"))
    if "planning_year" in scenario or "year" in scenario:
        _calendar_year(planning_year, "planning_year")
    if isinstance(source, (str, pathlib.Path)):
        path = pathlib.Path(source).resolve()
        raw = path.read_bytes()
        data = json.loads(raw)
        folder = path.parent
    else:
        data = copy.deepcopy(source)
        raw = json.dumps(data, sort_keys=True, allow_nan=False).encode()
        folder = pathlib.Path.cwd()
    if not isinstance(data, dict) or data.get("schema_version") not in (1, 2) or isinstance(data.get("schema_version"), bool):
        raise ValueError("network schema_version must be 1 or 2")
    version = data["schema_version"]
    if version == 1:
        required = {"schema_version", "scenario", "provenance", "voltage_kV", "angle_bound_rad", "nodes", "corridors"}
        optional = set()
        flow_model, policy = "dc", "bare"
    else:
        required = {"schema_version", "scenario", "provenance", "voltage_kV", "flow_model", "greenfield_policy", "nodes", "corridors"}
        optional = {"angle_bound_rad", "interfaces", "zone_limits", "projects"}
        flow_model, policy = data["flow_model"], data["greenfield_policy"]
        if flow_model not in ("dc", "transport"):
            raise ValueError("flow_model must be dc or transport")
        if policy not in ("bare", "rebuildable"):
            raise ValueError("greenfield_policy must be bare or rebuildable")
        if flow_model == "dc" and "angle_bound_rad" not in data:
            raise ValueError("dc flow model needs angle_bound_rad")
    if not required <= set(data) or set(data) - required - optional:
        raise ValueError(f"network requires {sorted(required)} (optional {sorted(optional)})")
    if data["scenario"] != scenario_name:
        raise ValueError("network scenario mismatch")
    provenance = data["provenance"]
    if (not isinstance(provenance, dict) or provenance.get("kind") not in ("synthetic", "reported", "derived")
            or not isinstance(provenance.get("source"), str) or not provenance["source"].strip()):
        raise ValueError("network provenance needs kind and source")
    _positive(data["voltage_kV"], "voltage_kV")
    angle = data.get("angle_bound_rad")
    if flow_model == "dc":
        _positive(angle, "angle_bound_rad")
        if angle > 0.5:
            raise ValueError("angle_bound_rad must be <= 0.5 for this linearized model")
    if not isinstance(data["nodes"], list) or len(data["nodes"]) < 2:
        raise ValueError("transmission planning requires at least two nodes")
    nodes = {}
    file_hashes = {}
    for node in data["nodes"]:
        fields = {"id", "demand_share", "new_capacity_limits_MW"}
        optional_node = {"solar_cf_multiplier", "wind_cf_multiplier", "profiles_csv"}
        if not isinstance(node, dict) or not fields <= set(node) or set(node) - fields - optional_node:
            raise ValueError("node requires id, demand_share, new_capacity_limits_MW; optional profiles_csv and CF multipliers")
        name = node["id"]
        if not isinstance(name, str) or not re.fullmatch(IDENT, name) or name in nodes:
            raise ValueError("node IDs must be unique alphanumeric/underscore/hyphen strings")
        _positive(node["demand_share"], "demand_share", zero=True)
        limits = node["new_capacity_limits_MW"]
        if not isinstance(limits, dict) or set(limits) != {"wind", "solar", "nuclear", "battery", "electrolyzer", "fuel_cell", "h2_storage_MWh"}:
            raise ValueError("each node must explicitly bound all seven supported new asset types")
        for k, v in limits.items():
            _positive(v, k, zero=True)
        prepared = dict(node)
        prepared["demand_MW"] = np.asarray(scenario["demand_MW"]) * node["demand_share"]
        for tech in ("solar", "wind"):
            multiplier = node.get(f"{tech}_cf_multiplier", 1.)
            _positive(multiplier, f"{tech}_cf_multiplier", zero=True)
            prepared[f"{tech}_cf"] = np.clip(np.asarray(scenario[f"{tech}_cf"]) * multiplier, 0, 1)
        if "profiles_csv" in node:
            if "solar_cf_multiplier" in node or "wind_cf_multiplier" in node:
                raise ValueError("profiles_csv cannot be combined with CF multipliers")
            path = (folder / node["profiles_csv"]).resolve()
            csv_raw = path.read_bytes()
            frame = pd.read_csv(path)
            times = pd.DatetimeIndex(pd.to_datetime(frame.time))
            if not times.is_unique:
                raise ValueError("duplicate source profile timestamps")
            frame = frame.set_index(times).reindex(snapshots)
            if set(frame.columns) != {"time", "demand_MW", "solar_cf", "wind_cf"}:
                raise ValueError("profiles CSV needs time,demand_MW,solar_cf,wind_cf")
            if not pd.DatetimeIndex(pd.to_datetime(frame.time)).equals(pd.DatetimeIndex(snapshots)):
                raise ValueError("nodal profile timestamps must exactly match scenario snapshots")
            for k in ("demand_MW", "solar_cf", "wind_cf"):
                values = frame[k].to_numpy(dtype=float)
                if not np.isfinite(values).all() or (values < 0).any() or (k.endswith("_cf") and (values > 1).any()):
                    raise ValueError(f"invalid nodal {k}")
                prepared[k] = values
            file_hashes[name] = hashlib.sha256(csv_raw).hexdigest()
        nodes[name] = prepared
    if not np.isclose(sum(n["demand_share"] for n in nodes.values()), 1):
        raise ValueError("node demand shares must sum to one")
    total = sum(n["demand_MW"] for n in nodes.values())
    if not np.allclose(total, scenario["demand_MW"], rtol=1e-6, atol=1e-5):
        raise ValueError("nodal demand must sum to the original demand every hour")
    if not isinstance(data["corridors"], list):
        raise ValueError("corridors must be a list")
    seen = set()
    corridors = []
    for edge in data["corridors"]:
        base = {"id", "bus0", "bus1", "length_km", "circuit_rating_MW", "max_new_circuits", "existing_circuits",
                "new_annual_cost_per_circuit_usd", "existing_annual_fixed_cost_per_circuit_usd"}
        if version == 1:
            required_edge, optional_edge = base | {"reactance_ohm"}, set()
        else:
            required_edge = base | {"source"} | ({"reactance_ohm"} if flow_model == "dc" else set())
            optional_edge = {"reverse_rating_MW", "existing_capacity_MW", "existing_reverse_capacity_MW", "note", "voltage_kV", "planned_projects"} | ({"reactance_ohm"} if flow_model == "transport" else set())
        if not isinstance(edge, dict) or not required_edge <= set(edge) or set(edge) - required_edge - optional_edge:
            raise ValueError(f"corridor requires {sorted(required_edge)} (optional {sorted(optional_edge)})")
        name = edge["id"]
        if not isinstance(name, str) or not re.fullmatch(IDENT, name) or name in seen:
            raise ValueError("corridor IDs must be unique")
        seen.add(name)
        if edge["bus0"] not in nodes or edge["bus1"] not in nodes or edge["bus0"] == edge["bus1"]:
            raise ValueError("corridor endpoints must be distinct known nodes")
        for k in ("length_km", "circuit_rating_MW", "new_annual_cost_per_circuit_usd"):
            _positive(edge[k], k)
        if "reactance_ohm" in edge:
            _positive(edge["reactance_ohm"], "reactance_ohm")
        _positive(edge["existing_annual_fixed_cost_per_circuit_usd"], "existing annual fixed cost", zero=True)
        for k in ("max_new_circuits", "existing_circuits"):
            _count(edge[k], k)
        if version == 2 and (not isinstance(edge["source"], str) or not edge["source"].strip()):
            raise ValueError(f"corridor {name} needs a non-empty source")
        prepared = dict(edge)
        if version == 2:
            prepared["reverse_rating_MW"] = edge.get("reverse_rating_MW", edge["circuit_rating_MW"])
            _positive(prepared["reverse_rating_MW"], "reverse_rating_MW")
            # Published existing capacity (e.g. an interface limit) can differ from circuits x new-circuit rating.
            if "existing_capacity_MW" in edge:
                _positive(edge["existing_capacity_MW"], "existing_capacity_MW")
                if edge["existing_circuits"] < 1:
                    raise ValueError(f"corridor {name}: existing_capacity_MW needs existing_circuits >= 1")
            prepared["existing_reverse_capacity_MW"] = edge.get("existing_reverse_capacity_MW", edge.get("existing_capacity_MW"))
            if prepared["existing_reverse_capacity_MW"] is not None:
                _positive(prepared["existing_reverse_capacity_MW"], "existing_reverse_capacity_MW")
        corridors.append(prepared)
    interfaces = []
    for iface in data.get("interfaces", []) or []:
        req = {"id", "members", "limit_MW", "source"}
        if not isinstance(iface, dict) or not req <= set(iface) or set(iface) - req - {"reverse_limit_MW", "note", "cut"}:
            raise ValueError("interface requires id, members, limit_MW, source (optional reverse_limit_MW, note, cut)")
        if not isinstance(iface["id"], str) or not re.fullmatch(IDENT, iface["id"]):
            raise ValueError("interface IDs must be alphanumeric")
        _positive(iface["limit_MW"], "interface limit_MW")
        if "reverse_limit_MW" in iface:
            _positive(iface["reverse_limit_MW"], "interface reverse_limit_MW")
        if not isinstance(iface["members"], list) or not iface["members"]:
            raise ValueError("interface needs at least one member corridor")
        for member in iface["members"]:
            if (not isinstance(member, dict) or set(member) != {"corridor", "direction"}
                    or member["corridor"] not in seen or member["direction"] not in (1, -1)):
                raise ValueError("interface member needs a known corridor and direction +1 (bus0->bus1) or -1")
        interfaces.append(iface)
    zone_limits = data.get("zone_limits", {}) or {}
    for bus, lim in zone_limits.items():
        if bus not in nodes or not isinstance(lim, dict) or not {"source"} <= set(lim) or set(lim) - {"import_MW", "export_MW", "source", "note"}:
            raise ValueError("zone_limits entries need a known node with import_MW and/or export_MW and a source")
        for k in ("import_MW", "export_MW"):
            if k in lim:
                _positive(lim[k], k, zero=True)
    # Official planned projects: one build decision covering one or more corridors, plus the transfer-limit
    # increments the project provides (published where available, otherwise a declared assumption).
    projects = []
    iface_ids = {i["id"] for i in interfaces}
    for pr in data.get("projects", []) or []:
        req = {"id", "members", "annual_cost_usd", "status", "source"}
        if not isinstance(pr, dict) or not req <= set(pr) or set(pr) - req - {"increments", "note", "in_service_year", "in_service_year_note"}:
            raise ValueError("project requires id, members, annual_cost_usd, status, source (optional increments, note, in_service_year, in_service_year_note)")
        if pr.get("in_service_year") is not None:
            _calendar_year(pr["in_service_year"], "project in_service_year")
        if "in_service_year_note" in pr and (not isinstance(pr["in_service_year_note"], str) or not pr["in_service_year_note"].strip()):
            raise ValueError("project in_service_year_note must be nonempty text")
        if "in_service_year" in pr and pr["in_service_year"] is None and not pr.get("in_service_year_note"):
            raise ValueError("An explicitly unknown in_service_year needs an in_service_year_note")
        if not isinstance(pr["id"], str) or not re.fullmatch(IDENT, pr["id"]) or any(q["id"] == pr["id"] for q in projects):
            raise ValueError("project IDs must be unique alphanumeric")
        _positive(pr["annual_cost_usd"], "project annual_cost_usd", zero=True)
        if flow_model != "transport":
            raise ValueError("projects are only supported with the transport flow model")
        if not isinstance(pr["members"], list) or not pr["members"]:
            raise ValueError("project needs at least one member corridor")
        for mem in pr["members"]:
            if not isinstance(mem, dict) or not {"corridor", "capacity_MW"} <= set(mem) or set(mem) - {"corridor", "capacity_MW", "reverse_capacity_MW"} or mem["corridor"] not in seen:
                raise ValueError("project member needs a known corridor and capacity_MW (optional reverse_capacity_MW)")
            _positive(mem["capacity_MW"], "member capacity_MW")
            if "reverse_capacity_MW" in mem:
                _positive(mem["reverse_capacity_MW"], "member reverse_capacity_MW", zero=True)
        inc = pr.get("increments", {}) or {}
        if set(inc) - {"interfaces", "zone_limits"}:
            raise ValueError("project increments may only have interfaces and zone_limits")
        for iid, mw in (inc.get("interfaces", {}) or {}).items():
            if iid not in iface_ids:
                raise ValueError(f"project {pr['id']} increments unknown interface {iid}")
            _positive(mw, "interface increment", zero=True)
        for bus, lim in (inc.get("zone_limits", {}) or {}).items():
            if bus not in nodes or set(lim) - {"import_MW", "export_MW"}:
                raise ValueError(f"project {pr['id']} zone_limits increment needs a known node with import_MW/export_MW")
            for k, mw in lim.items():
                _positive(mw, "zone limit increment", zero=True)
        projects.append(pr)
    # Version 1 returns exactly what it always did, so legacy scenario hashes are unchanged.
    result = dict(version=LEGACY_VERSION if version == 1 else VERSION, source_sha256=hashlib.sha256(raw).hexdigest(),
                  provenance=provenance, voltage_kV=data["voltage_kV"],
                  angle_bound_rad=angle, nodes=nodes,
                  corridors=corridors, profile_sha256=file_hashes)
    if version == 2:
        result.update(schema_version=2, flow_model=flow_model, greenfield_policy=policy, interfaces=interfaces, zone_limits=zone_limits, projects=projects)
    if planning_year is not None:
        result["planning_year"] = planning_year
    return result


def _flow_model(config):
    return config.get("flow_model", "dc")


def candidate_cap(edge, config, mode):
    """Candidate circuits allowed on a corridor. In greenfield under the
    rebuildable policy, removed existing routes may be rebuilt."""
    cap = edge["max_new_circuits"]
    if mode == "greenfield" and config.get("greenfield_policy", "bare") == "rebuildable":
        cap = max(cap, edge["existing_circuits"])
    return cap


def design_variables(config, mode="brownfield"):
    space, defaults = {}, {}
    for edge in config["corridors"]:
        key = f"tx_{edge['id']}_max_new_circuits"
        cap = candidate_cap(edge, config, mode)
        space[key] = dict(type="int", min=0, max=cap,
                          doc="maximum NEW assumed-expansion circuits allowed on this corridor (generic additional circuit along the existing route; an assumption, not an official project); solver chooses how many to build; existing circuits remain")
        defaults[key] = cap
    for pr in config.get("projects", []):
        key = f"project_{pr['id']}_allowed"
        eligible, reason = project_eligibility(pr, config)
        space[key] = dict(type="bool", doc=f"permit the official planned project {pr['id']} ({pr['status']}) as a build option; the solver decides whether to build it; members {[m['corridor'] for m in pr['members']]}; increments {pr.get('increments', {})}; fixed calendar eligibility: {eligible} ({reason}); this permission cannot override calendar eligibility")
        defaults[key] = eligible
    return space, defaults


def add_grid(n, config, context, design):
    """DC mode: one bidirectional Link per circuit. Transport mode: two unidirectional Links per circuit
    (forward bus0->bus1 and reverse), each with a negligible wheeling cost that removes free circulating
    loop flows (DC angle physics would otherwise prevent them). Reverse links carry no capital cost; a
    candidate's reverse capacity is tied to the same build decision."""
    metadata = {}
    transport = _flow_model(config) == "transport"
    wheeling = TRANSPORT_WHEELING_COST_USD_PER_MWH if transport else 0.0
    for edge in config["corridors"]:
        existing = edge["existing_circuits"] if context["mode"] == "brownfield" else 0
        allowed = design.get(f"tx_{edge['id']}_max_new_circuits", candidate_cap(edge, config, context["mode"]))
        rating = edge["circuit_rating_MW"]
        reverse = edge.get("reverse_rating_MW", rating)
        lumped = existing > 0 and edge.get("existing_capacity_MW") is not None
        for kind, count in (("existing", 1 if lumped else existing), ("new", allowed)):
            for i in range(count):
                name = f"tx::{edge['id']}::{kind}::{i}"
                meta = dict(edge, existing=kind == "existing", index=i)
                if kind == "existing":
                    fwd_cap = edge["existing_capacity_MW"] if lumped else rating
                    rev_cap = edge["existing_reverse_capacity_MW"] if lumped else reverse
                else:
                    fwd_cap, rev_cap = rating, reverse
                if not transport:
                    n.add("Link", name, bus0=edge["bus0"], bus1=edge["bus1"],
                          p_nom=fwd_cap if kind == "existing" else 0, p_nom_extendable=kind == "new", p_nom_max=fwd_cap,
                          p_min_pu=-1, efficiency=1,
                          capital_cost=edge["new_annual_cost_per_circuit_usd"] / rating if kind == "new" else 0)
                    metadata[name] = dict(meta, direction=0)
                    continue
                n.add("Link", name, bus0=edge["bus0"], bus1=edge["bus1"],
                      p_nom=fwd_cap if kind == "existing" else 0, p_nom_extendable=kind == "new", p_nom_max=fwd_cap,
                      p_min_pu=0, efficiency=1, marginal_cost=wheeling,
                      capital_cost=edge["new_annual_cost_per_circuit_usd"] / rating if kind == "new" else 0)
                metadata[name] = dict(meta, direction=1, reverse_name=name + "::rev", reverse_capacity_MW=rev_cap, kind="existing" if kind == "existing" else "assumed_expansion")
                n.add("Link", name + "::rev", bus0=edge["bus1"], bus1=edge["bus0"],
                      p_nom=rev_cap if kind == "existing" else 0, p_nom_extendable=kind == "new", p_nom_max=rev_cap,
                      p_min_pu=0, efficiency=1, marginal_cost=wheeling, capital_cost=0)
                metadata[name + "::rev"] = dict(meta, direction=-1, forward_name=name)
    for pr in config.get("projects", []):
        if not project_eligibility(pr, config)[0] or not design.get(f"project_{pr['id']}_allowed", True):
            continue
        for k, mem in enumerate(pr["members"]):
            edge = next(e for e in config["corridors"] if e["id"] == mem["corridor"])
            cap = mem["capacity_MW"]; rev = mem.get("reverse_capacity_MW", cap)
            name = f"tx::{edge['id']}::project::{pr['id']}"
            # whole project cost charged on the first member's forward leg; all legs tied to one build decision
            n.add("Link", name, bus0=edge["bus0"], bus1=edge["bus1"], p_nom=0, p_nom_extendable=True, p_nom_max=cap,
                  p_min_pu=0, efficiency=1, marginal_cost=wheeling, capital_cost=(pr["annual_cost_usd"] / cap) if k == 0 else 0)
            metadata[name] = dict(edge, existing=False, index=0, direction=1, kind="official_project", project=pr["id"],
                                  project_capacity_MW=cap, reverse_name=name + "::rev", reverse_capacity_MW=rev)
            if rev > 0:
                n.add("Link", name + "::rev", bus0=edge["bus1"], bus1=edge["bus0"], p_nom=0, p_nom_extendable=True, p_nom_max=rev,
                      p_min_pu=0, efficiency=1, marginal_cost=wheeling, capital_cost=0)
                metadata[name + "::rev"] = dict(edge, existing=False, index=0, direction=-1, kind="official_project", project=pr["id"], forward_name=name)
    n.meta["transmission_circuits"] = metadata
    n.meta["transmission_config"] = config


def add_spatial_system(n, scenario, design, context, add_existing):
    config, costs = scenario["transmission"], scenario["costs"]
    n.meta["design"] = design
    for bus, node in config["nodes"].items():
        n.add("Bus", bus, v_nom=config["voltage_kV"])
        n.add("Load", f"demand::{bus}", bus=bus, p_set=pd.Series(node["demand_MW"], n.snapshots))
        n.add("Generator", f"load_shedding::{bus}", bus=bus, p_nom=float(np.max(node["demand_MW"])), marginal_cost=scenario["voll"])
        limits = dict(node["new_capacity_limits_MW"])
        for tech in ("wind", "solar", "nuclear", "battery"):
            limits[tech] = min(limits[tech], design.get(f"site_{bus}_{tech}_max_MW", limits[tech]))
        for tech in ("wind", "solar", "nuclear"):
            if not design[tech]:
                continue
            attrs = dict(p_min_pu=costs[tech].get("p_min_pu", 0))
            if tech in ("wind", "solar"):
                attrs["p_max_pu"] = pd.Series(node[f"{tech}_cf"], n.snapshots)
            n.add("Generator", f"{tech}::{bus}", bus=bus, p_nom_extendable=True,
                  p_nom_max=min(limits[tech], design[f"{tech}_max_GW"] * 1000),
                  capital_cost=costs[tech]["capital"], marginal_cost=costs[tech]["marginal"], **attrs)
        if design["battery"]:
            n.add("StorageUnit", f"battery::{bus}", bus=bus, p_nom_extendable=True,
                  p_nom_max=limits["battery"], max_hours=design["battery_hours"],
                  capital_cost=costs["battery"]["capital_power"] + design["battery_hours"] * costs["battery"]["capital_energy"],
                  efficiency_store=.95, efficiency_dispatch=.95)
        if design["hydrogen"]:
            h2 = f"h2::{bus}"
            n.add("Bus", h2)
            n.add("Store", f"h2_storage::{bus}", bus=h2, e_nom_extendable=True,
                  e_nom_max=limits["h2_storage_MWh"], capital_cost=costs["h2_storage"]["capital"])
            for tech, b0, b1 in (("electrolyzer", bus, h2), ("fuel_cell", h2, bus)):
                n.add("Link", f"{tech}::{bus}", bus0=b0, bus1=b1, p_nom_extendable=True,
                      p_nom_max=limits[tech], capital_cost=costs[tech]["capital"], efficiency=costs[tech]["eff"])
    add_existing(n, scenario, context)
    add_grid(n, config, context, design)
    n.meta["existing_grid_annual_cost_usd"] = sum(e["existing_circuits"] * e["existing_annual_fixed_cost_per_circuit_usd"] for e in config["corridors"]) if context["mode"] == "brownfield" else 0


def _corridor_flow(m, circuits, corridor_id):
    """Signed flow expression (bus0->bus1 positive) summed over a corridor's circuits, or None."""
    fwd = [name for name, a in circuits.items() if a["id"] == corridor_id and a.get("direction", 0) >= 0]
    rev = [name for name, a in circuits.items() if a["id"] == corridor_id and a.get("direction", 0) < 0]
    if not fwd:
        return None
    expr = m.variables["Link-p"].sel(name=fwd).sum("name")
    if rev:
        expr = expr - m.variables["Link-p"].sel(name=rev).sum("name")
    return expr


def constraints(n, snapshots):
    config = n.meta["transmission_config"]
    m = n.model
    circuits = n.meta["transmission_circuits"]
    candidates = [k for k, v in circuits.items() if not v["existing"] and v.get("direction", 0) >= 0 and v.get("kind") != "official_project"]
    build = m.add_variables(binary=True, coords=[pd.Index(candidates, name="name")], name="tx_build") if candidates else None
    project_ids = sorted({v["project"] for v in circuits.values() if v.get("kind") == "official_project"})
    zproj = m.add_variables(binary=True, coords=[pd.Index(project_ids, name="project")], name="tx_project_build") if project_ids else None
    for name, edge in circuits.items():
        if edge.get("kind") == "official_project" and edge.get("direction", 0) > 0:
            z = zproj.sel(project=edge["project"])
            m.add_constraints(m.variables["Link-p_nom"].sel(name=name) == edge["project_capacity_MW"] * z, name=f"project_capacity::{name}")
            if edge.get("reverse_name") in circuits:
                m.add_constraints(m.variables["Link-p_nom"].sel(name=edge["reverse_name"]) == edge["reverse_capacity_MW"] * z, name=f"project_capacity_rev::{name}")
    projects = {pr["id"]: pr for pr in config.get("projects", [])}
    def assumed_on(corridor_id, direction):
        """Assumed-expansion build variables on a corridor with their forward/reverse rating (ASSUMPTION: a new
        circuit raises the limits it crosses by its rating)."""
        out = []
        for k, v in circuits.items():
            if v["id"] == corridor_id and v.get("kind") == "assumed_expansion" and v.get("direction", 0) > 0:
                out.append((build.sel(name=k), v["circuit_rating_MW"] if direction > 0 else v.get("reverse_capacity_MW", v["circuit_rating_MW"])))
        return out
    dc = _flow_model(config) == "dc"
    if dc:
        theta = m.add_variables(lower=-config["angle_bound_rad"], upper=config["angle_bound_rad"],
                                coords=[snapshots, pd.Index(config["nodes"], name="tx_bus")], name="tx_angle")
        m.add_constraints(theta.sel(tx_bus=next(iter(config["nodes"]))) == 0, name="tx_reference")
    for name, edge in circuits.items():
        if edge.get("direction", 0) < 0 or edge.get("kind") == "official_project":
            continue  # reverse legs and project legs are tied above/below
        p = m.variables["Link-p"].sel(name=name)
        if dc:
            slope = config["voltage_kV"] ** 2 / edge["reactance_ohm"]
            difference = p - slope * (theta.sel(tx_bus=edge["bus0"]) - theta.sel(tx_bus=edge["bus1"]))
        if edge["existing"]:
            if dc:
                m.add_constraints(difference == 0, name=f"dc_flow::{name}")
            continue
        y = build.sel(name=name)
        m.add_constraints(m.variables["Link-p_nom"].sel(name=name) == edge["circuit_rating_MW"] * y, name=f"build_capacity::{name}")
        if "reverse_name" in edge:
            m.add_constraints(m.variables["Link-p_nom"].sel(name=edge["reverse_name"]) == edge["reverse_capacity_MW"] * y, name=f"build_capacity_rev::{name}")
        if dc:
            big_m = 2 * config["angle_bound_rad"] * slope
            m.add_constraints(difference <= big_m * (1 - y), name=f"dc_upper::{name}")
            m.add_constraints(difference >= -big_m * (1 - y), name=f"dc_lower::{name}")
        if edge["index"] > 0:
            previous = f"tx::{edge['id']}::new::{edge['index'] - 1}"
            m.add_constraints(y <= build.sel(name=previous), name=f"circuit_order::{name}")
    # Symmetry breaking (instrument only, feasible networks unchanged): where a single-corridor official project
    # is identical to the corridor's assumed expansion (same capacity, same annual cost), building the expansion
    # alone duplicates building the project alone. Allow the first assumed circuit only after such a project.
    n.meta["symmetry_breaking"] = []
    for edge in config["corridors"]:
        twins = [pr["id"] for pr in projects.values() if pr["id"] in project_ids and len(pr["members"]) == 1 and pr["members"][0]["corridor"] == edge["id"]
                 and abs(pr["members"][0]["capacity_MW"] - edge["circuit_rating_MW"]) < 1e-6 * edge["circuit_rating_MW"] and abs(pr["annual_cost_usd"] - edge["new_annual_cost_per_circuit_usd"]) < 1e-3 * edge["new_annual_cost_per_circuit_usd"]]
        first = [k for k, v in circuits.items() if v["id"] == edge["id"] and v.get("kind") == "assumed_expansion" and v.get("direction", 0) > 0 and v["index"] == 0]
        if twins and first:
            m.add_constraints(build.sel(name=first[0]) <= sum((zproj.sel(project=t) for t in twins[1:]), zproj.sel(project=twins[0])), name=f"symmetry::{edge['id']}")
            n.meta["symmetry_breaking"].append(dict(corridor=edge["id"], assumed_after_projects=twins))
        # identical single-corridor projects on the same corridor are interchangeable: build them in a fixed order
        groups = {}
        for pr in projects.values():
            if pr["id"] in project_ids and len(pr["members"]) == 1 and pr["members"][0]["corridor"] == edge["id"]:
                key = (round(pr["members"][0]["capacity_MW"], 3), round(pr["annual_cost_usd"] / max(pr["annual_cost_usd"], 1.0), 6), round(pr["annual_cost_usd"]))
                groups.setdefault(key, []).append(pr["id"])
        for ids in groups.values():
            for a, b in zip(ids, ids[1:]):
                m.add_constraints(zproj.sel(project=b) <= zproj.sel(project=a), name=f"symmetry_order::{b}")
            if len(ids) > 1:
                n.meta["symmetry_breaking"].append(dict(corridor=edge["id"], ordered_identical_projects=ids))
    # Published limits over sets of paths (interfaces) and per-zone import/export caps.
    for iface in config.get("interfaces", []):
        terms = []
        for member in iface["members"]:
            flow = _corridor_flow(m, circuits, member["corridor"])
            if flow is not None:
                terms.append(member["direction"] * flow)
        if not terms:
            continue
        total = sum(terms[1:], terms[0])
        upper_inc = [(zproj.sel(project=pid), pr["increments"]["interfaces"][iface["id"]]) for pid, pr in projects.items()
                     if pid in project_ids and pr.get("increments", {}).get("interfaces", {}).get(iface["id"])]
        lower_inc = list(upper_inc)
        for member in iface["members"]:
            for y, rating in assumed_on(member["corridor"], member["direction"]):
                upper_inc.append((y, rating))
            for y, rating in assumed_on(member["corridor"], -member["direction"]):
                lower_inc.append((y, rating))
        upper = iface["limit_MW"] + sum((y * mw for y, mw in upper_inc), 0)
        m.add_constraints(total <= upper, name=f"interface_upper::{iface['id']}")
        if "reverse_limit_MW" in iface:
            lower = -iface["reverse_limit_MW"] - sum((y * mw for y, mw in lower_inc), 0)
            m.add_constraints(total >= lower, name=f"interface_lower::{iface['id']}")
    for bus, lim in config.get("zone_limits", {}).items():
        terms = []
        for edge in config["corridors"]:
            flow = _corridor_flow(m, circuits, edge["id"])
            if flow is None:
                continue
            if edge["bus1"] == bus:
                terms.append(flow)
            elif edge["bus0"] == bus:
                terms.append(-flow)
        if not terms:
            continue
        net_import = sum(terms[1:], terms[0])
        imp_inc = [(zproj.sel(project=pid), pr["increments"]["zone_limits"][bus].get("import_MW", 0)) for pid, pr in projects.items()
                   if pid in project_ids and bus in pr.get("increments", {}).get("zone_limits", {})]
        exp_inc = [(zproj.sel(project=pid), pr["increments"]["zone_limits"][bus].get("export_MW", 0)) for pid, pr in projects.items()
                   if pid in project_ids and bus in pr.get("increments", {}).get("zone_limits", {})]
        for edge in config["corridors"]:
            if bus not in (edge["bus0"], edge["bus1"]):
                continue
            into = 1 if edge["bus1"] == bus else -1
            imp_inc += assumed_on(edge["id"], into)
            exp_inc += assumed_on(edge["id"], -into)
        if "import_MW" in lim:
            m.add_constraints(net_import <= lim["import_MW"] + sum((z * mw for z, mw in imp_inc), 0), name=f"zone_import::{bus}")
        if "export_MW" in lim:
            m.add_constraints(net_import >= -lim["export_MW"] - sum((z * mw for z, mw in exp_inc), 0), name=f"zone_export::{bus}")
    # The existing agent caps constrain total new capacity across all sites.
    for tech in ("wind", "solar", "nuclear"):
        names = [name for name in n.generators.index if name.startswith(tech + "::")]
        if names:
            cap = n.meta["design"][f"{tech}_max_GW"] * 1000
            m.add_constraints(m.variables["Generator-p_nom"].sel(name=names).sum() <= cap, name=f"global_new_cap::{tech}")


def project_rows_for(config, circuits, n):
    zsol = n.model.variables["tx_project_build"].solution if "tx_project_build" in n.model.variables else None
    rows = []
    for pr in config.get("projects", []):
        built = bool(zsol is not None and pr["id"] in zsol.coords["project"].values and float(zsol.sel(project=pr["id"])) > 0.5)
        rows.append(dict(project=pr["id"], built=built, increments=pr.get("increments", {})))
    return rows


def report(n, context):
    config = n.meta["transmission_config"]
    circuits = n.meta["transmission_circuits"]
    dc = _flow_model(config) == "dc"
    theta = n.model.variables["tx_angle"].solution if dc else None
    outputs, max_residual = [], 0.
    corridor_flow = {}
    for edge in config["corridors"]:
        names = [name for name, a in circuits.items() if a["id"] == edge["id"] and a.get("direction", 0) >= 0]
        rev_names = [name for name, a in circuits.items() if a["id"] == edge["id"] and a.get("direction", 0) < 0]
        old_names = [name for name in names if circuits[name]["existing"]]
        old = edge["existing_circuits"] if old_names else 0
        old_cap = float(sum(n.links.at[name, "p_nom"] for name in old_names))
        new_names = [name for name in names if not circuits[name]["existing"] and circuits[name].get("kind") != "official_project"]
        proj_names = [name for name in names if circuits[name].get("kind") == "official_project"]
        new = sum(int(n.links.at[name, "p_nom_opt"] > edge["circuit_rating_MW"] / 2) for name in new_names)
        proj_cap = float(sum(n.links.at[name, "p_nom_opt"] for name in proj_names))
        proj_built = [circuits[name]["project"] for name in proj_names if n.links.at[name, "p_nom_opt"] > 1e-3]
        cap = old_cap + new * edge["circuit_rating_MW"] + proj_cap
        flow = n.links_t.p0[names].sum(axis=1) if names else pd.Series(0., index=n.snapshots)
        if rev_names:
            flow = flow - n.links_t.p0[rev_names].sum(axis=1)
        corridor_flow[edge["id"]] = flow
        if dc:
            for name in names:
                if n.links.at[name, "p_nom_opt"] > 1e-6:
                    expected = config["voltage_kV"] ** 2 / edge["reactance_ohm"] * (theta.sel(tx_bus=edge["bus0"]).values - theta.sel(tx_bus=edge["bus1"]).values)
                    max_residual = max(max_residual, float(np.max(np.abs(n.links_t.p0[name].values - expected))))
        peak = float(flow.abs().max())
        idx = int(np.argmax(flow.abs().values))
        old_rev = [circuits[k]["reverse_name"] for k in old_names if "reverse_name" in circuits[k]]
        new_rev = [circuits[k]["reverse_name"] for k in new_names if "reverse_name" in circuits[k]]
        old_peak = float((n.links_t.p0[old_names].sum(axis=1) - (n.links_t.p0[old_rev].sum(axis=1) if old_rev else 0)).abs().max()) if old_names else 0.
        new_peak = float((n.links_t.p0[new_names].sum(axis=1) - (n.links_t.p0[new_rev].sum(axis=1) if new_rev else 0)).abs().max()) if new_names else 0.
        outputs.append(dict(corridor=edge["id"], bus0=edge["bus0"], bus1=edge["bus1"],
                            existing_circuits=old, new_circuits=new,
                            existing_capacity_MW=old_cap,
                            new_capacity_MW=new * edge["circuit_rating_MW"], official_project_capacity_MW=proj_cap, official_projects_built=proj_built,
                            total_capacity_MW=cap,
                            reverse_rating_MW=edge.get("reverse_rating_MW", edge["circuit_rating_MW"]),
                            new_annual_cost_usd=new * edge["new_annual_cost_per_circuit_usd"],
                            existing_annual_fixed_cost_usd=old * edge["existing_annual_fixed_cost_per_circuit_usd"],
                            peak_absolute_flow_MW=peak, peak_utilization=peak / cap if cap else None,
                            existing_peak_absolute_flow_MW=old_peak,
                            new_peak_absolute_flow_MW=new_peak,
                            forward_energy_MWh=float(flow.clip(lower=0).sum()),
                            reverse_energy_MWh=float((-flow.clip(upper=0)).sum()),
                            congestion_hours=int((flow.abs() >= .999 * cap).sum()) if cap else 0,
                            peak_flow_timestamp=str(n.snapshots[idx]),
                            absolute_transferred_energy_MWh=float(flow.abs().sum()),
                            source=edge.get("source"),
                            candidate_kinds_built=(["assumed_expansion"] if new else []) + (["official_project"] if proj_built else []),
                            action="expand_existing" if old and (new or proj_built) else "build_new" if (new or proj_built) else "use_existing" if old else "not_built"))
    interfaces, iface_violation = [], 0.
    for iface in config.get("interfaces", []):
        total = sum(member["direction"] * corridor_flow[member["corridor"]] for member in iface["members"])
        upper, lower = iface["limit_MW"], iface.get("reverse_limit_MW")
        for row in project_rows_for(config, circuits, n):
            if row["built"]:
                upper += row["increments"].get("interfaces", {}).get(iface["id"], 0)
                lower = None if lower is None else lower + row["increments"].get("interfaces", {}).get(iface["id"], 0)
        for member in iface["members"]:
            for name, v in circuits.items():
                if v["id"] == member["corridor"] and v.get("kind") == "assumed_expansion" and v.get("direction", 0) > 0 and n.links.at[name, "p_nom_opt"] > v["circuit_rating_MW"] / 2:
                    fwd_r, rev_r = v["circuit_rating_MW"], v.get("reverse_capacity_MW", v["circuit_rating_MW"])
                    # mirror of constraints(): a built circuit raises the limit in each direction by its rating in that direction
                    upper += fwd_r if member["direction"] > 0 else rev_r
                    if lower is not None:
                        lower += rev_r if member["direction"] > 0 else fwd_r
        viol = max(float((total - upper).max()), float((-lower - total).max()) if lower is not None else 0., 0.)
        iface_violation = max(iface_violation, viol)
        interfaces.append(dict(interface=iface["id"], base_limit_MW=iface["limit_MW"], effective_limit_MW=upper, limit_MW=upper, reverse_limit_MW=lower,
                               peak_flow_MW=float(total.max()), peak_reverse_flow_MW=float(-total.min()),
                               binding_hours=int((total >= .999 * upper).sum() + ((total <= -.999 * lower).sum() if lower is not None else 0)),
                               source=iface["source"]))
    zones, zone_violation = [], 0.
    for bus, lim in config.get("zone_limits", {}).items():
        net = pd.Series(0., index=n.snapshots)
        for e in config["corridors"]:
            if e["bus1"] == bus:
                net = net + corridor_flow[e["id"]]
            elif e["bus0"] == bus:
                net = net - corridor_flow[e["id"]]
        imp = lim.get("import_MW"); exp = lim.get("export_MW")
        for row in project_rows_for(config, circuits, n):
            if row["built"] and bus in row["increments"].get("zone_limits", {}):
                imp = None if imp is None else imp + row["increments"]["zone_limits"][bus].get("import_MW", 0)
                exp = None if exp is None else exp + row["increments"]["zone_limits"][bus].get("export_MW", 0)
        for name, v in circuits.items():
            if v.get("kind") == "assumed_expansion" and v.get("direction", 0) > 0 and bus in (v["bus0"], v["bus1"]) and n.links.at[name, "p_nom_opt"] > v["circuit_rating_MW"] / 2:
                fwd_r, rev_r = v["circuit_rating_MW"], v.get("reverse_capacity_MW", v["circuit_rating_MW"])
                into = 1 if v["bus1"] == bus else -1
                imp = None if imp is None else imp + (fwd_r if into > 0 else rev_r)
                exp = None if exp is None else exp + (rev_r if into > 0 else fwd_r)
        viol = max(float((net - imp).max()) if imp is not None else 0., float((-exp - net).max()) if exp is not None else 0., 0.)
        zone_violation = max(zone_violation, viol)
        zones.append(dict(node=bus, base_import_limit_MW=lim.get("import_MW"), base_export_limit_MW=lim.get("export_MW"), import_limit_MW=imp, export_limit_MW=exp,
                          peak_import_MW=float(net.max()), peak_export_MW=float(-net.min()), source=lim["source"]))
    project_rows = []
    zsol = n.model.variables["tx_project_build"].solution if "tx_project_build" in n.model.variables else None
    for pr in config.get("projects", []):
        built = bool(zsol is not None and pr["id"] in zsol.coords["project"].values and float(zsol.sel(project=pr["id"])) > 0.5)
        allowed = any(v.get("project") == pr["id"] for v in circuits.values())
        eligible, reason = project_eligibility(pr, config)
        project_rows.append(dict(project=pr["id"], kind="official_project", status=pr["status"], allowed_by_design=allowed, built=built,
                                 in_service_year=pr.get("in_service_year"), in_service_year_note=pr.get("in_service_year_note"),
                                 eligible_by_year=eligible, eligibility_reason=reason,
                                 members=[m["corridor"] for m in pr["members"]], increments=pr.get("increments", {}),
                                 annual_cost_usd=pr["annual_cost_usd"] if built else 0.0, source=pr["source"]))
    from planning import solver_bounds
    gap = solver_bounds(n)[1]
    return dict(model=config.get("version", VERSION), schema_version=config.get("schema_version", 1), flow_model=_flow_model(config),
                planning_year=config.get("planning_year"),
                formulation=("lossless linearized AC (DC power flow); discrete candidate circuits" if dc
                             else "zonal transport model (capacity and direction only, no reactance); discrete candidate circuits; published interface and zone limits"),
                hours_modeled=len(n.snapshots),
                mip_relative_gap=gap if np.isfinite(gap) else None,
                maximum_dc_flow_residual_MW=max_residual if dc else None,
                max_interface_violation_MW=iface_violation, max_zone_limit_violation_MW=zone_violation,
                nonlinear_ac_validated=False, n_minus_one_validated=False,
                corridors=outputs, interfaces=interfaces, zone_limits=zones, projects=project_rows,
                symmetry_breaking=n.meta.get("symmetry_breaking", []),
                candidate_kinds=dict(assumed_expansion="generic extra circuit along an existing route, rating and cost are assumptions", official_project="planned project from an official source with published (or declared) transfer-limit increments"),
                new_annual_cost_billion=(sum(r["new_annual_cost_usd"] for r in outputs) + sum(r["annual_cost_usd"] for r in project_rows)) / 1e9,
                existing_annual_fixed_cost_billion=sum(r["existing_annual_fixed_cost_usd"] for r in outputs) / 1e9,
                source_sha256=config["source_sha256"], provenance=config["provenance"])


def distribution_check(n, config, distribution, design):
    """Keep each node's PV/battery local when checking representative feeders."""
    from distribution import feeder_sweep
    from tqdm import tqdm
    results, all_hours = {}, []
    for name, node in tqdm(config["nodes"].items(), desc="distribution check (nodes)", unit="node", leave=False):
        demand = node["demand_MW"]
        if np.max(demand) <= 0:
            continue  # No load-based feeder mapping is defined at generation-only nodes.
        proxy = SimpleNamespace(snapshots=n.snapshots, meta=n.meta,
                                generators=n.generators[n.generators.bus == name], generators_t=n.generators_t,
                                storage_units=n.storage_units[n.storage_units.bus == name], storage_units_t=n.storage_units_t)
        feeders = int(np.ceil(np.max(demand) * 1000 / distribution["feeder_peak_kW"]))
        cost = feeders * distribution["line_upgrade_cost_per_feeder_yr"] / 1e9
        local_design = dict(design)
        for key in ("dist_battery_b1_fraction", "dist_line_upgrade", "dist_volt_var"):
            local = design.get(f"{key}_{name}", design[key])
            local_design[key] = (local or design[key]) if key != "dist_battery_b1_fraction" else (design[key] if local == 0.4 else local)
        value = feeder_sweep(proxy, demand, distribution, local_design, cost, include_hourly=True, label=name)
        all_hours.append(value.pop("hourly"))
        results[name] = value
    over, under, failed = set(), set(), set()
    for hours in all_hours:
        for h in hours:
            if not h["converged"]:
                failed.add(h["hour"])
            elif h["overvoltage"]:
                over.add(h["hour"])
            if h.get("undervoltage"):
                under.add(h["hour"])
    return dict(passes=bool(results) and all(r["passes"] for r in results.values()),
                overvoltage_hours=len(over), undervoltage_hours=len(under),
                voltage_violation_hours=len(over | under), nonconverged_hours=len(failed),
                violation_pct=100 * len(over | under) / len(n.snapshots),
                worst_low_pu=min((r["worst_low_pu"] for r in results.values() if r["worst_low_pu"] is not None), default=None),
                worst_high_pu=max((r["worst_high_pu"] for r in results.values() if r["worst_high_pu"] is not None), default=None),
                distribution_capex_billion=sum(r["distribution_capex_billion"] for r in results.values()),
                thermal_check_complete=bool(results) and all(r["thermal_check_complete"] for r in results.values()),
                thermal_overload_hours=sum(r["thermal_overload_hours"] or 0 for r in results.values()) if any(r["thermal_checked_lines"] for r in results.values()) else None,
                nodes=results, skipped_zero_demand_nodes=[k for k, v in config["nodes"].items() if np.max(v["demand_MW"]) <= 0],
                note="Per-node representative distribution checks; voltage counts are unique hours across nodes. Thermal overload counts are node-hours. This is not AC transmission validation; generation-only nodes have no load-based feeder check.")
