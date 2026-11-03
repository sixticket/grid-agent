"""Attach truthful climate metadata without changing case-specific physics.

Install once in a fresh worker, before importing single_agent or multi_agent.
The future NASA packs keep the 2023 model clock. Only South Africa 2030 uses
the existing calendar-aware physical runtime; no other case gains project
calendar filtering through this adapter.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]
SOURCE_KIND = "synthetic_hourly_nasa_daily_with_era5_template"


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def dependency_paths():
    """Controller/worker must pin these files in its source manifest."""
    return [Path(__file__).resolve(), PROJECT / "preprocessing/nasa_climate/build_packs.py"]


def install(scenario_core, pack, *, expected_region=None, expected_year=None,
            expected_model="MPI-ESM1-2-HR", validate=True):
    """Validate inputs and install a bound load_pack wrapper; perform no solve/API.

    Return a JSON-serializable audit record for the worker's final metadata.
    validate=False is only for a controller that just performed full validation;
    identity and config/manifest agreement are always checked here.
    """
    pack = Path(pack).resolve()
    _require(not getattr(scenario_core, "_nasa_adapter_installed", False),
             "Climate adapter already installed; use a fresh process per job")
    cfg = json.loads((pack / "config.json").read_text())
    region = cfg["id"]
    if expected_region is not None:
        _require(region == expected_region, "Job region differs from input pack")
    manifest_path = pack / "prediction_manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else None
    metadata = manifest["scenario"] if manifest else None
    climate_manifest_path = pack / "climate_manifest.json"
    is_climate = climate_manifest_path.exists()
    if metadata is not None:
        _require(cfg.get("prediction") == metadata, "Config prediction and manifest scenario differ")
        _require(metadata.get("region") == region, "Prediction region differs from pack id")
        year = metadata.get("planning_year")
        _require(type(year) is int and year in (2030, 2035), "Unsupported future planning year")
    else:
        _require(cfg.get("prediction") is None, "Prediction config has no matching manifest")
        year = 2023
    if expected_year is not None:
        _require(year == expected_year, "Job planning year differs from input pack")
    has_calendar = hasattr(scenario_core, "planning_year_for")
    _require(has_calendar == ((region, year) == ("south_africa", 2030)),
             "Physical runtime route changed: only South Africa 2030 may use the calendar runtime")
    validation = None
    if year != 2023:
        _require(is_climate, "This campaign requires a NASA climate pack for future years")
        climate = metadata.get("climate")
        _require(isinstance(climate, dict), "Future pack needs explicit climate metadata")
        for key, value in {"source_kind": SOURCE_KIND, "scenario": "ssp245",
                           "projection_year": year, "weather_year": year,
                           "weather_template_year": 2023, "time_axis_year": 2023}.items():
            _require(climate.get(key) == value, f"Incorrect climate {key}")
        for key, value in {"weather_year": year, "weather_template_year": 2023,
                           "time_axis_year": 2023}.items():
            _require(metadata.get(key) == value, f"Incorrect prediction {key}")
        if expected_model is not None:
            _require(climate.get("model") == expected_model, "Wrong GCM for this campaign")
        if validate:
            validator_path = dependency_paths()[1]
            spec = importlib.util.spec_from_file_location("_campaign_nasa_pack_validator", validator_path)
            validator = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(validator)
            validation = validator.validate_pack(pack)
        else:
            validation = {"status": "not_repeated", "reason": "Caller explicitly disabled full input validation"}
    else:
        _require(not is_climate and metadata is None, "2023 baseline must use the original ERA5 experiment pack")
        climate = None
    original_load = scenario_core.load_pack
    manifest_digest = _sha(manifest_path) if manifest else None
    config_digest = _sha(pack / "config.json")

    def load_with_metadata(name, policy="baseline"):
        requested = Path(name)
        requested = requested.resolve() if requested.is_absolute() else (scenario_core.ROOT / requested).resolve()
        _require(requested == pack, "Worker attempted to load an input pack other than its pinned job")
        _require(_sha(pack / "config.json") == config_digest, "Pack config changed after adapter installation")
        if manifest:
            _require(_sha(manifest_path) == manifest_digest, "Prediction manifest changed after adapter installation")
        base = original_load(name, policy=policy)
        _require(base.SCENARIO.get("year") == 2023, "Physical model time-axis year must remain 2023")
        if has_calendar:
            _require(base.SCENARIO.get("planning_year") == year, "SA2030 project calendar was not retained")
        else:
            _require("planning_year" not in base.SCENARIO, "Original physical runtime unexpectedly gained a planning calendar")
        base.SCENARIO.update(weather_year=year, weather_template_year=2023, time_axis_year=2023)
        if metadata is not None:
            base.SCENARIO["prediction"] = copy.deepcopy(metadata)
            base.SCENARIO["climate"] = copy.deepcopy(climate)
            base.SCENARIO_SUMMARY = base.SCENARIO_SUMMARY.replace(
                "profile/weather year: 2023",
                f"profile time-axis year: 2023; climate projection year: {year}; ERA5 hourly-template year: 2023")
            base.SCENARIO_SUMMARY += "\nFuture demand and NASA climate scenario: " + json.dumps(metadata, ensure_ascii=False)
            base.SCENARIO_SUMMARY += (" NASA supplies daily climate constraints; hourly weather is synthetically reconstructed. "
                                      "Demand remains the inherited forecast profile and is not recomputed from this weather. "
                                      "This is a climate-scenario sensitivity, not a deterministic target-year weather prediction.")
        return base

    scenario_core.load_pack = load_with_metadata
    scenario_core._nasa_adapter_installed = True
    return {"schema_version": 1, "region": region, "planning_year": year,
            "weather_year": year, "weather_template_year": 2023, "time_axis_year": 2023,
            "source_kind": SOURCE_KIND if is_climate else "ERA5_2023_reanalysis",
            "model": climate["model"] if climate else None, "scenario": "ssp245" if climate else None,
            "physical_route": "corrected_sa2030" if has_calendar else "original",
            "pack": str(pack), "prediction_manifest_sha256": manifest_digest,
            "climate_manifest_sha256": _sha(climate_manifest_path) if is_climate else None,
            "climate_input_sha256": metadata.get("climate_input_sha256") if metadata else None,
            "validation": validation,
            "adapter_dependencies": {str(path): _sha(path) for path in dependency_paths()}}


# Stable public entry point used by the campaign worker and offline preflight.
install_climate_adapter = install
