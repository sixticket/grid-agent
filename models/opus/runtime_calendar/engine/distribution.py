"""Replay a solved PyPSA dispatch on the representative OpenDSS feeder.

Battery AC power is replayed, not re-optimized: positive p is discharge,
negative p is charge. PyPSA owns energy balance, SOC and conversion losses.
The feeder remains a stylized two-section, balanced network, not a measured
regional grid. No line ampacity is inferred from OpenDSS default values.
"""

import numpy as np
from tqdm import tqdm

MODEL_VERSION = "battery-dispatch-v2-existing-assets"
BATTERY_DESIGN_SPACE = {
    "dist_battery_b1_fraction": {
        "type": "float", "min": 0, "max": 1,
        "doc": "fraction of feeder battery power AND energy at upstream b1; "
               "the remainder is at downstream b2. Replays the solved hourly "
               "dispatch at both sites; no free redispatch or added capacity",
    },
}
BATTERY_DEFAULTS = {"dist_battery_b1_fraction": 0.4}
COUPLING_SUMMARY = (
    " Battery AC dispatch and SOC from PyPSA are mapped by the same fixed "
    "feeder/system peak-load share as PV. dist_battery_b1_fraction selects "
    "the allocation between b1 and b2 (default 0.4/0.6). Charging adds load; "
    "discharging injects power. No battery voltage control or local redispatch "
    "is assumed. Checks return worst-hour voltages, line currents, battery "
    "power/SOC and convergence failures. Thermal feasibility is checked only "
    "when explicit line ratings are supplied; otherwise it is unknown."
)


def map_dispatch(network, demand_mw, config):
    """Return feeder-scale AC trajectories in kW/kWh, aligned to snapshots."""
    demand = np.asarray(demand_mw, dtype=float)
    hours = len(network.snapshots)
    if (demand.shape != (hours,) or not np.isfinite(demand).all()
            or np.any(demand < 0) or demand.max() <= 0):
        raise ValueError("demand must be finite, nonnegative and snapshot-aligned")
    peak_kw = float(config["feeder_peak_kW"])
    if not np.isfinite(peak_kw) or peak_kw <= 0:
        raise ValueError("feeder_peak_kW must be positive and finite")
    # MW -> kW, followed by fixed feeder share of system peak load.
    mw_to_feeder_kw = peak_kw / demand.max()

    def series(table, name):
        values = table[name].reindex(network.snapshots).to_numpy(dtype=float)
        if values.shape != (hours,) or not np.isfinite(values).all():
            raise ValueError(f"missing/nonfinite solved trajectory: {name}")
        return values * mw_to_feeder_kw

    pv_kw = np.zeros(hours)
    pv_capacity_kw = 0.0
    assets = getattr(network, "meta", {}).get("planning_assets", {})
    for name in network.generators.index:
        if name.split("::")[0] == "solar" or assets.get(name, {}).get("technology") == "solar":
            pv_kw += series(network.generators_t.p, name)
            pv_capacity_kw += float(network.generators.at[name, "p_nom_opt"]) * mw_to_feeder_kw
    battery_kw = np.zeros(hours)
    soc_kwh = np.zeros(hours)
    battery_capacity_kw = battery_capacity_kwh = 0.0
    batteries = []
    for name in network.storage_units.index:
        if name.split("::")[0] == "battery" or assets.get(name, {}).get("technology") == "battery":
            power = series(network.storage_units_t.p, name)
            soc = series(network.storage_units_t.state_of_charge, name)
            cap = float(network.storage_units.at[name, "p_nom_opt"]) * mw_to_feeder_kw
            energy = cap * float(network.storage_units.at[name, "max_hours"])
            batteries.append(dict(name=name, power_kw=power,
                                  b1_fraction=assets.get(name, {}).get("b1_fraction")))
            battery_kw += power
            soc_kwh += soc
            battery_capacity_kw += cap
            battery_capacity_kwh += energy
    return dict(load_kw=demand * mw_to_feeder_kw, pv_kw=pv_kw,
                pv_capacity_kw=pv_capacity_kw, battery_kw=battery_kw,
                soc_kwh=soc_kwh, battery_capacity_kw=battery_capacity_kw,
                battery_capacity_kwh=battery_capacity_kwh, batteries=batteries,
                feeder_system_share=mw_to_feeder_kw / 1000)


def feeder_sweep(network, demand_mw, config, design, upgrade_cost_billion,
                 *, include_hourly=False, label="feeder"):
    """Validate voltage/convergence, plus thermal limits when supplied.

    Optional config line_normal_amps maps l1/l2 to documented normal ratings.
    line_upgrade_normal_amps can override them for a documented upgrade;
    absent an override, an impedance upgrade does not invent extra ampacity.
    include_hourly is for offline diagnostics; agents receive ten worst hours.
    """
    import opendssdirect as dss

    mapped = map_dispatch(network, demand_mw, config)
    fraction = design.get("dist_battery_b1_fraction", 0.4)
    if (isinstance(fraction, bool) or not isinstance(fraction, (float, int))
            or not np.isfinite(fraction) or not 0 <= fraction <= 1):
        raise ValueError("dist_battery_b1_fraction must lie in [0, 1]")
    upgraded = bool(design.get("dist_line_upgrade"))
    volt_var = bool(design.get("dist_volt_var"))
    ratings = dict(config.get("line_normal_amps", {}))
    if upgraded:
        ratings.update(config.get("line_upgrade_normal_amps", {}))
    if any(k not in ("l1", "l2") or isinstance(v, bool)
           or not np.isfinite(v) or v <= 0 for k, v in ratings.items()):
        raise ValueError("line ratings must be positive finite amps keyed by l1/l2")

    kv, km = config["feeder_kv"], config["line_km"]
    imp = config["line_upgrade_impedance_factor"] if upgraded else 1.0
    r, x = config["line_r1_ohm_km"] * imp, config["line_x1_ohm_km"] * imp
    dss.Text.Command("Clear")
    dss.Text.Command(f"New Circuit.feeder basekv={kv} bus1=src phases=3 pu=1.0")
    for name, start, end in (("l1", "src", "b1"), ("l2", "b1", "b2")):
        dss.Text.Command(f"New Line.{name} bus1={start} bus2={end} phases=3 "
                         f"length={km} units=km r1={r} x1={x}")
    for bus in ("b1", "b2"):
        dss.Text.Command(f"New Load.load_{bus} bus1={bus} phases=3 kv={kv} kw=0 pf=0.95")
        # Separate nonnegative load/generator elements avoid sign ambiguity.
        # Their operating window covers the voltage acceptance band; voltage
        # failures remain failures even if DSS falls back outside this window.
        dss.Text.Command(f"New Load.charge_{bus} bus1={bus} phases=3 kv={kv} "
                         "kw=0 kvar=0 model=1 vminpu=0.8 vmaxpu=1.2")
        dss.Text.Command(f"New Generator.discharge_{bus} bus1={bus} phases=3 "
                         f"kv={kv} kw=0 kvar=0 model=1 vminpu=0.8 vmaxpu=1.2")
    dss.Text.Command(f"New Generator.pv bus1=b2 phases=3 kv={kv} "
                     f"kw={mapped['pv_capacity_kw']} pf=1.0 model=1")
    dss.Text.Command(f"Set voltagebases=[{kv}]")
    dss.Text.Command("CalcVoltageBases")
    dss.Text.Command("Set mode=snapshot controlmode=off maxiterations=100 tolerance=0.000001")
    kvar_ratio = float(np.tan(np.arccos(config["volt_var_pf"])))
    v_lo, v_hi = config["v_band_pu"]
    records = []

    def solve():
        dss.Solution.Solve()
        if not dss.Solution.Converged():
            return None
        volts = {}
        for bus in ("src", "b1", "b2"):
            dss.Circuit.SetActiveBus(bus)
            values = dss.Bus.puVmagAngle()[::2]
            if not values or not np.isfinite(values).all():
                return None
            volts[bus] = {"min_pu": float(min(values)), "max_pu": float(max(values))}
        currents = {}
        for name in ("l1", "l2"):
            dss.Circuit.SetActiveElement(f"Line.{name}")
            amps = dss.CktElement.CurrentsMagAng()[::2]
            if not amps or not np.isfinite(amps).all():
                return None
            currents[name] = float(max(amps))
        return volts, currents

    for h, stamp in enumerate(tqdm(network.snapshots, desc=f"OpenDSS {label}", unit="h", leave=False, disable=len(network.snapshots) < 200)):
        power = float(mapped["battery_kw"][h])
        battery_bus_kw = {"b1": 0.0, "b2": 0.0}
        for battery in mapped["batteries"]:
            share = battery["b1_fraction"] if battery["b1_fraction"] is not None else fraction
            battery_bus_kw["b1"] += float(battery["power_kw"][h]) * share
            battery_bus_kw["b2"] += float(battery["power_kw"][h]) * (1 - share)
        for bus, load_share in (("b1", 0.4), ("b2", 0.6)):
            dss.Loads.Name(f"load_{bus}")
            dss.Loads.kW(float(mapped["load_kw"][h] * load_share))
            dss.Loads.PF(0.95)
            dss.Loads.Name(f"charge_{bus}")
            dss.Loads.kW(max(-battery_bus_kw[bus], 0))
            dss.Loads.kvar(0)
            dss.Generators.Name(f"discharge_{bus}")
            dss.Generators.kW(max(battery_bus_kw[bus], 0))
            dss.Generators.kvar(0)
        dss.Generators.Name("pv")
        dss.Generators.kW(float(mapped["pv_kw"][h]))
        dss.Generators.kvar(0)
        solved = solve()
        if solved and volt_var and max(v["max_pu"] for v in solved[0].values()) > config["volt_var_deadband_pu"]:
            dss.Generators.Name("pv")
            dss.Generators.kvar(-float(mapped["pv_kw"][h]) * kvar_ratio)
            solved = solve()
        record = dict(hour=h, timestamp=str(stamp), converged=solved is not None,
                      load_kW=float(mapped["load_kw"][h]), pv_kW=float(mapped["pv_kw"][h]),
                      battery_kW=power, battery_by_bus_kW=battery_bus_kw,
                      battery_soc_kWh=float(mapped["soc_kwh"][h]))
        if solved:
            volts, currents = solved
            low, high = min(v["min_pu"] for v in volts.values()), max(v["max_pu"] for v in volts.values())
            record.update(bus_voltage=volts, line_current_A=currents,
                          undervoltage=low < v_lo, overvoltage=high > v_hi,
                          overloaded_lines=[name for name, rating in ratings.items() if currents[name] > rating])
        records.append(record)

    good = [r for r in records if r["converged"]]
    failed = len(records) - len(good)
    over = sum(r["overvoltage"] for r in good)
    under = sum(r["undervoltage"] for r in good)
    voltage_hours = sum(r["overvoltage"] or r["undervoltage"] for r in good)
    thermal_hours = sum(bool(r["overloaded_lines"]) for r in good) if ratings else None

    def severity(record):
        if not record["converged"]:
            return float("inf")
        voltage = max(max(v_lo - v["min_pu"], v["max_pu"] - v_hi, 0) for v in record["bus_voltage"].values())
        thermal = max((record["line_current_A"][k] / v - 1 for k, v in ratings.items()), default=0)
        return max(voltage, thermal)

    result = dict(
        distribution_model_version=MODEL_VERSION,
        feeder_pv_MW=round(mapped["pv_capacity_kw"] / 1000, 3),
        feeder_battery_MW=round(mapped["battery_capacity_kw"] / 1000, 6),
        feeder_battery_MWh=round(mapped["battery_capacity_kwh"] / 1000, 6),
        feeder_system_share=mapped["feeder_system_share"],
        battery_b1_fraction=fraction,
        battery_location_basis="battery_b1_fraction controls new batteries; existing batteries retain inventory locations",
        battery_max_discharge_kW=float(max(mapped["battery_kw"].max(), 0)),
        battery_max_charge_kW=float(max(-mapped["battery_kw"].min(), 0)),
        overvoltage_hours=over, undervoltage_hours=under,
        voltage_violation_hours=voltage_hours,
        violation_pct=round(100 * voltage_hours / len(records), 2),
        nonconverged_hours=failed,
        worst_high_pu=max((v["max_pu"] for r in good for v in r["bus_voltage"].values()), default=None),
        worst_low_pu=min((v["min_pu"] for r in good for v in r["bus_voltage"].values()), default=None),
        line_peak_current_A={name: max((r["line_current_A"][name] for r in good), default=None) for name in ("l1", "l2")},
        line_normal_amps=ratings, thermal_checked_lines=sorted(ratings),
        thermal_check_complete=len(ratings) == 2 and failed == 0,
        thermal_overload_hours=thermal_hours,
        passes=voltage_hours == 0 and failed == 0 and not thermal_hours,
        volt_var_active=volt_var, line_upgrade_active=upgraded,
        distribution_capex_billion=upgrade_cost_billion if upgraded else 0.0,
        diagnostic_hours=sorted(records, key=severity, reverse=True)[:10],
        note="Stylized representative feeder; full demand, PV and battery share replayed. "
             "Battery uses PyPSA AC dispatch at unity power factor; SOC/efficiency "
             "are not simulated a second time. No local redispatch or extra battery capex. "
             "passes covers voltage, convergence and supplied thermal limits only; "
             "missing ratings do not establish thermal feasibility. Currents/voltages "
             "exclude nonconverged snapshots. Distribution capex is additive to LP cost.",
    )
    if include_hourly:
        result["hourly"] = records
    return result
