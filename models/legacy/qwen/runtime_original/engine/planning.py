"""Greenfield/brownfield generation, storage and transmission planning.

Spatial candidate-circuit planning and an explicit single-node ablation share
the same retained/new-asset accounting.

Brownfield is fixed-retention, one planning year: no retirement, refurbishment
or sunk construction charges. Existing and candidate assets are separate
PyPSA components, including batteries with different durations and locations.
"""
import copy
import hashlib
import json
import pathlib
import re
import time
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pypsa

from distribution import feeder_sweep

PLANNING_VERSION = "experiment-regional-v1"
TECHNOLOGIES = {"wind", "solar", "nuclear", "battery", "electrolyzer", "h2_storage", "fuel_cell", "coal", "gas", "oil", "hydro", "biomass", "waste", "geothermal", "csp", "other", "pumped_storage", "other_storage"}


def _number(value, label, *, positive=False, maximum=None):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not np.isfinite(value) or value < 0
            or (positive and value == 0) or (maximum is not None and value > maximum)):
        raise ValueError(f"invalid {label}: {value!r}")
    return value


def planning_context(mode="greenfield", inventory=None, scenario_name="base"):
    if mode not in ("greenfield", "brownfield"):
        raise ValueError("planning mode must be greenfield or brownfield")
    context = dict(mode=mode, version=PLANNING_VERSION, existing_assets=[],
                   inventory_provenance=None, inventory_sha256=None,
                   fixed_existing_annual_cost_usd=0.0)
    if mode == "greenfield":
        if inventory is not None:
            raise ValueError("--existing-assets is only valid in brownfield mode")
        return context
    if inventory is None:
        raise ValueError("brownfield requires --existing-assets JSON; no existing fleet is assumed")
    if isinstance(inventory, (str, pathlib.Path)):
        raw = pathlib.Path(inventory).read_bytes()
        data = json.loads(raw)
    else:
        data = copy.deepcopy(inventory)
        raw = json.dumps(data, sort_keys=True, allow_nan=False).encode()
    if not isinstance(data, dict):
        raise ValueError("inventory must be a JSON object")
    if set(data) != {"schema_version", "scenario", "provenance", "assets"}:
        raise ValueError("inventory requires exactly schema_version, scenario, provenance, assets")
    if data["schema_version"] != 1 or isinstance(data["schema_version"], bool):
        raise ValueError("unsupported inventory schema_version")
    if data["scenario"] != scenario_name:
        raise ValueError(f"inventory scenario {data['scenario']!r} does not match {scenario_name!r}")
    provenance = data["provenance"]
    if (not isinstance(provenance, dict)
            or provenance.get("kind") not in ("synthetic", "reported", "derived")
            or not isinstance(provenance.get("source"), str) or not provenance["source"].strip()
            or not isinstance(provenance.get("year"), int) or isinstance(provenance["year"], bool)):
        raise ValueError("provenance requires kind (synthetic/reported/derived), source and integer year")
    if not isinstance(data["assets"], list):
        raise ValueError("brownfield assets must be a list; it may be empty for an existing-grid-only case")
    seen = set()
    for a in data["assets"]:
        if not isinstance(a, dict):
            raise ValueError("each asset must be an object")
        ident, tech = a.get("id"), a.get("technology")
        if not isinstance(ident, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", ident) or ident in seen:
            raise ValueError("asset IDs must be unique alphanumeric/underscore/hyphen strings")
        seen.add(ident)
        if tech not in TECHNOLOGIES:
            raise ValueError(f"unsupported existing technology: {tech!r}; no asset is silently omitted")
        required = {"id", "technology", "annual_fixed_cost_usd"}
        required.add("energy_MWh" if tech == "h2_storage" else "capacity_MW")
        optional = {"bus", "availability", "annual_capacity_factor", "emissions_t_per_MWh"}
        if "bus" in a and (not isinstance(a["bus"], str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", a["bus"])):
            raise ValueError("asset bus must be a node ID")
        if tech not in ("battery", "pumped_storage", "other_storage", "electrolyzer", "fuel_cell", "h2_storage"):
            optional.add("marginal_cost_usd_per_MWh")
        if tech == "nuclear":
            optional.add("p_min_pu")
        if tech in ("battery", "pumped_storage", "other_storage"):
            required |= {"energy_MWh", "b1_fraction"}
            optional |= {"efficiency_store", "efficiency_dispatch"}
        if tech in ("electrolyzer", "fuel_cell"):
            optional.add("efficiency")
        if not required <= set(a) or set(a) - required - optional:
            raise ValueError(f"{ident}: required fields {sorted(required)}; optional {sorted(optional)}")
        for key in ("capacity_MW", "energy_MWh"):
            if key in a:
                _number(a[key], f"{ident}.{key}", positive=True)
        for key in ("annual_fixed_cost_usd", "marginal_cost_usd_per_MWh", "emissions_t_per_MWh"):
            if key in a:
                _number(a[key], f"{ident}.{key}")
        for key in ("b1_fraction", "p_min_pu", "availability", "annual_capacity_factor"):
            if key in a:
                _number(a[key], f"{ident}.{key}", maximum=1)
        for key in ("efficiency", "efficiency_store", "efficiency_dispatch"):
            if key in a:
                _number(a[key], f"{ident}.{key}", maximum=1, positive=True)
    context.update(existing_assets=data["assets"], inventory_provenance=provenance,
                   inventory_sha256=hashlib.sha256(raw).hexdigest(),
                   fixed_existing_annual_cost_usd=sum(a["annual_fixed_cost_usd"] for a in data["assets"]))
    return context


def _add_existing(n, scenario, context):
    c = scenario["costs"]
    n.meta["planning_assets"] = {}
    for a in context["existing_assets"]:
        tech, name = a["technology"], "existing__" + a["id"]
        bus = a.get("bus", "grid")
        h2_bus = f"h2::{bus}" if "transmission" in scenario else "h2"
        profiles = scenario.get("transmission", {}).get("nodes", {}).get(bus, scenario)
        n.meta["planning_assets"][name] = a
        if tech not in ("battery", "pumped_storage", "other_storage", "electrolyzer", "fuel_cell", "h2_storage"):
            kwargs = dict(p_min_pu=a.get("p_min_pu", 0), p_max_pu=a.get("availability", 1.0))
            if tech in ("wind", "solar"):
                kwargs["p_max_pu"] = pd.Series(np.asarray(profiles[f"{tech}_cf"]) * a.get("availability", 1.0), n.snapshots)
            n.add("Generator", name, bus=bus, p_nom=a["capacity_MW"],
                  p_nom_extendable=False, capital_cost=0,
                  marginal_cost=a.get("marginal_cost_usd_per_MWh", c.get(tech, {}).get("marginal", 0)), **kwargs)
        elif tech in ("battery", "pumped_storage", "other_storage"):
            n.add("StorageUnit", name, bus=bus, p_nom=a["capacity_MW"],
                  p_nom_extendable=False, capital_cost=0,
                  max_hours=a["energy_MWh"] / a["capacity_MW"],
                  p_max_pu=a.get("availability",1), p_min_pu=-a.get("availability",1),
                  efficiency_store=a.get("efficiency_store", 0.95),
                  efficiency_dispatch=a.get("efficiency_dispatch", 0.95), cyclic_state_of_charge=True)
        else:
            if h2_bus not in n.buses.index:
                n.add("Bus", h2_bus)
            if tech == "h2_storage":
                n.add("Store", name, bus=h2_bus, e_nom=a["energy_MWh"],
                      e_nom_extendable=False, capital_cost=0, e_cyclic=True)
            else:
                b0, b1 = (bus, h2_bus) if tech == "electrolyzer" else (h2_bus, bus)
                n.add("Link", name, bus0=b0, bus1=b1, p_nom=a["capacity_MW"],
                      p_nom_extendable=False, capital_cost=0,
                      efficiency=a.get("efficiency", c[tech]["eff"]))


SAFETY_CEILING_SECONDS = 86400.0  # not a budget: a zombie guard. Solves run to tolerance.


def choose_solver():
    """Gurobi (parallel barrier root) when installed and licensed, else HiGHS.
    GRID_SOLVER=highs|gurobi forces one. Threads via GRID_SOLVER_THREADS."""
    import os
    want = os.environ.get("GRID_SOLVER", "auto")
    threads = int(os.environ.get("GRID_SOLVER_THREADS", "18"))  # one solve at a time on this 18-core machine
    if want in ("auto", "gurobi"):
        try:
            import gurobipy
            env = gurobipy.Env(params={"OutputFlag": 0}); env.dispose()
            return "gurobi", ".".join(map(str, gurobipy.gurobi.version())), threads
        except Exception as exc:
            if want == "gurobi":
                raise RuntimeError(f"GRID_SOLVER=gurobi but Gurobi is unavailable: {exc}")
    import highspy
    return "highs", highspy.Highs().version(), threads


def solver_bounds(n):
    """(dual bound, relative gap) from the solver model, solver-neutral.
    Pure LPs (no candidate circuits) have no bound gap: dual == objective."""
    sm = getattr(n.model, "solver_model", None)
    try:
        if sm is None:
            raise ValueError("no solver model")
        if hasattr(sm, "getInfo"):  # HiGHS
            info = sm.getInfo()
            dual, gap = float(info.mip_dual_bound), float(info.mip_gap)
        else:  # Gurobi
            if sm.IsMIP:
                dual, gap = float(sm.ObjBound), float(sm.MIPGap)
            else:
                dual, gap = float(sm.ObjVal), 0.0
    except Exception:
        dual, gap = float("nan"), float("nan")
    if not np.isfinite(dual):
        dual, gap = float(n.objective), 0.0
    return dual, gap


def _report(n, scenario, context, solve_seconds):
    shed_names = [g for g in n.generators.index if g.split("::")[0] == "load_shedding"]
    shed = n.generators_t.p[shed_names].sum(axis=1)
    fixed = context["fixed_existing_annual_cost_usd"] + n.meta.get("existing_grid_annual_cost_usd", 0)
    # Solver evidence: the incumbent is an achievable design cost; the dual
    # bound is what the solver has proven no design in this space can beat.
    dual, gap = solver_bounds(n)
    absolute_gap = max(n.objective - dual, 0.0)
    lower_bound = n.objective - absolute_gap
    res = dict(status="optimal", planning_mode=context["mode"],
               planning_version=PLANNING_VERSION, solve_seconds=solve_seconds,
               solver=n.meta.get("solver"),
               inventory_provenance=context["inventory_provenance"],
               inventory_sha256=context["inventory_sha256"],
               total_cost_billion=round((n.objective + fixed) / 1e9, 9),
               cost_lower_bound_billion=round((lower_bound + fixed) / 1e9, 9),
               mip_gap=gap if np.isfinite(gap) else None,
               cost_evidence="total_cost_billion is the best design cost found (achievable, incumbent). "
                             "cost_lower_bound_billion is the solver's proven lower bound for this design space. "
                             "The true optimum lies in between; a wide interval means comparisons between designs are uncertain, not that the design is bad.",
               solver_objective_billion=round(n.objective / 1e9, 9),
               existing_fixed_cost_billion=round(fixed / 1e9, 9),
               cost_excl_shedding_billion=round((n.objective + fixed - shed.sum() * scenario.get("objective_weight", 1.0) * scenario["voll"]) / 1e9, 9),
               unmet_demand_GWh=float(shed.sum() / 1e3),
               unmet_hours=int((shed > 1).sum()),
               capacities_GW={}, existing_capacities_GW={}, new_capacities_GW={},
               annualized_capex_billion={}, curtailment_pct={}, assets=[])
    metadata = n.meta["planning_assets"]
    def accumulate(target, key, value):
        target[key] = target.get(key, 0.0) + value
    for table, nominal in ((n.generators, "p_nom"), (n.storage_units, "p_nom"),
                           (n.links, "p_nom"), (n.stores, "e_nom")):
        for name, row in table.iterrows():
            if name.split("::")[0] in ("load_shedding", "tx"):
                continue
            asset = metadata.get(name)
            tech = asset["technology"] if asset else name.split("::")[0]
            cap = float(row[nominal + "_opt"])
            key = tech + "_GWh" if nominal == "e_nom" else tech
            split = "existing_capacities_GW" if asset else "new_capacities_GW"
            accumulate(res["capacities_GW"], key, cap / 1000)
            accumulate(res[split], key, cap / 1000)
            annual = cap * row["capital_cost"]
            accumulate(res["annualized_capex_billion"], tech, annual / 1e9)
            record = dict(name=name, technology=tech, existing=asset is not None,
                          bus=row.get("bus", row.get("bus0")),
                          capacity_MW=cap if nominal == "p_nom" else None,
                          energy_MWh=cap if nominal == "e_nom" else None,
                          new_annualized_capex_usd=annual)
            if tech in ("battery", "pumped_storage", "other_storage"):
                energy = cap * row["max_hours"]
                for target in (res["capacities_GW"], res[split]):
                    accumulate(target, tech + "_energy_GWh", energy / 1000)
                record["energy_MWh"] = energy
                record["b1_fraction"] = asset["b1_fraction"] if asset else None
            res["assets"].append(record)
    for tech in ("wind", "solar"):
        names = [g for g in n.generators.index if g.split("::")[0] == tech or metadata.get(g, {}).get("technology") == tech]
        if names:
            available = sum(n.generators.at[g, "p_nom_opt"] * n.generators_t.p_max_pu[g].values for g in names)
            used = n.generators_t.p[names].sum(axis=1).to_numpy()
            res["curtailment_pct"][tech] = round(100 * (available - used).sum() / max(available.sum(), 1), 1)
    res["new_annualized_capex_billion"] = sum(res["annualized_capex_billion"].values())
    if "transmission_config" in n.meta:
        from transmission import report
        res["transmission"] = report(n, context)
        res["new_annualized_capex_billion"] += res["transmission"]["new_annual_cost_billion"]
        res["annualized_capex_billion"]["transmission"] = res["transmission"]["new_annual_cost_billion"]
        res["unmet_demand_by_node_GWh"] = {n.generators.at[g, "bus"]: float(n.generators_t.p[g].sum() / 1000) for g in shed_names}
    for key in ("capacities_GW", "existing_capacities_GW", "new_capacities_GW", "annualized_capex_billion"):
        res[key] = {k: round(v, 9 if key == "annualized_capex_billion" else 6) for k, v in res[key].items()}
    from extensions import report_physics
    res.update(report_physics(n, scenario, context))
    res["cost_basis"] = "New annualized capacity costs + all dispatch costs + shedding penalty + supplied existing annual fixed costs. Existing sunk construction costs excluded. Distribution capex separate."
    return json.loads(json.dumps(res, default=float, allow_nan=False))


def with_planning_mode(base, mode="greenfield", inventory=None, network=None):
    """Independent scenario instance; configuring a mode never mutates base."""
    context = planning_context(mode, inventory, base.NAME)
    scenario = copy.deepcopy(base.SCENARIO)
    scenario["planning"] = context
    snapshots = base.SNAPSHOTS.copy()
    if network is not None:
        from transmission import load_network
        scenario["transmission"] = load_network(network, base.NAME, scenario, snapshots)
    nodes = scenario.get("transmission", {}).get("nodes", {"grid": {}})
    for asset in context["existing_assets"]:
        if ("transmission" in scenario and "bus" not in asset) or asset.get("bus", "grid") not in nodes:
            raise ValueError(f"asset {asset['id']} needs a valid bus in the supplied network; assets are never relocated implicitly")
    def serial(value):
        if isinstance(value, np.ndarray):
            return value.tolist()
        raise TypeError(type(value).__name__)
    digest = hashlib.sha256(json.dumps(dict(scenario=scenario, snapshots=list(map(str, snapshots))),
                                      default=serial, sort_keys=True, allow_nan=False).encode()).hexdigest()
    design_space = copy.deepcopy(base.DESIGN_SPACE)
    for tech in ("wind", "solar", "nuclear", "battery", "hydrogen"):
        design_space[tech]["doc"] += "; controls NEW construction only; existing assets are retained in brownfield"
    for tech in ("wind", "solar", "nuclear"):
        design_space[f"{tech}_max_GW"]["doc"] = f"upper bound on NEW {tech} capacity in GW; existing capacity is additional"
    design_space["battery_hours"]["doc"] += "; NEW batteries only"
    design_space["dist_battery_b1_fraction"]["doc"] += "; NEW batteries only; existing location comes from inventory"
    summary = base.SCENARIO_SUMMARY + f" Planning mode: {mode}. "
    if mode == "brownfield":
        summary += ("Existing assets are retained for this planning year; no retirement or relocation. "
                    "Technology switches/caps and battery duration/location apply to NEW assets only. "
                    "Existing sunk construction costs are excluded; supplied annual fixed costs and dispatch costs are included. "
                    "Inventory provenance and retained assets: " + json.dumps({"asset_count": len(context["existing_assets"]), "provenance": context["inventory_provenance"], "capacity_by_type_MW": {t: sum(a.get("capacity_MW", 0) for a in context["existing_assets"] if a["technology"] == t) for t in sorted(TECHNOLOGIES)}}, sort_keys=True))
    else:
        summary += "No existing generation or storage; all selected capacity is new construction. "
    defaults = copy.deepcopy(base.DEFAULT_DESIGN)
    tx_keys = set()
    if "transmission" in scenario:
        from transmission import design_variables
        extra, extra_defaults = design_variables(scenario["transmission"], mode)
        design_space.update(extra)
        defaults.update(extra_defaults)
        tx_keys = set(extra)
        summary = summary.replace("Single-node system", "Spatial multi-node system")
        brief = {k: v for k, v in scenario["transmission"].items() if k != "nodes"}
        brief["nodes"] = [{k: v for k, v in node.items() if k not in ("demand_MW", "solar_cf", "wind_cf")} for node in nodes.values()]
        tx = scenario["transmission"]
        if tx.get("flow_model", "dc") == "dc":
            summary += " Generation, storage siting and candidate AC circuits are jointly optimized with lossless linearized DC power flow."
        else:
            summary += " Generation, storage siting and candidate inter-zonal circuits are jointly optimized with a zonal transport model (published capacities and directions, no reactance); interface limits over sets of corridors and per-zone import/export limits from public sources are enforced."
        summary += (" Greenfield starts with zero circuits" + ("; existing routes may be rebuilt as candidates." if tx.get("greenfield_policy") == "rebuildable" else ".")
                    + " Brownfield retains existing circuits. Candidate count caps control new construction only. Node-specific representative feeders validate local PV/battery dispatch; this does not validate nonlinear AC transmission or N-1 security. Network: " + json.dumps(brief))
    else:
        summary += " Single-node ablation: transmission is not represented."
    def validate(changes):
        errors = base.validate_changes({k: v for k, v in changes.items() if k not in tx_keys})
        for key in tx_keys & changes.keys():
            value, spec = changes[key], design_space[key]
            if spec["type"] == "bool":
                if not isinstance(value, bool):
                    errors.append(f"{key} must be boolean")
            elif isinstance(value, bool) or not isinstance(value, int) or not spec["min"] <= value <= spec["max"]:
                errors.append(f"{key} must be an integer in [{spec['min']}, {spec['max']}]")
        return errors
    cache = {"key": None, "network": None}
    def key(design):
        return json.dumps({k: v for k, v in design.items() if not k.startswith("dist_")}, sort_keys=True)
    def run(design, commit=True):
        """commit=False solves a hypothetical variant without replacing the
        cached solve of the committed design (used by verification calls)."""
        errors = validate(design)
        if errors:
            return {"status": "rejected", "validation_errors": errors}
        result, network = solve_system(design, scenario, snapshots)
        if commit:
            cache.update(key=key(design) if network is not None else None, network=network)
        result["scenario_hash"] = digest[:12]
        return result
    def check(design):
        errors = validate(design)
        if errors:
            return {"error": "invalid design", "validation_errors": errors, "passes": False}
        if cache["key"] != key(design):
            result = run(design)
            if result["status"] not in ("optimal", "feasible_time_limit"):
                return {"error": "system-level solve failed", "system_status": result["status"], "passes": False}
        dist = scenario["distribution"]
        if "transmission" in scenario:
            from transmission import distribution_check
            result = distribution_check(cache["network"], scenario["transmission"], dist, design)
            result.update(planning_mode=mode, scenario_hash=digest[:12])
            return result
        feeders = int(np.ceil(np.max(scenario["demand_MW"]) * 1000 / dist["feeder_peak_kW"]))
        upgrade = round(feeders * dist["line_upgrade_cost_per_feeder_yr"] / 1e9, 3)
        result = feeder_sweep(cache["network"], scenario["demand_MW"], dist, design, upgrade)
        result.update(planning_mode=mode, scenario_hash=digest[:12])
        return result
    def save_dispatch_checkpoint(path):
        if cache['network'] is None:
            raise ValueError('No committed dispatch to checkpoint')
        path = pathlib.Path(path)
        tmp = path.with_suffix('.tmp.nc')
        n = cache['network']
        original_meta = n.meta
        def encode_meta(value):
            if isinstance(value, np.ndarray):
                return {'__checkpoint_ndarray__': value.tolist(), 'dtype': str(value.dtype)}
            if isinstance(value, np.generic): return value.item()
            raise TypeError(type(value).__name__)
        n.meta = json.loads(json.dumps(original_meta, default=encode_meta))
        try:
            n.export_to_netcdf(tmp)
        finally:
            n.meta = original_meta
        tmp.replace(path)
        info = path.with_suffix('.json')
        temp_info = info.with_suffix('.tmp.json')
        temp_info.write_text(json.dumps({'scenario_digest': digest, 'key': cache['key'],
                                        'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}))
        temp_info.replace(info)
    def restore_dispatch_checkpoint(path, design):
        path = pathlib.Path(path)
        info = json.loads(path.with_suffix('.json').read_text())
        if info['scenario_digest'] != digest or info['sha256'] != hashlib.sha256(path.read_bytes()).hexdigest():
            raise ValueError('Dispatch checkpoint does not match scenario or file checksum')
        if info['key'] != key(design):
            raise ValueError('Saved dispatch belongs to another committed design; refusing implicit re-solve')
        import pypsa
        n = pypsa.Network(path)
        def decode_meta(value):
            if '__checkpoint_ndarray__' in value:
                return np.array(value['__checkpoint_ndarray__'], dtype=value['dtype'])
            return value
        n.meta = json.loads(json.dumps(n.meta), object_hook=decode_meta)
        cache.update(key=info['key'], network=n)
    def export_solution(folder):
        if cache['network'] is None:
            raise ValueError('No solved network to export')
        folder = pathlib.Path(folder); folder.mkdir(parents=True, exist_ok=True)
        n = cache['network']
        for label in ['generators', 'storage_units', 'links', 'stores', 'loads']:
            getattr(n, label).to_csv(folder / (label + '.csv'))
            for field in ['p', 'p0', 'p1', 'state_of_charge', 'p_store', 'p_dispatch', 'e']:
                table = getattr(getattr(n, label + '_t'), field, None)
                if table is not None and not table.empty:
                    table.to_csv(folder / (label + '_' + field + '.csv'))
    return SimpleNamespace(NAME=base.NAME, HOURS=base.HOURS, SNAPSHOTS=snapshots,
                           SCENARIO=scenario, SCENARIO_HASH=digest[:12], SCENARIO_DIGEST=digest,
                           SCENARIO_SUMMARY=summary, DESIGN_SPACE=design_space,
                           DEFAULT_DESIGN=defaults, PLANNING=context,
                           validate_changes=validate,
                           save_dispatch_checkpoint=save_dispatch_checkpoint,
                           restore_dispatch_checkpoint=restore_dispatch_checkpoint,
                           run_simulation=run, run_distribution_check=check, export_solution=export_solution)


def add_planning_arguments(parser):
    parser.add_argument("--policy", choices=("baseline", "published"), default="baseline")
    parser.add_argument("--planning-mode", choices=("greenfield", "brownfield"), default="greenfield")
    parser.add_argument("--existing-assets", type=pathlib.Path, help="brownfield inventory JSON with provenance")
    parser.add_argument("--network", type=pathlib.Path, help="nodal demand/resources and existing/candidate transmission circuits JSON")
    parser.add_argument("--single-node", action="store_true", help="explicit ablation without transmission")


def configure_from_arguments(base, args, parser):
    try:
        folder = base.DATA_DIR
        network = args.network or folder / "network.json"
        inventory = args.existing_assets or (folder / "inventory.json" if args.planning_mode == "brownfield" else None)
        if args.single_node:
            raise ValueError("experiment packages use explicit regional networks; single-node ablation is not enabled")
        return with_planning_mode(base, args.planning_mode, inventory, network)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))


def solve_system(design, scenario, idx):
    """Optimize new capacity and all dispatch; keep existing assets separate."""
    context = scenario.get("planning", planning_context())
    demand = np.asarray(scenario["demand_MW"])
    solar_cf = np.asarray(scenario["solar_cf"])
    wind_cf = np.asarray(scenario["wind_cf"])
    c = scenario["costs"]
    n = pypsa.Network()
    n.set_snapshots(idx)
    n.snapshot_weightings.loc[:, "objective"] = scenario.get("objective_weight", 1.0)
    extra = None
    if "transmission" in scenario:
        from transmission import add_spatial_system, constraints
        add_spatial_system(n, scenario, design, context, _add_existing)
        extra = constraints
    else:
        n.add("Bus", "grid")
        n.add("Load", "demand", bus="grid", p_set=pd.Series(demand, idx))
        n.add("Generator", "load_shedding", bus="grid", p_nom=demand.max(),
              marginal_cost=scenario["voll"])

        if design["wind"]:
            n.add("Generator", "wind", bus="grid", p_nom_extendable=True,
                  p_nom_max=design["wind_max_GW"] * 1e3,
                  capital_cost=c["wind"]["capital"],
                  marginal_cost=c["wind"]["marginal"],
                  p_max_pu=pd.Series(wind_cf, idx))
        if design["solar"]:
            n.add("Generator", "solar", bus="grid", p_nom_extendable=True,
                  p_nom_max=design["solar_max_GW"] * 1e3,
                  capital_cost=c["solar"]["capital"],
                  marginal_cost=c["solar"]["marginal"],
                  p_max_pu=pd.Series(solar_cf, idx))
        if design["nuclear"]:
            n.add("Generator", "nuclear", bus="grid", p_nom_extendable=True,
                  p_nom_max=design["nuclear_max_GW"] * 1e3,
                  capital_cost=c["nuclear"]["capital"],
                  marginal_cost=c["nuclear"]["marginal"],
                  p_min_pu=c["nuclear"]["p_min_pu"])
        if design["battery"]:
            # capital charged per MW of power plus per MWh of energy (via hours)
            bat_capital = (c["battery"]["capital_power"]
                           + design["battery_hours"] * c["battery"]["capital_energy"])
            n.add("StorageUnit", "battery", bus="grid", p_nom_extendable=True,
                  capital_cost=bat_capital,
                  max_hours=design["battery_hours"],
                  efficiency_store=0.95, efficiency_dispatch=0.95)
        if design["hydrogen"]:
            n.add("Bus", "h2")
            n.add("Link", "electrolyzer", bus0="grid", bus1="h2",
                  p_nom_extendable=True, capital_cost=c["electrolyzer"]["capital"],
                  efficiency=c["electrolyzer"]["eff"])
            n.add("Store", "h2_storage", bus="h2", e_nom_extendable=True,
                  capital_cost=c["h2_storage"]["capital"])
            n.add("Link", "fuel_cell", bus0="h2", bus1="grid",
                  p_nom_extendable=True, capital_cost=c["fuel_cell"]["capital"],
                  efficiency=c["fuel_cell"]["eff"])

        _add_existing(n, scenario, context)

    from extensions import physical_constraints
    prior_extra = extra
    def combined_extra(net, stamps):
        if prior_extra:
            prior_extra(net, stamps)
        physical_constraints(net, scenario, design)
    extra = combined_extra
    if len(n.storage_units):
        n.storage_units.loc[:, "cyclic_state_of_charge"] = True
    if len(n.stores):
        n.stores.loc[:, "e_cyclic"] = True
    n.meta["policy_design"] = design
    t0 = time.time()
    name, version, threads = choose_solver()
    ceiling = float(scenario.get("solver_time_limit", SAFETY_CEILING_SECONDS))
    n.meta["solver"] = dict(name=name, version=version, threads=threads, mip_gap_tolerance=0.001, safety_ceiling_seconds=ceiling,
                            note="Solves run to the gap tolerance; the ceiling is a zombie guard, not a budget. Solve time is an outcome.")
    import os
    logdir = os.environ.get("GRID_SOLVER_LOGDIR")  # progress visible without console noise
    if name == "gurobi":
        options = {"OutputFlag": 0, "MIPGap": 0.001, "Threads": threads, "TimeLimit": ceiling, "Method": 2}
        if logdir:
            # Gurobi writes LogFile only when OutputFlag=1; LogToConsole=0 keeps the console quiet.
            options.update(OutputFlag=1, LogToConsole=0, LogFile=str(pathlib.Path(logdir) / f"gurobi_{time.strftime('%Y%m%d_%H%M%S')}.log"))
    else:
        options = {"output_flag": False, "mip_rel_gap": 0.001, "threads": threads, "time_limit": ceiling}
        if logdir:
            options.update(output_flag=True, log_to_console=False, log_file=str(pathlib.Path(logdir) / f"highs_{time.strftime('%Y%m%d_%H%M%S')}.log"))
    tuning = json.loads(os.environ.get("GRID_SOLVER_EXTRA", "{}"))  # instrument tuning, recorded with the result
    options.update(tuning)
    n.meta["solver"]["extra_options"] = tuning
    import sys
    print(f"[solve] {name} {version}, {threads} threads, {len(n.snapshots)} h, gap tol 0.1%, no time budget (ceiling {ceiling:.0f} s)"
          + (f"; progress log: {options.get('LogFile') or options.get('log_file')}" if logdir else ""), file=sys.stderr, flush=True)
    status, condition = n.optimize(solver_name=name, extra_functionality=extra, solver_options=options)
    solve_seconds = round(time.time() - t0, 1)
    print(f"[solve] {status}/{condition} in {solve_seconds:.0f} s", file=sys.stderr, flush=True)
    solved = status == "ok" and condition == "optimal"
    incumbent = False
    if condition == "time_limit":
        sm = getattr(n.model, "solver_model", None)
        try:
            if hasattr(sm, "getSolution"):
                incumbent = bool(sm.getSolution().value_valid)
            else:
                incumbent = int(sm.SolCount) > 0
        except Exception:
            incumbent = False
        incumbent = incumbent and np.isfinite(n.objective)
    if not solved and not incumbent:
        return {"status": f"{status}/{condition}", "error": "No accepted solver solution", "planning_mode": context["mode"], "solve_seconds": solve_seconds}, None
    result = _report(n, scenario, context, solve_seconds)
    if not solved:
        result.update(status="feasible_time_limit", solver_warning="Feasible incumbent at time limit. Optimality tolerance NOT reached; reported MIP gap is an unresolved bound. Not certified optimal.")
    return result, n
