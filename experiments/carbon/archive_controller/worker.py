#!/usr/bin/env python3
"""Load, simulate, or run one pinned CO2 scenario in a fresh process.

The controller owns case selection, immutable source/input hashes and execution
authorization. This worker changes no archived input or frozen engine source.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import importlib
import importlib.util
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import socket
import sys

sys.dont_write_bytecode = True
BASE = Path(__file__).resolve().parent
PROJECT = BASE.parent
PACKAGES = ("openai", "anthropic", "numpy", "pandas", "pypsa", "linopy",
            "gurobipy", "OpenDSSDirect.py")
MODEL_DIRECTORIES = {
    "sol": "gpt_6_1_sol", "gpt_6_1_sol": "gpt_6_1_sol",
    "opus": "claude_opus_5_5", "claude_opus_5_5": "claude_opus_5_5",
}
API_CREDENTIAL_NAMES = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                        "GRID_LOCAL_API_KEY")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def block_network():
    """Disable Python socket connections for a preparation-only worker."""
    def forbidden(*args, **kwargs):
        raise RuntimeError("Network/API access is disabled in offline CO2 preflight")
    socket.create_connection = forbidden
    socket.socket.connect = forbidden
    socket.socket.connect_ex = forbidden


def block_optimization(planning):
    def forbidden(*args, **kwargs):
        raise RuntimeError("Optimization is disabled in offline CO2 preflight")
    planning.solve_system = forbidden
    planning.choose_solver = forbidden


def package_versions():
    result = {}
    for name in PACKAGES:
        try:
            result[name] = version(name)
        except PackageNotFoundError:
            result[name] = None
    return result


def load_file_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    require(spec is not None and spec.loader is not None, f"Cannot load module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def provider_contract(config, runtime):
    """Inspect the frozen provider without constructing any API client."""
    import providers
    source = Path(providers.__file__).resolve()
    require(source == (runtime / "engine" / "providers.py").resolve(),
            "Provider module was not loaded from the pinned numerical runtime")
    require(providers.MAX_TOKENS == config["output_tokens"],
            "Frozen provider output-token limit differs from the job configuration")
    require(providers.DEFAULT_MODELS.get(config["provider"]) == config["model"],
            "Frozen provider default model differs from the selected deployment")
    return dict(status="passed", provider=config["provider"], model=config["model"],
                provider_file=str(source), provider_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                max_output_tokens=providers.MAX_TOKENS, client_instantiated=False)


def configure_environment(mode, settings, config, out):
    hours = 2 if mode == "preflight" else settings["hours"]
    require(type(hours) is int and 2 <= hours <= 8760, "Invalid simulation horizon")
    configured = dict(
        GRID_EXPERIMENT_HOURS=str(hours), GRID_EXPERIMENT_START="0",
        GRID_SOLVER=settings["solver"], GRID_SOLVER_THREADS=str(settings["threads"]),
        GRID_SOLVER_TIME_LIMIT=str(settings["solver_seconds"]),
        GRID_SOLVER_LOGDIR=str(out), GRID_RUN_OUTPUT=str(out),
        GRID_API_USAGE_LOG=str(out / "api_usage.jsonl"),
        GRID_API_RESPONSE_LOG=str(out / "api_responses.jsonl"),
        GRID_REASONING_EFFORT=config["effort"], GRID_ANTHROPIC_CACHE_TTL="5m",
        PYTHONDONTWRITEBYTECODE="1",
    )
    # A shell left over from an older campaign must not enable recovery,
    # alternative solver tuning or different logging. Controller tokens are
    # retained only for re-verification and are never written to a receipt.
    for key in list(os.environ):
        if key.startswith("GRID_") and key not in configured and not key.endswith("_WORKER_TOKEN"):
            os.environ.pop(key, None)
    os.environ.update(configured)
    if mode != "agent":
        for key in API_CREDENTIAL_NAMES:
            os.environ.pop(key, None)
    if mode == "preflight":
        block_network()
    return hours


def prepare_runtime(record, hours, mode):
    """Validate the source climate pack, then install the fixed CO2 overlay."""
    job = record["job"]
    case = job["case"]
    require(isinstance(case, dict), "job.case must contain the pinned source case")
    pack, runtime = Path(job["pack"]).resolve(), Path(job["runtime"]).resolve()
    model_dir = PROJECT / "weather_prediction" / MODEL_DIRECTORIES[job["model_key"]]
    # Shared adapter/provider helpers must resolve from the selected deployment;
    # the numerical engine itself resolves from the controller-pinned runtime.
    sys.path.insert(0, str(model_dir))
    sys.path.insert(0, str(model_dir / "adapters"))
    sys.path.insert(0, str(runtime / "engine"))
    import scenario_core
    import planning
    import reporting
    provider_receipt = provider_contract(record["config"], runtime)
    if mode == "preflight":
        block_optimization(planning)
    climate_adapter = load_file_module("_co2_source_climate_adapter", model_dir / "climate_adapter.py")
    climate_receipt = climate_adapter.install_climate_adapter(
        scenario_core, pack, expected_region=case["region"], expected_year=case["year"])
    original_hash = None
    if mode == "preflight":
        original = planning.with_planning_mode(
            scenario_core.load_pack(str(pack), policy=record["settings"]["policy"]),
            "brownfield", pack / "inventory.json", pack / "network.json")
        original_hash = original.SCENARIO_HASH
    # Import explicitly from this experiment, never from the upstream runtime.
    co2_adapter = load_file_module("_co2_constraint_adapter", BASE / "co2_adapter.py")
    co2_receipt = co2_adapter.install(scenario_core, case, pack)
    gate_receipt = co2_adapter.install_acceptance_gate(reporting, case, hours)
    base = scenario_core.load_pack(str(pack), policy=record["settings"]["policy"])
    sim = planning.with_planning_mode(base, "brownfield", pack / "inventory.json", pack / "network.json")
    require(sim.HOURS == hours and len(sim.SNAPSHOTS) == hours, "Loaded horizon differs from worker settings")
    require(sim.SCENARIO["assumptions"]["co2_limit_Mt"] == case["co2_limit_Mt"],
            "Loaded scenario does not contain the pinned CO2 cap")
    require("co2_limit_Mt" not in sim.DESIGN_SPACE, "CO2 cap cannot be an agent design variable")
    if original_hash is not None:
        require(original_hash != sim.SCENARIO_HASH, "CO2 scenario identity was not changed")
    return sim, reporting, {
        "climate_adapter": climate_receipt, "co2_adapter": co2_receipt,
        "co2_acceptance_gate": gate_receipt,
        "provider_contract": provider_receipt,
        "original_scenario_hash": original_hash,
        "constrained_scenario_hash": sim.SCENARIO_HASH,
    }


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def attach_api_accounting(final, out, config, model_dir):
    from api_usage import summarize_ledger
    ledger = read_jsonl(out / "api_usage.jsonl")
    journal = read_jsonl(out / "api_responses.jsonl")
    require(not (Counter(row["response_id"] for row in ledger)
                 - Counter(row["response_id"] for row in journal)),
            "API response journal does not cover the usage ledger")
    final["api_usage_summary"] = summarize_ledger(out / "api_usage.jsonl")
    final["api_response_journal"] = dict(recorded_responses=len(journal),
                                       accounted_responses=len(ledger), ledger_coverage_complete=True)
    metrics = final.setdefault("metrics", {})
    final["agent_reported_usage"] = {key: metrics.get(key)
                                     for key in ("api_calls", "input_tokens", "output_tokens")}
    for key in ("api_calls", "input_tokens", "output_tokens"):
        metrics[key] = final["api_usage_summary"].get(key)
    metrics["legacy_estimated_llm_cost_usd"] = metrics.get("estimated_llm_cost_usd")
    metrics["estimated_llm_cost_usd"] = final["api_usage_summary"]["estimated_cost_usd"]
    metrics["billed_api_calls_recorded"] = final["api_usage_summary"]["api_calls"]
    if config["provider"] == "anthropic":
        protocol = load_file_module("_co2_opus_protocol", model_dir / "opus_protocol.py")
        final["thinking_protocol"] = protocol.validate_journals(out)


def run_agent(record, out):
    job, config, settings = record["job"], record["config"], record["settings"]
    require(job["agent"] in ("single", "multi"), "Unknown agent architecture")
    module = importlib.import_module(job["agent"] + "_agent")
    sys.argv = [job["agent"] + "_agent", "--scenario", str(Path(job["pack"]).resolve()),
                "--planning-mode", "brownfield", "--policy", settings["policy"],
                "--provider", config["provider"], "--model", config["model"]]
    sys.argv += (["--max-cycles", str(settings["max_cycles"])] if job["agent"] == "multi" else
                 ["--max-runs", str(settings["max_runs"]), "--max-turns", str(settings["max_turns"])])
    module.main()
    final = json.loads((out / "final.json").read_text())
    model_dir = PROJECT / "weather_prediction" / MODEL_DIRECTORIES[job["model_key"]]
    attach_api_accounting(final, out, config, model_dir)
    return final


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--mode", choices=("preflight", "simulate", "agent"), required=True)
    args = parser.parse_args()
    job_path = args.job.resolve()
    record = json.loads(job_path.read_text())
    from run_campaign import verify_record, save, now
    verify_record(record)
    job, config, settings = record["job"], record["config"], record["settings"]
    out = job_path.parent
    require(record.get("execution_mode") == args.mode, "Requested mode differs from the pinned job")
    require(Path(job["output"]).resolve() == out, "Job file location differs from its authorized output")
    require(out.is_relative_to(BASE), "Worker output must remain inside co2_constraint")
    require(not any((out / name).exists() for name in ("log.jsonl", "final.json", "preflight.json")),
            "Worker requires a fresh attempt directory")
    hours = configure_environment(args.mode, settings, config, out)
    save(out / "worker_configuration.json", dict(
        execution_mode=args.mode, job_id=job["id"], case_id=job["case"].get("id"),
        model_key=job["model_key"], config=config, settings=settings,
        effective_hours=hours, output=str(out), runtime=job["runtime"], pack=job["pack"],
        policy=settings["policy"], planning_mode="brownfield",
        network_blocked=args.mode == "preflight", optimization_blocked=args.mode == "preflight",
        api_credentials_removed=args.mode != "agent", inherited_grid_options_cleared=True,
        source_sha256=record["source_sha256"],
    ))
    sim, reporting, receipts = prepare_runtime(record, hours, args.mode)
    if args.mode == "preflight":
        verify_record(record)
        receipt = dict(status="passed", completed_at=now(), job_id=job["id"],
                       model_key=job["model_key"], case=job["case"], hours_loaded=hours,
                       pack=job["pack"], runtime=job["runtime"],
                       annual_cap_Mt=sim.SCENARIO["assumptions"]["co2_limit_Mt"],
                       model_api_calls=0, annual_solver_runs=0, solver_executed=False,
                       solver_license_test="not_run", runtime_versions=package_versions(),
                       input_sha256=record.get("input_sha256", job.get("input_sha256")),
                       source_sha256=record["source_sha256"], **receipts)
        save(out / "preflight.json", receipt)
        print(json.dumps(dict(status="passed", job_id=job["id"], receipt=str(out / "preflight.json")),
                         ensure_ascii=False), flush=True)
        return 0
    for name in ("climate_adapter", "co2_adapter", "co2_acceptance_gate", "provider_contract"):
        save(out / (name + ".json"), receipts[name])
    if args.mode == "agent":
        final = run_agent(record, out)
        require(final["scenario_hash"] == sim.SCENARIO_HASH,
                "Agent scenario identity differs from the validated CO2 scenario")
        # Re-assess selected evidence with the same fixed cap gate used during
        # accepted-incumbent selection, rather than trusting a saved boolean.
        final["acceptance"] = reporting.assess(final.get("final_results"),
                                               final.get("final_distribution_check"), sim.SCENARIO)
    else:
        design = dict(sim.DEFAULT_DESIGN)
        errors = sim.validate_changes(design)
        require(not errors, "; ".join(errors))
        result = sim.run_simulation(design)
        solved = result.get("status") in ("optimal", "feasible_time_limit")
        dist = sim.run_distribution_check(design) if solved else None
        final = dict(agent="none", scenario_hash=sim.SCENARIO_HASH, final_design=design,
                     final_results=result, final_distribution_check=dist,
                     acceptance=reporting.assess(result, dist, sim.SCENARIO),
                     implementation=reporting.implementation_info(), model_api_calls=0)
        if solved:
            sim.export_solution(out / "dispatch")
    verify_record(record)
    final.update(region=job["case"]["region"], year=job["case"]["year"],
                 mode="brownfield", experiment="co2_constraint", execution_mode=args.mode,
                 hours=hours, start_hour=0, physical_runtime=job["runtime"],
                 co2_case=job["case"], co2_constraint=receipts["co2_adapter"],
                 climate_adapter=receipts["climate_adapter"],
                 co2_acceptance_gate=receipts["co2_acceptance_gate"],
                 provider_contract=receipts["provider_contract"],
                 electricity_policy=sim.SCENARIO["electricity_policy"],
                 paper_run_settings=dict(**settings, **config),
                 input_sha256=record.get("input_sha256", job.get("input_sha256")),
                 source_sha256=record["source_sha256"], runtime_versions=package_versions(),
                 completed_at=now())
    if sim.SCENARIO.get("prediction"):
        final["prediction"] = sim.SCENARIO["prediction"]
    save(out / "final.json", final)
    reporting.write_report(out, final)
    with (out / "report.md").open("a") as stream:
        stream.write("\n## Fixed CO2 constraint\n\n" + json.dumps(job["case"], ensure_ascii=False, indent=2)
                     + "\n\n" + json.dumps(final["acceptance"].get("co2_budget", {}), indent=2) + "\n")
    print(json.dumps(dict(status="completed", job_id=job["id"], output=str(out),
                          solver=(final.get("final_results") or {}).get("status"),
                          accepted=final["acceptance"]["passes"]), ensure_ascii=False), flush=True)
    # Physical rejection is a completed experimental outcome, not an API or
    # worker failure; the controller can continue to the next selected case.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
