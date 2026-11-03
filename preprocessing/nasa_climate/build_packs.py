#!/usr/bin/env python3
"""Build/validate separate NASA climate packs, without solver or model API calls.

Existing packs are validated and reused, never overwritten. Examples:
  python preprocessing/nasa_climate/build_packs.py --data-root /path/to/data --plan
  python preprocessing/nasa_climate/build_packs.py --data-root /path/to/data --validate-only --dry-load --years 2030 --regions miso south_africa

Dry loading uses an isolated subprocess, the existing case-specific physical
runtime, and an in-memory climate metadata adapter. Existing source files stay
unchanged. The synthetic profiles retain the 2023 model clock; their climate
projection year is recorded honestly as 2030 or 2035.
"""
from __future__ import annotations
import argparse
import copy
import csv
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta

sys.dont_write_bytecode = True
# Loading a sibling explicitly also supports importlib-based offline validators.
import importlib.util as _importlib_util
_spec = _importlib_util.spec_from_file_location("_grid_nasa_paths", Path(__file__).resolve().with_name("_paths.py"))
_paths = _importlib_util.module_from_spec(_spec)
_spec.loader.exec_module(_paths)
RELEASE_ROOT = _paths.RELEASE_ROOT
PROJECT = _paths.data_root()
CLIMATE = PROJECT / "nasa_climate"

def configure_roots(data_root=None):
    """Set the external research root; no input reads or writes occur."""
    global PROJECT, CLIMATE
    PROJECT = _paths.data_root(data_root)
    CLIMATE = PROJECT / "nasa_climate"
    return PROJECT
MODELS = ("ACCESS-CM2", "MPI-ESM1-2-HR", "MRI-ESM2-0")
YEARS = (2030, 2035)
REGIONS = ("miso", "ercot", "nyiso", "south_africa", "australia", "new_zealand", "mexico", "brazil")
SCENARIO = "ssp245"
SOURCE_KIND = "synthetic_hourly_nasa_daily_with_era5_template"
UNCHANGED = ("inventory.json", "network.json", "demand.csv", "demand_audit.csv")
COLUMNS = ["time", "demand_MW", "solar_cf", "wind_cf"]


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def source_pack(region, year):
    if (region, year) == ("south_africa", 2030):
        return PROJECT / "corrections/20260925-v2/packs/south_africa_2030"
    suffix = "official" if region == "nyiso" else "baseline"
    return PROJECT / "prediction_local/packs" / f"{region}_{year}_{suffix}"


def runtime_path(region, year):
    profile = "runtime_calendar" if (region, year) == ("south_africa", 2030) else "runtime_original"
    return RELEASE_ROOT / "models/sol" / profile


def contained(root, name):
    path = (Path(root) / name).resolve()
    require(path.is_relative_to(Path(root).resolve()), f"Path escapes pack: {name}")
    return path


def runtime_hashes(runtime):
    files = sorted((runtime / "engine").glob("*.py")) + sorted((runtime / "shared").glob("*.json"))
    files += [runtime / "requirements.lock.txt"]
    return {str(p.relative_to(runtime)): sha(p) for p in files}


def check_record(record, role, expected_path=None):
    require(isinstance(record, dict) and "path" in record and "sha256" in record, f"Missing {role} path/hash")
    path = _paths.resolve_reference(record["path"], PROJECT)
    if expected_path is not None:
        require(path == Path(expected_path).resolve(), f"Wrong {role} file: {path}")
    require(path.is_file() and sha(path) == record["sha256"], f"{role} hash mismatch: {path}")
    return _paths.reference(path, PROJECT), record["sha256"]


def profile_rows(path):
    with Path(path).open(newline="") as handle:
        reader = csv.DictReader(handle)
        require(reader.fieldnames == COLUMNS, f"Wrong profile columns: {path}")
        rows = list(reader)
    require(len(rows) == 8760, f"Expected 8760 rows: {path}")
    origin = datetime(2023, 1, 1)
    for index, row in enumerate(rows):
        require(datetime.fromisoformat(row["time"]) == origin + timedelta(hours=index), f"Non-contiguous 2023 clock at row {index}: {path}")
        for key in COLUMNS[1:]:
            value = float(row[key])
            require(math.isfinite(value) and value >= 0, f"Invalid {key}, row {index}: {path}")
            if key.endswith("_cf"):
                require(value <= 1, f"Capacity factor exceeds one, row {index}: {path}")
    return rows


def compare_profile(current, baseline):
    rows, before = profile_rows(current), profile_rows(baseline)
    for index, (a, b) in enumerate(zip(rows, before)):
        require(a["time"] == b["time"], f"Time changed at row {index}: {current}")
        require(math.isclose(float(a["demand_MW"]), float(b["demand_MW"]), rel_tol=1e-12, abs_tol=1e-8), f"Demand changed at row {index}: {current}")
    return {"hours": 8760, "demand_GWh": sum(float(r["demand_MW"]) for r in rows) / 1000,
            "solar_cf_mean": sum(float(r["solar_cf"]) for r in rows) / 8760,
            "wind_cf_mean": sum(float(r["wind_cf"]) for r in rows) / 8760,
            "demand_unchanged": True, "time_axis_year": 2023}


def summaries_match(actual, recorded):
    """Allow floating summation roundoff across Python versions; hashes stay exact."""
    if actual.keys() != recorded.keys():
        return False
    numeric = {"demand_GWh", "solar_cf_mean", "wind_cf_mean"}
    for zone, row in actual.items():
        other = recorded[zone]
        if row.keys() != other.keys():
            return False
        for key, value in row.items():
            if key in numeric:
                if not math.isclose(value, other[key], rel_tol=1e-12, abs_tol=1e-12):
                    return False
            elif value != other[key]:
                return False
    return True


def verify_source(pack):
    manifest = read_json(pack / "prediction_manifest.json")
    for name, digest in manifest["files"].items():
        require(sha(contained(pack, name)) == digest, f"Authoritative source changed: {pack / name}")
    correction = pack / "correction_manifest.json"
    if correction.exists():
        for name, digest in read_json(correction)["corrected_input_sha256"].items():
            require(sha(contained(pack, name)) == digest, f"Corrected source changed: {pack / name}")
    return manifest


def climate_description(model, year):
    return {"source_kind": SOURCE_KIND, "dataset": "NASA NEX-GDDP-CMIP6", "model": model,
            "scenario": SCENARIO, "projection_year": year, "weather_year": year,
            "weather_template_year": 2023, "time_axis_year": 2023,
            "hourly_weather_kind": "NASA daily constrained synthetic hourly weather using ERA5 2023 intraday templates",
            "limitations": ["Projected weather realization, not a forecast of the actual target-year weather.",
                "Hourly chronology is synthetic; NASA supplies daily climate constraints and ERA5 supplies intraday shape.",
                "Future demand and existing fleet/network assumptions are inherited unchanged; no weather-response load model is added."]}


def validate_pack(pack, *, verify_upstream=True, data_root=None):
    if data_root is not None:
        configure_roots(data_root)
    pack = Path(pack).resolve()
    cm, pm, cfg = [read_json(pack / name) for name in ("climate_manifest.json", "prediction_manifest.json", "config.json")]
    require(cm.get("schema_version") == 1 and cm.get("status") == "validated", "Unsupported/unvalidated climate manifest")
    region, year, model = cm["region"], cm["projection_year"], cm["model"]
    require(region in REGIONS and year in YEARS and model in MODELS and cm["scenario"] == SCENARIO, "Unknown climate identity")
    source = source_pack(region, year)
    require(_paths.resolve_reference(cm["source_pack"], PROJECT) == source.resolve(), "Incorrect authoritative source")
    original = verify_source(source)
    metadata = pm["scenario"]
    require(cfg["prediction"] == metadata, "Config and manifest prediction metadata differ")
    for key, value in {"planning_year": year, "weather_year": year, "weather_template_year": 2023, "time_axis_year": 2023, "region": region}.items():
        require(metadata.get(key) == value, f"Incorrect scenario {key}")
    require(metadata["climate"] == climate_description(model, year), "Unexpected climate description")
    require(metadata["forecast"] == original["scenario"]["forecast"], "Demand forecast changed")
    require(metadata["climate_input_sha256"] == identity(cm["climate_identity"]), "Climate identity mismatch")
    require(metadata["fixed_context_sha256"] == identity({"baseline": original["scenario"]["fixed_context_sha256"], "climate": metadata["climate_input_sha256"]}), "Fixed context mismatch")
    require(pm["fixed_inputs"] == original["fixed_inputs"], "Reference input hashes changed")
    actual_files = {str(p.relative_to(pack)) for p in pack.rglob("*") if p.is_file()}
    require(actual_files == set(pm["files"]) | {"prediction_manifest.json"}, "Missing/unexpected pack files")
    require(set(cm["files"]) == set(pm["files"]) - {"climate_manifest.json"}, "Climate payload inventory differs")
    for name, digest in pm["files"].items():
        require(sha(contained(pack, name)) == digest, f"Pack file hash mismatch: {name}")
        if name != "climate_manifest.json":
            require(cm["files"][name] == digest, f"Manifest hash disagreement: {name}")
    for name in UNCHANGED:
        require(sha(pack / name) == sha(source / name), f"Preserved input changed: {name}")
    assumptions = read_json(pack / "assumptions.json")
    require(assumptions.pop("climate_weather") == metadata["climate"], "Climate assumptions differ")
    require(assumptions == read_json(source / "assumptions.json"), "Non-weather assumptions changed")
    require(read_json(pack / "provenance/source_prediction_manifest.json") == original, "Original prediction provenance changed")
    for name, digest in cm["source_pack_sha256"].items():
        require(sha(contained(source, name)) == digest, f"Source pack changed since build: {name}")
    # Historical packs pin the runtime used when their inputs were prepared.
    # New packs pin the released runtime. Verify either record's bytes without
    # rewriting historical provenance to the current execution source hash.
    runtime = _paths.resolve_reference(cm["runtime"]["path"], PROJECT)
    expected_profile = "corrected_sa2030" if (region, year) == ("south_africa", 2030) else "original"
    require(cm["runtime"]["physics_profile"] == expected_profile, "Incorrect physical runtime route")
    require(runtime_hashes(runtime) == cm["runtime"]["files"], "Physical runtime changed since build")
    if verify_upstream:
        for path, digest in cm["upstream_files"].items():
            check_record({"path": path, "sha256": digest}, "upstream")
    network = read_json(pack / "network.json")
    zones = [n["id"] for n in network["nodes"]]
    require(set(zones) == set(cm["climate_identity"]["profiles"]) == set(cm["climate_identity"]["provenance"]), "Climate identity misses a node")
    summaries = {}
    for node in network["nodes"]:
        zone = node["id"]
        profile = contained(pack, node["profiles_csv"])
        require(sha(profile) == cm["climate_identity"]["profiles"][zone], f"Climate profile identity changed: {zone}")
        pp = pack / "provenance/hourly" / f"{zone}.provenance.json"
        provenance = read_json(pp)
        require(sha(pp) == cm["climate_identity"]["provenance"][zone], f"Profile provenance identity changed: {zone}")
        for key, value in {"status": "validated", "source_kind": SOURCE_KIND, "model": model, "scenario": SCENARIO, "projection_year": year, "region": region, "node": zone}.items():
            require(provenance.get(key) == value, f"Incorrect hourly provenance {key}: {zone}")
        require(provenance["profile_file"]["sha256"] == sha(profile), f"Profile differs from hourly provenance: {zone}")
        summaries[zone] = compare_profile(profile, contained(source, node["profiles_csv"]))
    require(summaries_match(summaries, cm["validation"]["nodes"]), "Recorded validation summaries differ")
    return {"pack": str(pack), "model": model, "scenario": SCENARIO, "year": year, "region": region,
            "nodes": len(zones), "hours": 8760, "status": "validated", "climate_input_sha256": metadata["climate_input_sha256"]}


def build_pack(model, year, region):
    output = CLIMATE / "packs" / model / SCENARIO / str(year) / region
    if output.exists():
        return {**validate_pack(output), "action": "validated_existing"}
    source = source_pack(region, year)
    old = verify_source(source)
    network = read_json(source / "network.json")
    hourly_root = CLIMATE / "profiles" / model / SCENARIO / str(year) / region
    profiles, provenance, upstream, summaries = {}, {}, {}, {}
    for node in network["nodes"]:
        zone = node["id"]
        profile, provenance_file = hourly_root / f"{zone}.csv", hourly_root / f"{zone}.provenance.json"
        pr = read_json(provenance_file)
        for key, value in {"status": "validated", "source_kind": SOURCE_KIND, "model": model, "scenario": SCENARIO, "projection_year": year, "region": region, "node": zone}.items():
            require(pr.get(key) == value, f"Incorrect hourly provenance {key}: {provenance_file}")
        summaries[zone] = compare_profile(profile, contained(source, node["profiles_csv"]))
        for role in ("profile_file", "hourly_file", "validation_file"):
            path, digest = check_record(pr[role], role, profile if role == "profile_file" else None)
            upstream[path] = digest
        require(isinstance(pr.get("source_files"), list) and pr["source_files"], "Hourly provenance needs source files")
        for record in pr["source_files"]:
            path, digest = check_record(record, record.get("role", "source"))
            upstream[path] = digest
        upstream[_paths.reference(provenance_file, PROJECT)] = sha(provenance_file)
        profiles[zone], provenance[zone] = sha(profile), sha(provenance_file)
    climate_identity = {"model": model, "scenario": SCENARIO, "projection_year": year, "source_kind": SOURCE_KIND,
                        "profiles": profiles, "provenance": provenance}
    climate_hash = identity(climate_identity)
    metadata = copy.deepcopy(old["scenario"])
    metadata.update(name=f"{region}_{year}_{model}_{SCENARIO}_nasa_climate", weather_year=year,
                    weather_template_year=2023, time_axis_year=2023, climate_input_sha256=climate_hash,
                    climate=climate_description(model, year), source_scenario_name=old["scenario"]["name"],
                    fixed_context_sha256=identity({"baseline": old["scenario"]["fixed_context_sha256"], "climate": climate_hash}))
    metadata["assumptions"] = [a for a in metadata.get("assumptions", []) if "2023 weather and hourly shape retained" not in a]
    metadata["assumptions"].insert(0, "NASA target-year daily climate constrains synthetic hourly weather; ERA5 2023 supplies intraday templates. The 8760-hour demand/model time axis remains 2023; future weather is not an observed or deterministic forecast.")
    cfg = read_json(source / "config.json")
    cfg["prediction"] = metadata
    cfg["description"] += f" Separate NASA NEX-GDDP-CMIP6 {model} {SCENARIO} {year} synthetic hourly weather scenario."
    assumptions = read_json(source / "assumptions.json")
    assumptions["climate_weather"] = metadata["climate"]
    source_names = set(old["files"]) | {"prediction_manifest.json"}
    if (source / "correction_manifest.json").exists():
        source_names.add("correction_manifest.json")
    source_hashes = {name: sha(contained(source, name)) for name in sorted(source_names)}
    runtime = runtime_path(region, year)
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=f".{region}-", dir=output.parent))
    try:
        (temp / "profiles").mkdir()
        (temp / "provenance/hourly").mkdir(parents=True)
        for name in UNCHANGED:
            shutil.copy2(source / name, temp / name)
        for name in ("prediction_manifest.json", "correction_manifest.json", "config.json", "assumptions.json"):
            if (source / name).exists():
                shutil.copy2(source / name, temp / "provenance" / ("source_" + name))
        for node in network["nodes"]:
            zone = node["id"]
            require(node["profiles_csv"] == f"profiles/{zone}.csv", f"Unsupported source profile path: {node['profiles_csv']}")
            shutil.copy2(hourly_root / f"{zone}.csv", temp / node["profiles_csv"])
            pr = read_json(hourly_root / f"{zone}.provenance.json")
            shutil.copy2(hourly_root / f"{zone}.provenance.json", temp / "provenance/hourly" / f"{zone}.provenance.json")
            shutil.copy2(_paths.resolve_reference(pr["validation_file"]["path"], PROJECT), temp / "provenance/hourly" / f"{zone}.validation.json")
        write_json(temp / "config.json", cfg)
        write_json(temp / "assumptions.json", assumptions)
        payload = {str(p.relative_to(temp)): sha(p) for p in sorted(temp.rglob("*")) if p.is_file()}
        cm = {"schema_version": 1, "status": "validated", "model": model, "scenario": SCENARIO,
              "region": region, "projection_year": year, "source_kind": SOURCE_KIND,
              "source_pack": _paths.reference(source, PROJECT), "source_pack_sha256": source_hashes,
              "climate_identity": climate_identity, "upstream_files": dict(sorted(upstream.items())),
              "runtime": {"path": _paths.reference(runtime, PROJECT), "physics_profile": "corrected_sa2030" if (region, year) == ("south_africa", 2030) else "original",
                          "files": runtime_hashes(runtime), "loader": _paths.reference(Path(__file__), PROJECT), "loader_sha256": sha(__file__),
                          "adapter": "In-memory metadata adaptation only; current case-specific physical runtime preserved."},
              "files": payload, "validation": {"nodes": summaries, "solver_executed": False, "model_api_executed": False}}
        write_json(temp / "climate_manifest.json", cm)
        manifest = {"schema_version": 1, "scenario": metadata, "sources": {**old["sources"], **upstream},
                    "fixed_inputs": old["fixed_inputs"], "files": {**payload, "climate_manifest.json": sha(temp / "climate_manifest.json")},
                    "climate_manifest": "climate_manifest.json"}
        write_json(temp / "prediction_manifest.json", manifest)
        validate_pack(temp)
        require(not output.exists(), f"Refusing to overwrite {output}")
        temp.rename(output)
    except BaseException:
        shutil.rmtree(temp)
        raise
    return {**validate_pack(output), "action": "created"}


def dry_load_one(pack):
    """Construct a planning instance in this process; never invoke simulation."""
    pack = Path(pack).resolve()
    validation = validate_pack(pack)
    metadata = read_json(pack / "prediction_manifest.json")["scenario"]
    runtime = runtime_path(validation["region"], validation["year"])
    os.environ["GRID_EXPERIMENT_HOURS"] = "2"
    os.environ["GRID_EXPERIMENT_START"] = "0"
    sys.path.insert(0, str(runtime / "engine"))
    spec = importlib.util.spec_from_file_location("nasa_climate_scenario_core", runtime / "engine/scenario_core.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROOT = PROJECT / "experiment"
    import policy
    policy.CATALOG = PROJECT / "experiment/shared/electricity_policies.json"
    if hasattr(module, "planning_year_for"):
        def climate_planning_year(folder, cfg):
            recorded = read_json(Path(folder) / "prediction_manifest.json")["scenario"]
            require(cfg.get("prediction") == recorded == metadata, "Climate loader metadata mismatch")
            require(recorded["weather_year"] == recorded["planning_year"] and recorded["time_axis_year"] == 2023, "Climate loader year mismatch")
            return recorded["planning_year"]
        module.planning_year_for = climate_planning_year
    base = module.load_pack(str(pack), policy="baseline")
    base.SCENARIO.update(prediction=metadata, weather_year=metadata["weather_year"], weather_template_year=2023, time_axis_year=2023)
    base.SCENARIO_SUMMARY += "\nNASA climate scenario: " + json.dumps(metadata, ensure_ascii=False)
    from planning import with_planning_mode
    sim = with_planning_mode(base, "brownfield", pack / "inventory.json", pack / "network.json")
    require(sim.HOURS == 2 and sim.SCENARIO["prediction"] == metadata, "Loader did not preserve climate identity")
    require(sim.SCENARIO["weather_year"] == metadata["planning_year"], "Wrong runtime weather year")
    return {"pack": str(pack), "status": "dry_load_passed", "hours_loaded": 2, "weather_year": metadata["weather_year"],
            "time_axis_year": 2023, "runtime": str(runtime), "scenario_hash": sim.SCENARIO_HASH,
            "solver_executed": False, "model_api_executed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--models", nargs="+", choices=MODELS, default=["MPI-ESM1-2-HR"])
    parser.add_argument("--years", nargs="+", type=int, choices=YEARS, default=list(YEARS))
    parser.add_argument("--regions", nargs="+", choices=REGIONS, default=list(REGIONS))
    parser.add_argument("--validate-only", action="store_true", help="Read-only verification, without creating or rewriting packs")
    parser.add_argument("--dry-load", action="store_true", help="Also construct planning instances, without optimization, in isolated subprocesses")
    parser.add_argument("--dry-load-one", type=Path, help=argparse.SUPPRESS)
    _paths.add_data_root(parser)
    args = parser.parse_args()
    configure_roots(args.data_root)
    if args.plan:
        print(json.dumps({"stage": "validate_packs" if args.validate_only else "build_packs", "selection": _paths.selection_json(args), "data_root": str(PROJECT), "packs": [{"source": str(source_pack(region, year)), "output": str(CLIMATE / "packs" / model / SCENARIO / str(year) / region), "physical_runtime": str(runtime_path(region, year))} for model in args.models for year in args.years for region in args.regions], "solver_executed": False, "model_api_executed": False, "files_written": False}, indent=2))
        return 0
    if args.dry_load_one:
        print(json.dumps(dry_load_one(args.dry_load_one)))
        return 0
    count = 0
    for model in args.models:
        for year in args.years:
            for region in args.regions:
                pack = CLIMATE / "packs" / model / SCENARIO / str(year) / region
                result = validate_pack(pack) if args.validate_only else build_pack(model, year, region)
                if args.dry_load:
                    child = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "--dry-load-one", str(pack), "--data-root", str(PROJECT)],
                                           check=True, text=True, capture_output=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
                    result["dry_load"] = json.loads(child.stdout.strip().splitlines()[-1])
                count += 1
                print(json.dumps(result), flush=True)
    print(json.dumps({"status": "validated", "packs": count, "dry_load_requested": args.dry_load,
                      "solver_executed": False, "model_api_executed": False}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
