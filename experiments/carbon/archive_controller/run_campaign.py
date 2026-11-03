#!/usr/bin/env python3
"""Run selected, fixed official-source CO2 cases without changing the paper runs.

Default invocation writes a plan. --preflight is offline; --simulate runs the
default design without an LLM; --execute runs the selected frozen agent engine.
"""
from __future__ import annotations

import argparse
import datetime
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import uuid

sys.dont_write_bytecode = True
BASE = Path(__file__).resolve().parent
PROJECT = BASE.parent
MODEL_DIRS = {"sol": "gpt_6_1_sol", "opus": "claude_opus_5_5"}
TOKEN_ENV = "GRID_CO2_WORKER_TOKEN"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    tmp.replace(path)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def cases_by_id():
    from co2_adapter import SELECTED_CASES
    cases = json.loads((BASE / "cases.json").read_text())["cases"]
    require(len(cases) == 10 and {(c["region"], c["year"]) for c in cases} == SELECTED_CASES,
            "Manifest must contain exactly the ten selected region/year cases")
    result = {c["id"]: c for c in cases}
    require(len(result) == 10 and all(c["id"] == f'{c["year"]}/{c["region"]}' for c in cases),
            "Duplicate or inconsistent case identifiers")
    return result


def model_config(key):
    model_dir = PROJECT / "weather_prediction" / MODEL_DIRS[key]
    config = json.loads((model_dir / "model.json").read_text())
    study = json.loads((PROJECT / "weather_prediction/study_config.json").read_text())
    expected = study["campaigns"][MODEL_DIRS[key]]
    require(config == expected["model"], "Model differs from the frozen study configuration")
    return config, expected["settings"]


def routes(case, model_key):
    year, region = case["year"], case["region"]
    pack = PROJECT / (f"experiment/{region}" if year == 2023 else
                      f"weather_prediction/inputs/{year}/{region}")
    model_dir = PROJECT / "weather_prediction" / MODEL_DIRS[model_key]
    # The weather deployment preserves the archived physics and supplies the
    # common 64k provider configuration, including when loading a 2023 pack.
    variant = "runtime_calendar" if (region, year) == ("south_africa", 2030) else "runtime_original"
    runtime = model_dir / variant / "experiment"
    require(case["mapping"]["base_pack"] == str(pack.relative_to(PROJECT)),
            "Manifest input provenance differs from the actual pack")
    require(pack.is_dir() and runtime.is_dir(), "Missing frozen input pack or physical runtime")
    return pack.resolve(), runtime.resolve()


def contained(root, name):
    path = (root / name).resolve()
    require(path.is_relative_to(root.resolve()), "Input path escapes its pack")
    return path


def input_hashes(pack):
    pack = Path(pack).resolve()
    network = json.loads((pack / "network.json").read_text())
    names = {"config.json", "network.json", "inventory.json", "assumptions.json"}
    names.update(node["profiles_csv"] for node in network["nodes"])
    manifest_path = pack / "prediction_manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        for name, digest in manifest["files"].items():
            require(sha(contained(pack, name)) == digest, f"Forecast input changed: {name}")
        for name, digest in manifest["fixed_inputs"].items():
            require(sha(contained(PROJECT / "experiment", name)) == digest,
                    f"Forecast reference changed: {name}")
        names.update(manifest["files"])
        names.add("prediction_manifest.json")
    return {name: sha(contained(pack, name)) for name in sorted(names)}


def source_hashes(model_key, runtime):
    runtime = Path(runtime)
    model_dir = PROJECT / "weather_prediction" / MODEL_DIRS[model_key]
    files = {BASE / name for name in ("run_campaign.py", "worker.py", "co2_adapter.py", "cases.json")}
    files.update(runtime.rglob("*.py"))
    files.update((runtime / "shared").rglob("*.json"))
    files.update((model_dir / "adapters").rglob("*.py"))
    # Some frozen provider versions import adapters relative to their own runtime.
    files.update((runtime.parents[1] / "adapters").rglob("*.py"))
    files.update({runtime / "requirements.lock.txt", model_dir / "model.json",
                  model_dir / "climate_adapter.py", PROJECT / "weather_prediction/study_config.json",
                  PROJECT / "weather_prediction/credentials.py"})
    files.update((PROJECT / "nasa_climate/scripts").glob("*.py"))
    for name in ("opus_protocol.py", "runtime_origin.json"):
        if (model_dir / name).exists():
            files.add(model_dir / name)
    return {str(path.resolve()): sha(path) for path in sorted(files)}


def validate_job(job, config, settings, mode):
    require(mode in ("preflight", "simulate", "agent"), "Unknown execution mode")
    case = job["case"]
    require(case == cases_by_id().get(case["id"]), "Job changed its fixed case or official source")
    key = job["model_key"]
    require(key in MODEL_DIRS, "Unselected model")
    expected_config, expected_settings = model_config(key)
    require(config == expected_config and settings == expected_settings,
            "Job differs from the frozen model or annual experiment settings")
    pack, runtime = routes(case, key)
    require(Path(job["pack"]) == pack and Path(job["runtime"]) == runtime, "Unapproved input/runtime route")
    require(job["agent"] in ("single", "multi"), "Unknown agent architecture")
    require(job["id"] == f'{key}/{case["id"]}/{job["agent"]}', "Inconsistent job identity")


def verify_record(record):
    """Recheck identity, all consumed inputs/sources, and live controller ownership."""
    job = record["job"]
    validate_job(job, record["config"], record["settings"], record["execution_mode"])
    output = Path(job["output"]).resolve()
    require(output.is_relative_to(BASE) and output != BASE, "Output must remain inside co2_constraint")
    require(input_hashes(Path(job["pack"])) == record["input_sha256"], "Input files changed")
    require(source_hashes(job["model_key"], job["runtime"]) == record["source_sha256"], "Source files changed")
    require(os.getppid() == record["controller_pid"]
            and os.environ.get(TOKEN_ENV) == record["controller_token"],
            "Worker must be started by its live CO2 controller")
    campaign_path = Path(record["controller_campaign"]).resolve()
    require(campaign_path.is_relative_to(BASE) and output.is_relative_to(campaign_path.parent),
            "Output does not belong to this campaign")
    state = json.loads(campaign_path.read_text())
    require(state["status"] == "running" and state["pid"] == record["controller_pid"]
            and state["execution_mode"] == record["execution_mode"], "Controller ownership changed")
    candidates = [j for j in state["jobs"] if j["id"] == job["id"]]
    require(len(candidates) == 1, "Job does not belong to its controller")
    current = candidates[0]
    for key in ("id", "case", "model_key", "agent", "pack", "runtime", "output"):
        require(current.get(key) == job.get(key), f"Controller job differs: {key}")
    require(current["status"] == "running", "Controller job is not running")
    for key in ("config", "settings", "source_sha256", "input_sha256"):
        require(current[key] == record[key], f"Controller record differs: {key}")


def make_plan(models, agents, selected, mode):
    catalog = cases_by_id()
    selected = selected or list(catalog)
    require(len(set(selected)) == len(selected) and set(selected) <= set(catalog), "Unknown or duplicate cases")
    require(len(set(models)) == len(models) and len(set(agents)) == len(agents), "Duplicate model or agent")
    jobs = []
    # SA is more expensive; retain its existing convention of running last.
    ordered = sorted(selected, key=lambda key: catalog[key]["region"] == "south_africa")
    for model in models:
        config, settings = model_config(model)
        for case_id in ordered:
            case = catalog[case_id]
            pack, runtime = routes(case, model)
            inputs, sources = input_hashes(pack), source_hashes(model, runtime)
            for agent in agents if mode == "agent" else ["single"]:
                jobs.append(dict(id=f"{model}/{case_id}/{agent}", case=case, model_key=model, agent=agent,
                                 pack=str(pack), runtime=str(runtime), config=config, settings=settings,
                                 input_sha256=inputs, source_sha256=sources, status="pending", attempts=[]))
    return dict(schema_version=1, created_at=now(), execution_mode=mode, status="planned",
                selection=dict(models=models, agents=agents, cases=selected), jobs=jobs)


def assert_unchanged(state):
    require(state["execution_mode"] in ("preflight", "simulate", "agent"), "Unknown execution mode")
    planned = make_plan(
        state["selection"]["models"], state["selection"]["agents"], state["selection"]["cases"], state["execution_mode"])
    require(len(planned["jobs"]) == len(state["jobs"]), "Campaign selection changed")
    for expected, actual in zip(planned["jobs"], state["jobs"]):
        for key in ("id", "case", "model_key", "agent", "pack", "runtime", "config", "settings",
                    "source_sha256", "input_sha256"):
            require(expected[key] == actual[key], f"Campaign source/input/selection changed: {actual['id']} {key}")


def terminate_group(child):
    if child is None:
        return
    try:
        os.killpg(child.pid, signal.SIGTERM)
        child.wait(timeout=8)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    child.wait()


def execute(state, campaign, retry_failed=False):
    # Coordinate with the existing campaigns; do not compete for their 18 cores.
    with (PROJECT / "gpt_sol/.paper_campaign.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another grid campaign is already using the solver")
        assert_unchanged(state)
        state.update(status="running", pid=os.getpid(), started_at=now())
        save(campaign / "campaign.json", state)
        child, current, attempt = None, None, None

        def stop(signum, frame):
            raise KeyboardInterrupt

        handlers = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            for job in state["jobs"]:
                if job["status"] == "completed":
                    continue
                attempt = None
                require(job["status"] == "pending" or retry_failed,
                        f"{job['id']} needs --retry-failed for a fresh attempt")
                current = job
                validate_job(job, job["config"], job["settings"], state["execution_mode"])
                require(source_hashes(job["model_key"], job["runtime"]) == job["source_sha256"], "Sources changed")
                require(input_hashes(Path(job["pack"])) == job["input_sha256"], "Inputs changed")
                out = campaign / job["id"] / f"attempt_{len(job['attempts']) + 1:03d}"
                out.mkdir(parents=True, exist_ok=False)
                attempt = dict(status="running", output=str(out), started_at=now())
                job["attempts"].append(attempt)
                job.update(status="running", output=str(out))
                env = os.environ.copy()
                if state["execution_mode"] == "agent":
                    spec = importlib.util.spec_from_file_location("_co2_credentials", PROJECT / "weather_prediction/credentials.py")
                    loader = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(loader)
                    receipt = loader.configure_credentials(job["config"]["provider"])
                    job["credential_source"] = {k: receipt[k] for k in ("source", "provider")}
                    env = os.environ.copy()
                token = uuid.uuid4().hex
                env.update({TOKEN_ENV: token, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"})
                record = dict(job=job, config=job["config"], settings=job["settings"],
                              input_sha256=job["input_sha256"], source_sha256=job["source_sha256"],
                              execution_mode=state["execution_mode"], controller_pid=os.getpid(),
                              controller_token=token, controller_campaign=str(campaign / "campaign.json"))
                save(out / "job.json", record)
                save(campaign / "campaign.json", state)
                print(now(), job["id"], "START", flush=True)
                with (out / "execution.log").open("w") as log:
                    child = subprocess.Popen([sys.executable, str(BASE / "worker.py"), "--job", str(out / "job.json"),
                                              "--mode", state["execution_mode"]], cwd=out, env=env,
                                             stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                    attempt["pid"] = child.pid
                    save(campaign / "campaign.json", state)
                    code = child.wait()
                child = None
                attempt["exit_code"] = code
                require(code == 0, f"Worker exited {code}; inspect {out / 'execution.log'}")
                result_file = out / ("preflight.json" if state["execution_mode"] == "preflight" else "final.json")
                result = json.loads(result_file.read_text())
                if state["execution_mode"] == "preflight":
                    require(result["status"] == "passed", "Preflight did not pass")
                    summary = dict(annual_cap_Mt=result["annual_cap_Mt"], hours_loaded=result["hours_loaded"])
                else:
                    solved = result.get("final_results") or {}
                    summary = dict(accepted=result["acceptance"]["passes"], solver_status=solved.get("status"),
                                   co2_budget=result["acceptance"].get("co2_budget"),
                                   api_usage_summary=result.get("api_usage_summary"))
                attempt.update(status="completed", completed_at=now(), exit_code=code, summary=summary)
                job.update(status="completed", summary=summary)
                save(campaign / "campaign.json", state)
                print(now(), job["id"], "COMPLETED", flush=True)
            state.update(status="completed", completed_at=now())
        except BaseException as exc:
            terminate_group(child)
            status = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
            if current is not None:
                current["status"] = status
            if attempt is not None:
                attempt.update(status=status, completed_at=now())
            state.update(status=status, error_type=type(exc).__name__, error=str(exc), stopped_at=now())
            raise
        finally:
            save(campaign / "campaign.json", state)
            for sig, handler in handlers.items():
                signal.signal(sig, handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", nargs="+", choices=MODEL_DIRS, default=["sol"])
    parser.add_argument("--agents", nargs="+", choices=("single", "multi"), default=["single"])
    parser.add_argument("--cases", nargs="+")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--preflight", action="store_true")
    action.add_argument("--simulate", action="store_true")
    action.add_argument("--execute", action="store_true")
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args()
    mode = "preflight" if args.preflight else "simulate" if args.simulate else "agent"
    do_execute = args.preflight or args.simulate or args.execute
    if args.resume:
        campaign = args.resume.resolve()
        require(campaign.is_relative_to(BASE), "Resume directory must be inside co2_constraint")
        state = json.loads((campaign / "campaign.json").read_text())
        require(not do_execute or mode == state["execution_mode"], "Resume action differs from the saved mode")
        assert_unchanged(state)
    else:
        state = make_plan(args.models, args.agents, args.cases, mode)
        name = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
        folder = "preflight" if mode == "preflight" else "simulations" if mode == "simulate" else "runs" if do_execute else "plans"
        campaign = BASE / folder / name
        campaign.mkdir(parents=True)
        save(campaign / "campaign.json", state)
    print(json.dumps(dict(campaign=str(campaign), mode=state["execution_mode"], jobs=len(state["jobs"]),
                          executing=bool(do_execute), selection=state["selection"]), ensure_ascii=False), flush=True)
    if do_execute:
        execute(state, campaign, args.retry_failed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
