#!/usr/bin/env python3
"""Run a new case with preserved model engines and user-provided input packs.

The default prints a plan. --check-inputs uses only the standard library;
--execute is required to import the scientific stack and call a model/solver.
This launcher creates fresh runs. tools/run_campaign.py preserves earlier attempts
and retries failed cases in fresh directories; historical replay remains pinned.
"""
import argparse
import contextlib
import csv
import datetime as dt
import hashlib
import importlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import sys
import time

REGIONS = ("miso", "ercot", "nyiso", "australia", "new_zealand", "mexico", "brazil", "south_africa")
MODELS = {"sol": "gpt-6.1-sol", "opus": "claude-opus-5-5"}
CLIMATE_MODEL = "MPI-ESM1-2-HR"
SOURCE_KIND = "synthetic_hourly_nasa_daily_with_era5_template"


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--deployment", choices=MODELS, required=True)
    p.add_argument("--region", choices=REGIONS, required=True)
    p.add_argument("--year", type=int, choices=(2023, 2030, 2035), required=True)
    p.add_argument("--agent", choices=("single", "multi"), required=True)
    p.add_argument("--pack", type=Path, required=True, help="Prepared regional pack; datasets are external")
    p.add_argument("--shared", type=Path, required=True, help="External directory named shared containing cost/policy JSON")
    p.add_argument("--output", type=Path, required=True, help="New or empty output directory")
    p.add_argument("--data-root", type=Path, help="External provenance root; default GRID_DATA_ROOT or repository/data")
    p.add_argument("--co2", action="store_true", help="Use the fixed published carbon case and 64k hosted runtime")
    p.add_argument("--solver", choices=("gurobi", "highs", "auto"), default="gurobi")
    p.add_argument("--threads", type=int, default=18)
    p.add_argument("--check-inputs", action="store_true", help="Validate paths, JSON and full-year CSVs without API/solver")
    p.add_argument("--execute", action="store_true", help="Run inference and optimization; cloud inference may incur charges")
    return p


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    tmp.replace(path)



def make_plan(args, root=None):
    root = Path(root or Path(__file__).resolve().parents[1]).resolve()
    if args.threads < 1:
        raise ValueError("--threads must be positive")
    profile = ("runtime_baseline2023" if args.deployment == "opus" and args.year == 2023 and not args.co2 else
               "runtime_calendar" if (args.region, args.year) == ("south_africa", 2030) else "runtime_original")
    shared = args.shared.resolve()
    if shared.name != "shared":
        raise ValueError("--shared must point to a directory named shared, as expected by the preserved engine")
    data_root = Path(args.data_root or os.environ.get("GRID_DATA_ROOT", root / "data")).resolve()
    plan = dict(schema_version=2, deployment=args.deployment, model=MODELS[args.deployment],
                provider="anthropic" if args.deployment == "opus" else "openai",
                region=args.region, year=args.year, agent=args.agent, profile=profile,
                repository=str(root), data_root=str(data_root), adapters=str(root / "models" / args.deployment / "adapters"),
                weather_year=args.year, weather_template_year=2023, time_axis_year=2023,
                climate_model=CLIMATE_MODEL if args.year != 2023 else None,
                physics_profile="corrected_sa2030" if (args.region, args.year) == ("south_africa", 2030) else "original",
                engine=str(root / "models" / args.deployment / profile / "engine"),
                pack=str(args.pack.resolve()), shared=str(shared), output=str(args.output.resolve()),
                settings=dict(hours=8760, start_hour=0, planning_mode="brownfield", policy="baseline",
                              solver=args.solver, solver_seconds=86400, threads=args.threads,
                              max_cycles=120, max_turns=120, max_runs=40))
    plan.update(effort="high", service_tier="standard_only" if args.deployment == "opus" else "default",
                output_tokens=16000 if profile == "runtime_baseline2023" else 64000,
                api_key_env="ANTHROPIC_API_KEY" if args.deployment == "opus" else "OPENAI_API_KEY")
    if args.co2:
        catalog = read_json(root / "experiments/carbon/cases.json")["cases"]
        matches = [c for c in catalog if (c["region"], c["year"]) == (args.region, args.year)]
        if len(matches) != 1:
            raise ValueError("CO2 experiment requires one of the ten published system/year cases")
        plan["co2_case"] = matches[0]
    return plan


def contained_file(folder, name):
    if not isinstance(name, str) or not name or Path(name).is_absolute():
        raise ValueError("Input references must be nonempty relative paths")
    path = (folder / name).resolve()
    if not path.is_relative_to(folder.resolve()) or not path.is_file():
        raise ValueError("Missing input file or input reference outside its directory")
    return path


def check_inputs(plan):
    """Structural validation and SHA-256 inventory, without scientific imports."""
    pack, shared, engine = (Path(plan[key]) for key in ("pack", "shared", "engine"))
    if not (engine / (plan["agent"] + "_agent.py")).is_file():
        raise ValueError("Selected engine is missing")
    files = {name: contained_file(pack, name) for name in ("config.json", "network.json", "inventory.json", "assumptions.json")}
    values = {name: read_json(path) for name, path in files.items()}
    if any(not isinstance(value, dict) for value in values.values()):
        raise ValueError("Pack JSON inputs must be objects")
    cfg, network = values["config.json"], values["network.json"]
    if cfg.get("id") != plan["region"]:
        raise ValueError("Pack config id does not match --region")
    costs = contained_file(shared, cfg.get("cost_file"))
    policies = contained_file(shared, "electricity_policies.json")
    if not isinstance(read_json(costs), dict) or not isinstance(read_json(policies), dict):
        raise ValueError("Shared costs/policies must be JSON objects")
    nodes = network.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("Network must contain a nonempty nodes list")
    ids, reference_times = set(), None
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("id"), str) or node["id"] in ids:
            raise ValueError("Network nodes require unique string ids")
        ids.add(node["id"])
        name = node.get("profiles_csv")
        path = contained_file(pack, name)
        files[name] = path
        times = []
        with path.open(newline="") as stream:
            reader = csv.DictReader(stream)
            if set(reader.fieldnames or []) != {"time", "demand_MW", "solar_cf", "wind_cf"}:
                raise ValueError("Profiles require exactly time,demand_MW,solar_cf,wind_cf columns")
            for row in reader:
                stamp = dt.datetime.fromisoformat(row["time"].replace("Z", "+00:00"))
                times.append(stamp)
                for column in ("demand_MW", "solar_cf", "wind_cf"):
                    value = float(row[column])
                    if not math.isfinite(value) or value < 0 or (column.endswith("_cf") and value > 1):
                        raise ValueError("Profile contains invalid demand or capacity factors")
        if len(times) != 8760 or times[0].year != 2023 or times[-1].year != 2023:
            raise ValueError("Profiles must contain 8760 rows on the retained 2023 time axis")
        if any(b - a != dt.timedelta(hours=1) for a, b in zip(times, times[1:])):
            raise ValueError("Profiles must be contiguous hourly records")
        if reference_times is not None and times != reference_times:
            raise ValueError("Node profile timestamps do not align")
        reference_times = times
    prediction, climate_validation = None, None
    manifest_path = pack / "prediction_manifest.json"
    if plan["year"] != 2023:
        manifest_path = contained_file(pack, "prediction_manifest.json")
        manifest = read_json(manifest_path)
        prediction = manifest.get("scenario")
        if not isinstance(prediction, dict) or prediction != cfg.get("prediction"):
            raise ValueError("Future config prediction must match the manifest scenario")
        expected = {"region": plan["region"], "planning_year": plan["year"], "weather_year": plan["year"],
                    "weather_template_year": 2023, "time_axis_year": 2023}
        if any(prediction.get(key) != value for key, value in expected.items()):
            raise ValueError("Prediction region/planning/weather/template/time-axis years do not match the case")
        climate = prediction.get("climate")
        expected_climate = dict(source_kind=SOURCE_KIND, model=CLIMATE_MODEL, scenario="ssp245",
                                projection_year=plan["year"], weather_year=plan["year"],
                                weather_template_year=2023, time_axis_year=2023)
        if not isinstance(climate, dict) or any(climate.get(k) != v for k, v in expected_climate.items()):
            raise ValueError("Future case requires the paper NASA climate scenario")
        files["prediction_manifest.json"] = manifest_path
        for name, digest in manifest.get("files", {}).items():
            path = contained_file(pack, name)
            if sha(path) != digest:
                raise ValueError("Prediction manifest file checksum mismatch")
            files[name] = path
        climate_path = contained_file(pack, "climate_manifest.json")
        climate_manifest = read_json(climate_path)
        if manifest.get("climate_manifest") != "climate_manifest.json":
            raise ValueError("Prediction climate manifest reference differs")
        if manifest.get("files", {}).get("climate_manifest.json") != sha(climate_path):
            raise ValueError("Prediction climate manifest checksum differs")
        for key, value in {"region": plan["region"], "projection_year": plan["year"], "model": CLIMATE_MODEL,
                           "scenario": "ssp245", "source_kind": SOURCE_KIND}.items():
            if climate_manifest.get(key) != value:
                raise ValueError("Climate manifest identity differs from case")
        for name, digest in climate_manifest.get("files", {}).items():
            path = contained_file(pack, name)
            if sha(path) != digest:
                raise ValueError("Climate manifest file checksum mismatch")
            files[name] = path
        files["climate_manifest.json"] = climate_path
        # Full validator pins inherited demand/source prediction manifest, NASA
        # profile/provenance hashes and physical runtime; it imports stdlib only.
        validator = load_source("release_nasa_validator", Path(plan["repository"]) / "preprocessing/nasa_climate/build_packs.py")
        climate_validation = validator.validate_pack(pack, data_root=plan["data_root"])
    elif cfg.get("prediction") is not None or manifest_path.exists() or (pack / "climate_manifest.json").exists():
        raise ValueError("2023 baseline requires its original ERA5 pack without future manifests")
    correction = pack / "correction_manifest.json"
    if correction.exists():
        files["correction_manifest.json"] = correction
        for name, digest in read_json(correction).get("corrected_input_sha256", {}).items():
            path = contained_file(pack, name)
            if sha(path) != digest:
                raise ValueError("Correction manifest file checksum mismatch")
            files[name] = path
    input_hashes = {"pack/" + name: sha(path) for name, path in sorted(files.items())}
    input_hashes.update({"shared/" + str(p.relative_to(shared)): sha(p) for p in (costs, policies)})
    if plan.get("co2_case"):
        catalog = read_json(Path(plan["repository"]) / "experiments/carbon/cases.json")["cases"]
        if plan["co2_case"] not in catalog or (plan["co2_case"]["region"], plan["co2_case"]["year"]) != (plan["region"], plan["year"]):
            raise ValueError("CO2 case differs from the fixed published catalogue")
    source_paths = list(engine.glob("*.py"))
    # reporting.implementation_info() hashes these alongside engine sources.
    source_paths += [contained_file(engine.parent, name) for name in ("run.py", "requirements.lock.txt")]
    model_root = engine.parents[1]
    source_paths += list((model_root / "adapters").glob("*.py"))
    source_paths += [contained_file(model_root, name) for name in ("climate_adapter.py", "runtime_origin.json", "model.json")]
    if plan["deployment"] == "opus":
        source_paths.append(contained_file(model_root, "opus_protocol.py"))
    if prediction:
        source_paths += list((Path(plan["repository"]) / "preprocessing/nasa_climate").glob("*.py"))
    root = engine.parents[3]
    sources = {str(path.relative_to(root)): sha(path) for path in sorted(source_paths)}
    sources["tools/run_case.py"] = sha(Path(__file__))
    if plan.get("co2_case"):
        for name in ("co2_adapter.py", "cases.json"):
            rel = "experiments/carbon/" + name
            sources[rel] = sha(Path(plan["repository"]) / rel)
    return dict(input_sha256=input_hashes, source_sha256=sources, prediction=prediction,
                climate_validation=climate_validation,
                validation_scope="Structural annual inputs and all future NASA/source-pack/runtime provenance hashes; scientific acceptance requires execution")


def environment(plan):
    env = os.environ.copy()
    for key in ("OPENAI_BASE_URL", "ANTHROPIC_BASE_URL", "GRID_RESUME", "GRID_RECOVERY_AUDIT", "GRID_RESUME_MAX_CYCLES", "GRID_SOLVER_EXTRA", "GRID_COMPARE_LIMITS", "GRID_LOCAL_OUTPUT_TOKENS", "GRID_LOCAL_BACKEND", "GRID_LOCAL_BASE_URL", "GRID_CORRECTION_WORKER_TOKEN", "GRID_COMPARISON_RELEASE"):
        env.pop(key, None)
    out, settings = Path(plan["output"]), plan["settings"]
    env.update(GRID_DATA_ROOT=plan["data_root"], GRID_EXPERIMENT_HOURS="8760", GRID_EXPERIMENT_START="0", GRID_SOLVER=settings["solver"],
               GRID_SOLVER_THREADS=str(settings["threads"]), GRID_SOLVER_TIME_LIMIT="86400",
               GRID_RUN_OUTPUT=str(out), GRID_SOLVER_LOGDIR=str(out), GRID_REASONING_EFFORT="high",
               GRID_ANTHROPIC_CACHE_TTL="5m", GRID_API_USAGE_LOG=str(out / "api_usage.jsonl"),
               GRID_API_RESPONSE_LOG=str(out / "api_responses.jsonl"))
    return env


def safe_error(exc):
    """Never serialize provider response bodies, exception messages, or secrets."""
    result = {"type": type(exc).__name__}
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        result["http_status"] = status
    request_id = getattr(exc, "request_id", None)
    if isinstance(request_id, str) and re.fullmatch(r"(?:req_|req-|request_)[A-Za-z0-9_-]{1,150}", request_id):
        result["request_id"] = request_id
    return result


def load_source(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def invoke_engine(plan, inventory):
    """Called once in a fresh CLI process after explicit --execute."""
    out = Path(plan["output"])
    sys.path.insert(0, plan["adapters"])
    sys.path.insert(0, plan["engine"])
    import scenario_core
    import policy
    scenario_core.ROOT = Path(plan["shared"]).parent
    policy.CATALOG = Path(plan["shared"]) / "electricity_policies.json"
    adapter = load_source("release_climate_adapter", Path(plan["engine"]).parents[1] / "climate_adapter.py")
    receipt = adapter.install_climate_adapter(scenario_core, Path(plan["pack"]),
                expected_region=plan["region"], expected_year=plan["year"])
    save(out / "climate_adapter.json", receipt)
    co2_receipt = gate_receipt = None
    if plan.get("co2_case"):
        import reporting
        carbon = load_source("release_co2_adapter", Path(plan["repository"]) / "experiments/carbon/co2_adapter.py")
        co2_receipt = carbon.install(scenario_core, plan["co2_case"], Path(plan["pack"]))
        gate_receipt = carbon.install_acceptance_gate(reporting, plan["co2_case"], 8760)
        save(out / "co2_adapter.json", co2_receipt)
        save(out / "co2_acceptance_gate.json", gate_receipt)
    module = importlib.import_module(plan["agent"] + "_agent")
    settings = plan["settings"]
    sys.argv = [plan["agent"] + "_agent", "--scenario", plan["pack"], "--planning-mode", "brownfield",
                "--policy", "baseline", "--provider", plan["provider"], "--model", plan["model"]]
    sys.argv += (["--max-cycles", str(settings["max_cycles"])] if plan["agent"] == "multi" else
                 ["--max-runs", str(settings["max_runs"]), "--max-turns", str(settings["max_turns"])])
    module.main()
    final = read_json(out / "final.json")
    final.update(region=plan["region"], year=plan["year"], agent=plan["agent"], mode="brownfield",
                 hours=8760, start_hour=0, release_run_config="run_config.json",
                 physics_profile=plan["physics_profile"], climate_model=plan["climate_model"],
                 climate_adapter=receipt, paper_run_settings=dict(**settings, model=plan["model"],
                 provider=plan["provider"], effort=plan["effort"], service_tier=plan["service_tier"],
                 output_tokens=plan["output_tokens"]),
                 input_sha256=inventory["input_sha256"], source_sha256=inventory["source_sha256"])
    if co2_receipt is not None:
        final.update(experiment="co2_constraint", co2_case=plan["co2_case"],
                     co2_constraint=co2_receipt, co2_acceptance_gate=gate_receipt)
    if inventory["prediction"]:
        final["prediction"] = inventory["prediction"]
        final["distribution_scope"] = ("Representative feeders are normalized to each scenario zonal peak. "
            "Demand growth changes the implied feeder count; this does not test growth on a fixed 2023 "
            "distribution fleet or account for new-feeder construction costs.")
    if plan["deployment"] == "opus":
        protocol = load_source("release_opus_protocol", Path(plan["engine"]).parents[1] / "opus_protocol.py")
        final["thinking_protocol"] = protocol.validate_journals(out)
    from api_usage import summarize_ledger
    from collections import Counter
    final["api_usage_summary"] = summarize_ledger(out / "api_usage.jsonl")
    journal = [json.loads(line) for line in (out / "api_responses.jsonl").read_text().splitlines() if line.strip()]
    ledger = [json.loads(line) for line in (out / "api_usage.jsonl").read_text().splitlines() if line.strip()]
    if Counter(row["response_id"] for row in ledger) - Counter(row["response_id"] for row in journal):
        raise ValueError("API response journal does not cover usage ledger")
    final["api_response_journal"] = dict(recorded_responses=len(journal), accounted_responses=len(ledger), ledger_coverage_complete=True)
    metrics = final.setdefault("metrics", {})
    final["agent_reported_usage"] = {key: metrics.get(key) for key in ("api_calls", "input_tokens", "output_tokens")}
    for key in ("api_calls", "input_tokens", "output_tokens"):
        metrics[key] = final["api_usage_summary"].get(key)
    metrics["legacy_estimated_llm_cost_usd"] = metrics.get("estimated_llm_cost_usd")
    metrics["estimated_llm_cost_usd"] = final["api_usage_summary"]["estimated_cost_usd"]
    metrics["billed_api_calls_recorded"] = final["api_usage_summary"]["api_calls"]
    save(out / "final.json", final)
    from reporting import write_report
    write_report(out, final)


def execute_case(plan, inventory, runner=None):
    out = Path(plan["output"])
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ValueError("Refusing to overwrite a nonempty output path; choose a new directory")
    if not os.environ.get(plan["api_key_env"]):
        raise ValueError("Required provider API key environment variable is not set")
    # Validate again before creating outputs; no changes can hide behind the plan.
    current = check_inputs(plan)
    if current != inventory:
        raise ValueError("Code or input files changed after validation")
    out.mkdir(parents=True, exist_ok=True)
    config = dict(plan, **inventory, started_at=dt.datetime.now(dt.timezone.utc).isoformat())
    # Exclusive creation also prevents two processes from claiming an empty directory.
    with (out / "run_config.json").open("x") as stream:
        stream.write(json.dumps(config, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    status = {"status": "running", "run_config_sha256": sha(out / "run_config.json")}
    save(out / "status.json", status)
    previous_env, previous_cwd, previous_argv = os.environ.copy(), Path.cwd(), sys.argv[:]
    run_env = environment(plan)
    started = time.monotonic()
    try:
        os.environ.clear()
        os.environ.update(run_env)
        os.chdir(out)
        with (out / "execution.log").open("w") as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            (runner or invoke_engine)(plan, inventory)
        if not (out / "final.json").is_file():
            raise RuntimeError("Engine returned without final.json")
        final = read_json(out / "final.json")
        status.update(status="completed", accepted=(final.get("acceptance") or {}).get("passes"), exit_code=0)
    except BaseException as exc:
        status.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
                      error=safe_error(exc), exit_code=130 if isinstance(exc, KeyboardInterrupt) else 1)
    finally:
        os.chdir(previous_cwd)
        os.environ.clear()
        os.environ.update(previous_env)
        sys.argv = previous_argv
        status["elapsed_seconds"] = round(time.monotonic() - started, 3)
        save(out / "status.json", status)
    return status


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        plan = make_plan(args)
        if not (args.check_inputs or args.execute):
            print(json.dumps(dict(plan, status="plan_only", execution_requires="--execute"), indent=2))
            return 0
        inventory = check_inputs(plan)
        if not args.execute:
            print(json.dumps(dict(plan, status="inputs_checked", **inventory), indent=2))
            return 0
        status = execute_case(plan, inventory)
        print(json.dumps(dict(output=plan["output"], **status), indent=2))
        return status["exit_code"]
    except (ValueError, OSError, KeyError, TypeError) as exc:
        # Validation errors originate here; provider errors are caught and sanitized above.
        print("Case preparation failed: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
