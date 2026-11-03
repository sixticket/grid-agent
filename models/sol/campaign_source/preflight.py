#!/usr/bin/env python3
"""Prepare-only input/runtime checks for a configured weather campaign.

Validates selected NASA packs and loads each distinct region/year route for
just two hours. Does not invoke any agent, model/API, inference server,
optimization, or license test. The receipt proves input and loader readiness,
not provider credit, GPU serving, solver license, or annual model acceptance.
"""
from __future__ import annotations
# Preserved campaign source reference.
if __name__ == '__main__':
    raise SystemExit('Reference only; use tools/run_campaign.py. Historical replay requires the original pinned archive.')

import argparse
import datetime as dt
import importlib.util
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace

sys.dont_write_bytecode = True
BASE = Path(__file__).resolve().parent
PROJECT = BASE.parents[1]
RECEIPT = BASE / "offline_preflight.json"
from run_campaign import CAMPAIGN_YEARS, EXPECTED_CONFIG
YEARS = tuple(CAMPAIGN_YEARS)
REGIONS = ("miso", "ercot", "nyiso", "australia", "new_zealand", "mexico", "brazil", "south_africa")
PACKAGES = ("openai", "anthropic", "numpy", "pandas", "pypsa", "linopy", "gurobipy", "OpenDSSDirect.py")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def environment():
    packages = {}
    for name in PACKAGES:
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    return {"python_executable": sys.executable, "python_version": sys.version.split()[0], "packages": packages}


def expected_job_ids():
    return {f"{year}/{region}/{agent}" for year in YEARS for region in REGIONS for agent in ("single", "multi")}


def check_scope(state):
    from run_campaign import assert_plan_scope
    assert_plan_scope(state)
    require(len(state["jobs"]) == len(expected_job_ids()) and {j["id"] for j in state["jobs"]} == expected_job_ids(), "Wrong configured run scope")
    for job in state["jobs"]:
        require(job["agent"] in ("single", "multi") and job["id"] == f"{job['year']}/{job['region']}/{job['agent']}", "Job fields do not match identity")
        require(job.get("climate_model") == (None if job["year"] == 2023 else "MPI-ESM1-2-HR"), "Only MPI-ESM1-2-HR future climate is authorized")
    require(state.get("climate_model") == "MPI-ESM1-2-HR" and state.get("emissions_scenario") == "ssp245", "Wrong climate scenario")
    require(state["config"] == EXPECTED_CONFIG, "Model/provider configuration differs from study_config.json")


def check_runtime_provenance():
    """Only the documented provider/accounting/calendar-metadata edits may differ."""
    from run_campaign import sha
    origin = json.loads((BASE / "runtime_origin.json").read_text())
    result = {}
    for name in ("runtime_original", "runtime_calendar"):
        record = origin["runtimes"][name]
        source = Path(record["source_path"])
        allowed = {"experiment/engine/providers.py", "experiment/engine/api_usage.py"}
        if name == "runtime_calendar":
            allowed.add("experiment/engine/scenario_core.py")
            if BASE.name == "qwen":
                allowed.update({"experiment/engine/transmission.py", "experiment/engine/reporting.py"})
        declared = record.get("allowed_changes")
        if declared is not None:
            require(set(declared) <= allowed, f"Unexpected declared runtime adaptation: {name}")
            allowed = set(declared)
        unchanged, adaptations = {}, {}
        for relative, before in record["files"].items():
            original_file = Path(record.get("source_files", {}).get(relative, source / relative)).resolve()
            require(original_file.is_relative_to(PROJECT), f"Runtime source outside project: {original_file}")
            require(sha(original_file) == before, f"Original runtime changed: {original_file}")
            current = sha(BASE / name / relative)
            if current == before:
                unchanged[relative] = current
            else:
                require(relative in allowed, f"Unexpected copied-runtime change: {name}/{relative}")
                adaptations[relative] = {"source_sha256": before, "campaign_sha256": current}
        result[name] = {"status": "passed", "unchanged_files": unchanged, "documented_adaptations": adaptations}
    if origin.get("adapters"):
        adapters = {}
        for name, record in origin["adapters"].items():
            source = Path(record["source_path"]).resolve()
            target = (BASE / "adapters" / name).resolve()
            require(source.is_relative_to(PROJECT) and target.is_relative_to(BASE / "adapters"), f"Invalid adapter provenance path: {name}")
            require(record.get("unchanged") is True, f"Undocumented adapter modification: {name}")
            require(sha(source) == record["sha256"] == sha(target), f"Adapter differs from canonical baseline: {name}")
            adapters[name] = {"status": "passed", "source_path": str(source), "sha256": record["sha256"]}
        result["adapters"] = adapters
    return result


def verify_receipt(state):
    """Fail closed on stale or incomplete offline validation; no API calls."""
    from run_campaign import input_hashes, sha, source_hashes
    check_scope(state)
    require(RECEIPT.is_file(), "Run preflight.py before execution: offline receipt is missing")
    receipt = json.loads(RECEIPT.read_text())
    require(receipt.get("schema_version") == 1 and receipt.get("status") == "passed", "Offline preflight did not pass")
    require(receipt.get("model_api_calls") == 0 and receipt.get("annual_solver_runs") == 0 and receipt.get("solver_executed") is False, "Preflight must perform no model/API or optimization execution")
    require(receipt["config"] == state["config"] and receipt["settings"] == state["settings"], "Preflight settings/config changed")
    require(receipt["source_sha256"] == state["source_sha256"] == source_hashes(), "Code changed since offline preflight")
    require(receipt["environment"] == environment(), "Python/dependencies changed since offline preflight")
    require(receipt.get("runtime_provenance") == check_runtime_provenance(), "Runtime provenance changed after offline preflight")
    expected_inputs = {job["id"]: job["input_sha256"] for job in state["jobs"]}
    require(receipt["input_sha256"] == expected_inputs, "Preflight input identity differs from the campaign")
    for job in state["jobs"]:
        require(input_hashes(Path(job["pack"])) == expected_inputs[job["id"]], f"Inputs changed after preflight: {job['id']}")
    expected_routes = {(region, year) for year in YEARS for region in REGIONS}
    routes = receipt.get("routes", [])
    require(len(routes) == len(expected_routes) and {(r["region"], r["year"]) for r in routes} == expected_routes, "Preflight did not cover every configured route")
    require(all(r.get("status") == "passed" and r.get("hours_loaded") == 2 for r in routes), "A route preflight failed")
    climates = receipt.get("climate_packs", [])
    require(len(climates) == 16 and {(r["region"], r["year"]) for r in climates} == {(r, y) for r in REGIONS for y in (2030, 2035)}, "Preflight did not cover all 16 future packs")
    require(all(r.get("status") == "validated" and r.get("model") == "MPI-ESM1-2-HR" for r in climates), "A future pack did not pass full validation")
    require(receipt.get("gurobi", {}).get("status") == "not_run", "Preparation receipt must not claim a new solver/license test")
    return {"path": str(RECEIPT), "sha256": sha(RECEIPT), "status": "passed", "routes": len(expected_routes), "climate_packs": 16}


def block_network():
    def forbidden(*args, **kwargs):
        raise RuntimeError("Network connections are disabled in offline route preflight")
    socket.create_connection = forbidden
    socket.socket.connect = forbidden
    socket.socket.connect_ex = forbidden


def load_route(region, year):
    """Runs in a separate process so original/calendar modules cannot mix."""
    require(region in REGIONS and year in YEARS, "Unknown preflight route")
    block_network()
    from run_campaign import pack_path, runtime_path
    pack, runtime = pack_path(region, year), runtime_path(region, year)
    os.environ.update(GRID_EXPERIMENT_HOURS="2", GRID_EXPERIMENT_START="0")
    for name in ("GRID_RESUME", "GRID_RECOVERY_AUDIT", "GRID_RESUME_MAX_CYCLES", "GRID_SOLVER_EXTRA"):
        os.environ.pop(name, None)
    sys.path.insert(0, str(runtime / "engine"))
    import numpy as np
    import pandas as pd
    import scenario_core
    from climate_adapter import install_climate_adapter
    install_climate_adapter(scenario_core, pack)
    base = scenario_core.load_pack(str(pack), policy="baseline")
    import planning
    def no_optimization(*args, **kwargs):
        raise RuntimeError("Optimization is disabled in preparation-only preflight")
    planning.solve_system = no_optimization
    planning.choose_solver = no_optimization
    sim = planning.with_planning_mode(base, "brownfield", pack / "inventory.json", pack / "network.json")
    require(base.HOURS == 2 and sim.HOURS == 2 and len(sim.SNAPSHOTS) == 2, "Wrong dry-load horizon")
    require(list(sim.SNAPSHOTS) == list(pd.date_range("2023-01-01", periods=2, freq="h")), "Wrong model time axis")
    require(sim.SCENARIO["electricity_policy"]["mode"] == "baseline", "Policy changed in dry load")
    require(sim.SCENARIO["planning"]["mode"] == "brownfield", "Planning mode changed")
    config = json.loads((pack / "config.json").read_text())
    if year != 2023:
        metadata = json.loads((pack / "prediction_manifest.json").read_text())["scenario"]
        require(metadata == config["prediction"] == sim.SCENARIO["prediction"], "Future climate metadata not retained")
        require(metadata["planning_year"] == metadata["weather_year"] == year, "Wrong projection/planning year")
        require(metadata["weather_template_year"] == metadata["time_axis_year"] == 2023, "Wrong template/time axis")
        require(metadata["climate"]["model"] == "MPI-ESM1-2-HR" and metadata["climate"]["scenario"] == "ssp245", "Wrong future climate selection")
    else:
        require(not config.get("prediction") and sim.SCENARIO["year"] == 2023, "Baseline must retain ERA5 2023")
    if (region, year) == ("south_africa", 2030):
        require(sim.SCENARIO["planning_year"] == 2030, "Corrected South Africa planning calendar lost")
        require(sim.SCENARIO["transmission"].get("planning_year") == 2030, "South Africa transmission calendar lost")
    network = json.loads((pack / "network.json").read_text())
    nodes = sim.SCENARIO["transmission"]["nodes"]
    require(set(nodes) == {n["id"] for n in network["nodes"]}, "Loaded network node set changed")
    for node in network["nodes"]:
        expected = pd.read_csv(pack / node["profiles_csv"]).iloc[:2]
        for field in ("demand_MW", "solar_cf", "wind_cf"):
            value = np.asarray(nodes[node["id"]][field], dtype=float)
            require(value.shape == (2,) and np.isfinite(value).all() and (value >= 0).all(), f"Invalid loaded profile: {node['id']}/{field}")
            if field.endswith("_cf"):
                require((value <= 1).all(), "Loaded capacity factor exceeds one")
            np.testing.assert_allclose(value, expected[field].to_numpy(), rtol=1e-12, atol=1e-8)
    return {"status": "passed", "region": region, "year": year, "pack": str(pack), "runtime": str(runtime),
            "hours_loaded": 2, "node_count": len(nodes), "time_axis_year": 2023, "weather_year": year,
            "scenario_hash": sim.SCENARIO_HASH, "solver_executed": False, "model_api_calls": 0, "network_disabled": True, "optimization_disabled": True}


def run_preflight():
    from run_campaign import make_plan, input_hashes, save, source_hashes
    receipt = {"schema_version": 1, "status": "running", "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
               "environment": environment(), "model_api_calls": 0, "annual_solver_runs": 0,
               "solver_executed": False,
               "gurobi": {"status": "not_run", "reason": "Preparation only; solver license and optimization were not tested."},
               "scope": "Input/hash validation and two-hour object construction only; no paid/local model inference or optimization.",
               "routes": [], "climate_packs": []}
    try:
        require(all(receipt["environment"]["packages"].values()), "Use experiment/.venv/bin/python with the existing solver dependencies")
        state = make_plan(SimpleNamespace(years=list(YEARS), agents=["single", "multi"], regions=None, include_south_africa=True))
        check_scope(state)
        receipt["runtime_provenance"] = check_runtime_provenance()
        receipt.update(config=state["config"], settings=state["settings"], source_sha256=state["source_sha256"],
                       input_sha256={j["id"]: j["input_sha256"] for j in state["jobs"]})
        builder = PROJECT / "nasa_climate/scripts/build_packs.py"
        spec = importlib.util.spec_from_file_location("nasa_preflight_pack_validation", builder)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        unique = {(j["region"], j["year"]): j for j in state["jobs"]}
        require(len(unique) == len(YEARS) * len(REGIONS), "Wrong number of unique input routes")
        for (region, year), job in unique.items():
            if year != 2023:
                receipt["climate_packs"].append(module.validate_pack(Path(job["pack"]), verify_upstream=True))
        env = os.environ.copy()
        for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "GRID_API_RESPONSE_LOG", "GRID_API_USAGE_LOG", "GRID_CAMPAIGN_WORKER_TOKEN", "GRID_CORRECTION_WORKER_TOKEN"):
            env.pop(name, None)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        for (region, year), job in unique.items():
            child = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "--load-route", region, str(year)],
                                   cwd=BASE, env=env, text=True, capture_output=True, timeout=120)
            if child.returncode:
                raise RuntimeError(f"Route {year}/{region} failed: {child.stderr[-6000:]}")
            result = json.loads(child.stdout.strip().splitlines()[-1])
            receipt["routes"].append(result)
            print(json.dumps({"checked_route": f"{year}/{region}", "status": result["status"], "completed": len(receipt["routes"]), "total": len(unique)}), flush=True)
        require(source_hashes() == receipt["source_sha256"], "Source files changed during preflight")
        for job in state["jobs"]:
            require(input_hashes(Path(job["pack"])) == receipt["input_sha256"][job["id"]], f"Inputs changed during preflight: {job['id']}")
        receipt.update(status="passed", completed_at=dt.datetime.now(dt.timezone.utc).isoformat())
        save(RECEIPT, receipt)
        verify_receipt(state)
        print(json.dumps({"status": "passed", "receipt": str(RECEIPT), "jobs": len(state["jobs"]), "routes": len(unique), "climate_packs": len(receipt["climate_packs"]),
                          "model_api_calls": 0, "annual_solver_runs": 0, "solver_executed": False, "solver_license_test": "not_run"}), flush=True)
        return 0
    except Exception as exc:
        receipt.update(status="failed", completed_at=dt.datetime.now(dt.timezone.utc).isoformat(), error_type=type(exc).__name__, error=str(exc))
        save(RECEIPT, receipt)
        print(json.dumps({"status": "failed", "receipt": str(RECEIPT), "error_type": type(exc).__name__, "error": str(exc)}), file=sys.stderr, flush=True)
        return 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--load-route", nargs=2, metavar=("REGION", "YEAR"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.load_route:
        print(json.dumps(load_route(args.load_route[0], int(args.load_route[1]))))
        return 0
    return run_preflight()


if __name__ == "__main__":
    raise SystemExit(main())
