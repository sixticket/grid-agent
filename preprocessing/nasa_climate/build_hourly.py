#!/usr/bin/env python3
"""Synthetic hourly weather constrained by NASA daily scenarios, not forecasts.

Never changes experiment inputs. Run after download_nasa.py and the companion
ERA5 cache are complete. Model-clock time remains 2023 for demand alignment;
projection_time records the actual simulated scenario year.
"""
from __future__ import annotations

import argparse
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path

# Loading a sibling explicitly also supports importlib-based offline validators.
import importlib.util as _importlib_util
_spec = _importlib_util.spec_from_file_location("_grid_nasa_paths", Path(__file__).resolve().with_name("_paths.py"))
_paths = _importlib_util.module_from_spec(_spec)
_spec.loader.exec_module(_paths)
RELEASE_ROOT = _paths.RELEASE_ROOT
PROJECT = _paths.data_root()
BUNDLE = PROJECT / "nasa_climate"

def configure_roots(data_root=None):
    global PROJECT, BUNDLE
    PROJECT = _paths.data_root(data_root)
    BUNDLE = PROJECT / "nasa_climate"
    return PROJECT

def _load_science():
    global np, pd, pvlib, pv
    import numpy as np
    import pandas as pd
    import pvlib
    import PySAM.Pvwattsv8 as pv
MODELS = ["ACCESS-CM2", "MPI-ESM1-2-HR", "MRI-ESM2-0"]
REGIONS = ["miso", "ercot", "nyiso", "south_africa", "australia", "new_zealand", "mexico", "brazil"]
SCENARIO = "ssp245"
SOURCE_KIND = "synthetic_hourly_nasa_daily_with_era5_template"
METHODS = {
    "temperature": "Affine transformation of each complete UTC day's ERA5 hourly shape to NASA tasmin/tasmax; daily hourly mean is not constrained to tas.",
    "radiation": "NASA rsds*24 is daily Wh/m2. Redistribute the ERA5 preceding-hour radiation shape subject to nonnegative midpoint extraterrestrial-horizontal caps; preserve daily energy exactly or fail. Remaining energy uses available daylight capacity when the original positive shape is exhausted.",
    "decomposition": "pvlib Erbs using projection-date midpoint true zenith; cap DNI at extraterrestrial normal irradiance and recompute DHI to retain GHI closure.",
    "wind": "Daily NASA near-surface mean divided by ERA5 10m daily mean; multiply both ERA5 10m and 100m hourly wind by the same ratio. Preserves template vertical shear and intraday shape.",
    "time": "NASA daily timestamps treated as UTC calendar days. Source template maps month/day to 2023, previous Dec31 to 2022, next Jan1 to 2024. Demand clock retains 2023; projection clock replaces year before applying node fixed UTC offset. No DST or weekday rearrangement.",
    "pv": "PVWatts v8: 1000 kW DC, DC/AC=1, fixed rack, tilt=abs(latitude), equator-facing, losses=14%; CF per MW DC. Projection clock and minute 30. Synthetic 100m wind retained as thermal-wind proxy for original-model comparability.",
    "wind_power": "Original NREL 4MW/150m-rotor reference curve evaluated at synthetic 100m wind, no measured wake loss or additional hub-height correction.",
}
LIMITATIONS = [
    "Hourly values are an analyst-created synthetic disaggregation; NASA supplies daily climate-scenario values, not actual hourly forecasts.",
    "Three models and SSP2-4.5 do not span full climate-model or scenario uncertainty. No model-average hourly weather is generated.",
    "The 2023 template supplies hourly shapes; this does not preserve all projected multivariate dependence, storms, ramps, or future subdaily extremes.",
    "Keeping existing future-demand packs does not recompute temperature-sensitive demand; original 2023 demand-weather covariance is not preserved after daily weather replacement.",
    "Near-surface wind scaling at 100m assumes unchanged vertical shear. PV thermal wind remains a 100m proxy.",
    "UTC-day convention and fixed node clock offsets are explicit processing conventions. Solar midpoint caps approximate hourly intervals.",
    "Representative weather points, spatial downscaling and NASA bias-correction limitations remain; these outputs are sensitivity inputs, not engineering validation.",
]


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def source_file(path: Path, role: str) -> dict:
    return {"path": _paths.reference(path, PROJECT), "sha256": digest(path), "role": role}


def atomic_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    frame.to_csv(temporary, index=False)
    temporary.replace(path)


def atomic_json(value: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def finite(values, name: str) -> np.ndarray:
    array = np.asarray(values, dtype=float)
    if not np.isfinite(array).all():
        raise ValueError(f"{name}: missing or nonfinite values")
    return array


def read_weather(path: Path) -> tuple[pd.DataFrame, dict]:
    if not path.exists():
        raise FileNotFoundError(f"Missing ERA5 cache {path}; run the NASA companion ERA5 downloader first")
    data = json.loads(path.read_text())
    if "hourly" not in data or "time" not in data["hourly"]:
        raise ValueError(f"{path}: not an Open-Meteo hourly response")
    frame = pd.DataFrame(data["hourly"]).set_index("time")
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.index))
    if frame.index.tz is not None:
        frame.index = frame.index.tz_convert("UTC").tz_localize(None)
    if not frame.index.is_unique or not frame.index.is_monotonic_increasing:
        raise ValueError(f"{path}: duplicate or unordered timestamps")
    if data.get("utc_offset_seconds", 0) != 0:
        raise ValueError(f"{path}: source cache must use UTC")
    for column in ("wind_speed_10m", "wind_speed_100m"):
        if column in frame and data.get("hourly_units", {}).get(column) not in ("m/s", "ms", "m s-1"):
            raise ValueError(f"{path}: {column} must declare m/s units")
    if "temperature_2m" in frame and data.get("hourly_units", {}).get("temperature_2m") not in ("°C", "C", "celsius"):
        raise ValueError(f"{path}: temperature_2m must declare Celsius")
    if "shortwave_radiation" in frame and data.get("hourly_units", {}).get("shortwave_radiation") not in ("W/m²", "W/m2", "W m-2"):
        raise ValueError(f"{path}: shortwave_radiation must declare W/m2")
    return frame, data


def offset_hours(region: str, node) -> int:
    value = getattr(node, "utc_offset_hours", np.nan)
    if pd.notna(value):
        if float(value) != int(value):
            raise ValueError(f"{region}/{node.id}: non-integer model clock offset is unsupported")
        return int(value)
    return 2 if region == "south_africa" else 0


def demand_pack_path(region: str, year: int, override: Path | None = None) -> Path:
    if override is not None:
        suffix = "official" if region == "nyiso" else "baseline"
        return override / f"{region}_{year}_{suffix}" / "demand.csv"
    if region == "south_africa" and year == 2030:
        return PROJECT / "corrections/20260925-v2/packs/south_africa_2030/demand.csv"
    suffix = "official" if region == "nyiso" else "baseline"
    return PROJECT / f"prediction_local/packs/{region}_{year}_{suffix}/demand.csv"


def demand_path(region: str, year: int, override: Path | None) -> Path:
    if override is not None:
        override = override.expanduser().resolve()
        if not override.is_relative_to(PROJECT):
            raise ValueError("--packs-root must be within --data-root for traceable provenance")
        candidates = [override / f"{region}_{year}" / "demand.csv",
                      override / f"{region}_{year}_{'official' if region == 'nyiso' else 'baseline'}" / "demand.csv"]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        raise FileNotFoundError(f"No future demand pack found: {candidates}")
    if region == "south_africa" and year == 2030:
        path = PROJECT / "corrections/20260925-v2/packs/south_africa_2030/demand.csv"
    else:
        suffix = "official" if region == "nyiso" else "baseline"
        path = PROJECT / f"prediction_local/packs/{region}_{year}_{suffix}/demand.csv"
    if not path.exists():
        raise FileNotFoundError(f"Authoritative future demand missing: {path}; set --packs-root only for an explicit alternative")
    return path


def distribute_energy(shape: np.ndarray, cap: np.ndarray, target: float) -> tuple[np.ndarray, dict]:
    """Bounded proportional redistribution; conserves Wh/m2 for 1h bins."""
    shape, cap = finite(shape, "solar shape"), finite(cap, "solar cap")
    if target < 0 or np.any(shape < -1e-9) or np.any(cap < 0):
        raise ValueError("Negative solar target, shape, or cap")
    tolerance = max(1e-7, abs(target) * 1e-10)
    if target > float(cap.sum()) + tolerance:
        raise ValueError(f"NASA daily radiation energy {target:.6f} Wh/m2 exceeds midpoint TOA capacity {cap.sum():.6f}; cannot reconstruct physically bounded hours")
    result = np.zeros_like(cap)
    fallback = False
    iterations = 0
    while target - float(result.sum()) > tolerance:
        iterations += 1
        if iterations > len(cap) + 3:
            raise RuntimeError("Solar bounded redistribution did not converge")
        room = np.maximum(cap - result, 0)
        available = room > 1e-10
        weights = np.where(available, np.maximum(shape, 0), 0)
        if weights.sum() <= 1e-12:
            weights = room
            fallback = True
        if weights.sum() <= 0:
            raise ValueError("No daylight capacity for remaining NASA daily energy")
        proposed = (target - float(result.sum())) * weights / weights.sum()
        result += np.minimum(proposed, room)
    if abs(float(result.sum()) - target) > tolerance or np.any(result > cap + tolerance):
        raise ValueError("Solar daily-energy conservation failed")
    return result, {"shape_fallback_to_daylight_capacity": fallback,
                    "toa_cap_saturated_hours": int(np.sum((cap > 0) & np.isclose(result, cap, atol=1e-6, rtol=0)))}


@lru_cache(maxsize=50000)
def solar_geometry(day: pd.Timestamp, lat: float, lon: float, elevation: float):
    midpoint = (pd.date_range(day, periods=24, freq="h") + pd.Timedelta(minutes=30)).tz_localize("UTC")
    position = pvlib.solarposition.get_solarposition(midpoint, lat, lon, altitude=elevation)
    zenith = finite(position.zenith, "solar zenith")
    cosine = np.maximum(np.cos(np.deg2rad(zenith)), 0)
    extra = finite(pvlib.irradiance.get_extra_radiation(midpoint), "extraterrestrial irradiance")
    return midpoint, zenith, cosine, extra


def reconstruct_day(row, source: pd.DataFrame, year: int, lat: float, lon: float, elevation: float) -> tuple[pd.DataFrame, dict]:
    day = pd.Timestamp(row.date).normalize()
    target = pd.date_range(day, periods=24, freq="h")
    template_day = day.replace(year=2023 + day.year - year)
    template = pd.date_range(template_day, periods=24, freq="h")
    met = source.reindex(template)
    rad = source.reindex(template + pd.Timedelta(hours=1))
    for column in ("temperature_2m", "wind_speed_10m", "wind_speed_100m"):
        if column not in met:
            raise ValueError(f"ERA5 companion missing {column}; redownload full companion fields")
    if "shortwave_radiation" not in rad:
        raise ValueError("ERA5 companion missing shortwave_radiation")
    temperature = finite(met.temperature_2m, f"{template_day.date()} ERA5 temperature")
    wind10 = finite(met.wind_speed_10m, f"{template_day.date()} ERA5 10m wind")
    wind100 = finite(met.wind_speed_100m, f"{template_day.date()} ERA5 100m wind")
    radiation = finite(rad.shortwave_radiation, f"{template_day.date()} ERA5 radiation (needs next-day 00:00)")
    if np.any(wind10 < 0) or np.any(wind100 < 0):
        raise ValueError("ERA5 wind speeds must be nonnegative")
    minimum, maximum, rsds, sfc_wind = finite([row.tasmin_K, row.tasmax_K, row.rsds_W_m2, row.sfcWind_m_s], "NASA targets")
    minimum -= 273.15
    maximum -= 273.15
    if not (-120 <= minimum <= maximum <= 90) or rsds < 0 or not (0 <= sfc_wind <= 100):
        raise ValueError(f"Invalid NASA targets on {day.date()}: {minimum}, {maximum}, {rsds}, {sfc_wind}")
    flags = []
    temp_range = float(np.ptp(temperature))
    if temp_range > 1e-9:
        fraction = (temperature - temperature.min()) / temp_range
    elif maximum - minimum <= 1e-9:
        fraction = np.zeros(24)
    else:
        # Explicit fallback preserves extrema when observed intraday shape is flat.
        fraction = np.cos(2 * np.pi * (np.arange(24) + lon / 15 - 14) / 24)
        fraction = (fraction - fraction.min()) / np.ptp(fraction)
        flags.append("flat_era5_temperature_used_solar_time_cosine_shape")
    tnew = minimum + (maximum - minimum) * fraction
    mean10 = float(wind10.mean())
    if mean10 < 1e-6:
        if sfc_wind > 1e-6:
            raise ValueError(f"{day.date()}: ERA5 10m daily mean {mean10} cannot normalize NASA wind {sfc_wind}; no invented vertical shear")
        ratio = 0.0
    else:
        ratio = float(sfc_wind / mean10)
    u10, u100 = wind10 * ratio, wind100 * ratio
    if ratio < 0.25 or ratio > 4:
        flags.append("large_daily_wind_scaling_ratio")
    if float(u100.max()) > 50:
        flags.append("synthetic_100m_wind_exceeds_50_m_s")
    midpoint, zenith, cosine, extra = solar_geometry(day, lat, lon, elevation)
    cap = extra * cosine
    ghi, solar_info = distribute_energy(radiation, cap, float(rsds * 24))
    split = pvlib.irradiance.erbs(ghi, zenith, midpoint)
    raw_dni = finite(split["dni"], "Erbs DNI")
    dni = np.clip(raw_dni, 0, extra)
    dhi = ghi - dni * cosine
    if np.any(dhi < -1e-7):
        raise ValueError("Erbs decomposition has negative diffuse radiation")
    dhi = np.maximum(dhi, 0)
    if solar_info["shape_fallback_to_daylight_capacity"]:
        flags.append("solar_shape_exhausted_used_remaining_daylight_capacity")
    frame = pd.DataFrame({"projection_utc_time": target, "source_utc_time": template,
                          "nasa_date": day.strftime("%Y-%m-%d"), "temperature_2m_C": tnew,
                          "wind_speed_10m_m_s": u10, "wind_speed_100m_m_s": u100,
                          "ghi_W_m2": ghi, "dni_W_m2": dni, "dhi_W_m2": dhi,
                          "solar_zenith_deg": zenith, "toa_horizontal_W_m2": cap})
    finite(frame.select_dtypes(include=[np.number]), "reconstructed day")
    validation = {"date": day.strftime("%Y-%m-%d"), "source_date": template_day.strftime("%Y-%m-%d"),
                  "temperature_min_error_C": float(tnew.min() - minimum),
                  "temperature_max_error_C": float(tnew.max() - maximum),
                  "reconstructed_temperature_mean_C": float(tnew.mean()),
                  "target_rsds_W_m2": float(rsds), "reconstructed_rsds_W_m2": float(ghi.mean()),
                  "solar_daily_energy_error_Wh_m2": float(ghi.sum() - rsds * 24),
                  "solar_decomposition_max_error_W_m2": float(np.max(np.abs(ghi - dhi - dni * cosine))),
                  "target_sfcWind_m_s": float(sfc_wind), "reconstructed_10m_wind_mean_m_s": float(u10.mean()),
                  "wind_10m_mean_error_m_s": float(u10.mean() - sfc_wind),
                  "era5_10m_wind_mean_m_s": mean10, "daily_wind_scaling_ratio": ratio,
                  "maximum_100m_wind_m_s": float(u100.max()),
                  "dni_toa_adjusted_hours": int(np.sum(raw_dni > extra)),
                  **solar_info, "flags": flags}
    if max(abs(validation["temperature_min_error_C"]), abs(validation["temperature_max_error_C"])) > 1e-7:
        raise ValueError("Temperature extrema conservation failed")
    if abs(validation["wind_10m_mean_error_m_s"]) > 1e-7:
        raise ValueError("Near-surface daily mean wind conservation failed")
    return frame, validation


def build_node(model: str, year: int, region: str, node, daily: pd.DataFrame, daily_path: Path, packs_root: Path | None) -> dict:
    bus = str(node.id)
    subset = daily.loc[(daily.region == region) & (daily.node == bus)].copy()
    if subset.empty:
        raise ValueError(f"{daily_path}: missing NASA daily data for {region}/{bus}")
    subset["date"] = pd.to_datetime(subset.date).dt.normalize()
    if subset.date.duplicated().any():
        raise ValueError(f"{model}/{year}/{region}/{bus}: duplicate NASA dates")
    latitude, longitude = float(node.latitude), float(node.longitude)
    if not np.allclose(subset.latitude, latitude) or not np.allclose(subset.longitude, longitude):
        raise ValueError(f"{region}/{bus}: NASA requested coordinates differ from nodes.csv")
    subset = subset.set_index("date", drop=False).sort_index()
    daily_provenance_path = daily_path.with_suffix(".provenance.json")
    if not daily_provenance_path.exists():
        raise FileNotFoundError(f"Missing NASA extraction provenance {daily_provenance_path}; complete normalization first")
    daily_provenance = json.loads(daily_provenance_path.read_text())
    for key, expected_value in (("model", model), ("scenario", SCENARIO), ("projection_year", year)):
        if daily_provenance.get(key) != expected_value:
            raise ValueError(f"{daily_provenance_path}: {key} identity mismatch; expected {expected_value}")
    if daily_provenance.get("daily_csv_sha256") != digest(daily_path):
        raise ValueError(f"{daily_path}: CSV does not match extraction-provenance hash")
    if str(daily_provenance.get("version")) != "2.0":
        raise ValueError(f"{daily_provenance_path}: expected verified NASA version 2.0")
    mapping = [entry for entry in daily_provenance.get("node_mapping", [])
               if entry.get("region") == region and str(entry.get("node")) == bus]
    if len(mapping) != 1:
        raise ValueError(f"{daily_provenance_path}: need one grid-selection provenance record for {region}/{bus}")
    grid_selection = mapping[0]
    if grid_selection.get("model") != model or grid_selection.get("scenario") != SCENARIO or grid_selection.get("year") != year:
        raise ValueError(f"{daily_provenance_path}: grid-selection model/scenario/year identity mismatch")
    if not np.allclose(subset.nasa_latitude, grid_selection["nasa_latitude"]) or not np.allclose(subset.nasa_longitude, grid_selection["nasa_longitude"]):
        raise ValueError(f"{region}/{bus}: inconsistent NASA grid coordinates")
    node_flags = []
    if grid_selection.get("nearest_cell_missing"):
        node_flags.append("nearest_grid_cell_missing_used_nearest_common_finite_land_cell")
    if float(grid_selection.get("approx_distance_km", 0)) > 30:
        node_flags.append("NASA_selected_land_grid_cell_over_30km_from_representative_point")
    nodes_path = PROJECT / "experiment" / region / "nodes.csv"
    original_path = PROJECT / "experiment" / region / "raw/weather" / f"{bus}.json"
    companion_path = BUNDLE / "raw/era5_10m" / region / f"{bus}.json"
    original, metadata = read_weather(original_path)
    companion, _ = read_weather(companion_path)
    # Preserve original cached values; companion fills 10m wind and boundary hours.
    source = original.combine_first(companion).sort_index()
    elevation = float(metadata["elevation"])
    if not np.isfinite(elevation):
        raise ValueError(f"{original_path}: missing finite elevation")
    demand_file = demand_path(region, year, packs_root)
    demand = pd.read_csv(demand_file)
    if "time" not in demand or bus not in demand:
        raise ValueError(f"{demand_file}: missing time or node {bus}")
    local = pd.DatetimeIndex(pd.to_datetime(demand.time))
    expected = pd.date_range("2023-01-01", periods=8760, freq="h")
    if not local.equals(expected):
        raise ValueError(f"{demand_file}: expected complete ordered 2023 hourly model clock")
    load = finite(demand[bus], "future demand")
    if np.any(load < 0):
        raise ValueError("Negative future demand")
    projection_local = pd.DatetimeIndex([stamp.replace(year=year) for stamp in local])
    off = offset_hours(region, node)
    utc = projection_local - pd.Timedelta(hours=off)
    required_days = utc.normalize().unique()
    missing = required_days.difference(subset.index)
    if len(missing):
        raise ValueError(f"{daily_path}: missing UTC days for {region}/{bus}: {list(missing.strftime('%Y-%m-%d'))}; download target plus adjacent-year boundary days")
    frames, checks = [], []
    for day in required_days:
        frame, check = reconstruct_day(subset.loc[day], source, year, latitude, longitude, elevation)
        frames.append(frame)
        checks.append(check)
    full = pd.concat(frames, ignore_index=True).set_index("projection_utc_time", drop=False)
    hourly = full.loc[utc].reset_index(drop=True)
    hourly.insert(0, "projection_time", projection_local)
    hourly.insert(0, "time", local)
    if len(hourly) != 8760 or hourly.isna().any().any():
        raise ValueError("Invalid complete annual hourly output")
    srd = dict(lat=latitude, lon=longitude, tz=off, elev=elevation,
               year=projection_local.year.tolist(), month=projection_local.month.tolist(),
               day=projection_local.day.tolist(), hour=projection_local.hour.tolist(), minute=[30] * 8760,
               dn=hourly.dni_W_m2.tolist(), df=hourly.dhi_W_m2.tolist(), gh=hourly.ghi_W_m2.tolist(),
               tdry=hourly.temperature_2m_C.tolist(), wspd=hourly.wind_speed_100m_m_s.tolist())
    solar_model = pv.default("PVWattsNone")
    solar_model.SolarResource.solar_resource_data = srd
    solar_model.SystemDesign.system_capacity = 1000.0
    solar_model.SystemDesign.tilt = abs(latitude)
    solar_model.SystemDesign.azimuth = 0 if latitude < 0 else 180
    solar_model.SystemDesign.array_type = 0
    solar_model.SystemDesign.dc_ac_ratio = 1.0
    solar_model.SystemDesign.losses = 14.0
    solar_model.execute()
    raw_solar = finite(solar_model.Outputs.ac, "PVWatts AC output") / 1e6
    solar_cf = np.clip(raw_solar, 0, 1)
    no_sun = (hourly.ghi_W_m2.to_numpy() <= 1e-9) | (hourly.solar_zenith_deg.to_numpy() >= 90)
    if np.any(solar_cf[no_sun] > 1e-9):
        raise ValueError("PVWatts produces positive solar CF in zero-irradiance/nighttime intervals")
    curve_path = PROJECT / "experiment/shared/raw/NREL_Reference_4MW_150_powercurve.csv"
    curve = pd.read_csv(curve_path)
    speeds, powers = finite(curve.iloc[:, 0], "power curve speed"), finite(curve.iloc[:, 1], "power curve kW")
    if np.any(np.diff(speeds) <= 0):
        raise ValueError("Power curve speeds must be strictly increasing")
    wind_cf = np.interp(hourly.wind_speed_100m_m_s, speeds, powers, left=0, right=0) / 4000
    profiles = pd.DataFrame({"time": local, "demand_MW": load, "solar_cf": solar_cf, "wind_cf": wind_cf})
    finite(profiles[["demand_MW", "solar_cf", "wind_cf"]], "annual generation profiles")
    if not ((profiles.solar_cf.between(0, 1)).all() and profiles.wind_cf.between(0, 1).all()):
        raise ValueError("Generation capacity factors outside [0,1]")
    stem = Path(model) / SCENARIO / str(year) / region / bus
    hourly_file = BUNDLE / "hourly" / stem.with_suffix(".csv")
    profile_file = BUNDLE / "profiles" / stem.with_suffix(".csv")
    validation_file = BUNDLE / "validation" / stem.with_suffix(".json")
    provenance_file = BUNDLE / "profiles" / stem.with_suffix(".provenance.json")
    sources = [source_file(daily_path, "NASA_normalized_daily"), source_file(daily_provenance_path, "NASA_daily_extraction_provenance"), source_file(nodes_path, "representative_nodes"),
               source_file(original_path, "ERA5_original_hourly_template"), source_file(companion_path, "ERA5_10m_and_boundary_companion"),
               source_file(demand_file, "future_demand"), source_file(curve_path, "NREL_wind_power_curve"),
               source_file(Path(__file__), "processing_code")]
    summary = {"hours": 8760, "complete_utc_days_validated": len(checks),
               "solar_cf_mean": float(solar_cf.mean()), "wind_cf_mean": float(wind_cf.mean()),
               "temperature_mean_C": float(hourly.temperature_2m_C.mean()),
               "ghi_annual_Wh_m2": float(hourly.ghi_W_m2.sum()),
               "days_with_flags": sum(bool(check["flags"]) for check in checks),
               "node_flags": node_flags,
               "maximum_abs_daily_solar_error_Wh_m2": max(abs(check["solar_daily_energy_error_Wh_m2"]) for check in checks),
               "maximum_abs_daily_wind_10m_error_m_s": max(abs(check["wind_10m_mean_error_m_s"]) for check in checks),
               "pv_ac_negative_values_clipped": int(np.sum(raw_solar < 0)),
               "pv_ac_above_nameplate_values_clipped": int(np.sum(raw_solar > 1)),
               "maximum_night_or_zero_irradiance_solar_cf": float(solar_cf[no_sun].max()) if no_sun.any() else 0.0,
               "daily_conservation_scope": "Full reconstructed UTC days before selecting 8760 demand-clock hours; boundary UTC days are only partly present in the annual CSV."}
    identity = {"schema_version": 1, "status": "validated", "source_kind": SOURCE_KIND,
                "model": model, "scenario": SCENARIO, "projection_year": year, "region": region, "node": bus}
    validation = {**identity, "summary": summary, "methods": METHODS, "limitations": LIMITATIONS,
                  "source_files": sources, "grid_selection": grid_selection, "daily_conservation": checks}
    # Write provenance last; readers must validate its hashes before accepting files.
    atomic_csv(hourly, hourly_file)
    atomic_csv(profiles, profile_file)
    atomic_json(validation, validation_file)
    provenance = {**identity, "source_files": sources,
                  "hourly_file": source_file(hourly_file, "synthetic_hourly_weather"),
                  "profile_file": source_file(profile_file, "generation_profile"),
                  "validation_file": source_file(validation_file, "daily_conservation_validation"),
                  "methods": METHODS, "limitations": LIMITATIONS, "summary": summary,
                  "grid_selection": grid_selection,
                  "pv_system_design": solar_model.SystemDesign.export(),
                  "point": {"latitude": latitude, "longitude": longitude, "elevation_m": elevation,
                            "utc_offset_hours": off, "nasa_grid_latitude": float(subset.nasa_latitude.iloc[0]),
                            "nasa_grid_longitude": float(subset.nasa_longitude.iloc[0])},
                  "software": {"numpy": np.__version__, "pandas": pd.__version__, "pvlib": pvlib.__version__}}
    atomic_json(provenance, provenance_file)
    print(f"{model} {year} {region}/{bus}: {len(hourly)} h, solar={solar_cf.mean():.4f}, wind={wind_cf.mean():.4f}, flagged_days={summary['days_with_flags']}", flush=True)
    return {**identity, "provenance_file": str(provenance_file), "summary": summary}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", nargs="+", default=["MPI-ESM1-2-HR"], choices=MODELS)
    parser.add_argument("--years", nargs="+", type=int, default=[2030, 2035], choices=[2030, 2035])
    parser.add_argument("--regions", nargs="+", default=REGIONS, choices=REGIONS)
    parser.add_argument("--nodes", nargs="+", help="Optional node IDs for a small validation run")
    parser.add_argument("--packs-root", type=Path, help="Explicit alternative root of future demand packs")
    _paths.add_data_root(parser)
    args = parser.parse_args()
    configure_roots(args.data_root)
    if args.plan:
        print(json.dumps({"stage": "hourly", "selection": _paths.selection_json(args), "data_root": str(PROJECT), "climate_root": str(BUNDLE), "demand_packs": [str(demand_pack_path(region, year, args.packs_root)) for year in args.years for region in args.regions], "network_executed": False, "files_written": False}, indent=2))
        return
    _load_science()
    required = {"date", "region", "node", "latitude", "longitude", "nasa_latitude", "nasa_longitude", "tasmin_K", "tasmax_K", "rsds_W_m2", "sfcWind_m_s"}
    results = []
    for model in args.models:
        for year in args.years:
            path = BUNDLE / f"processed/daily/{model}/{SCENARIO}/{year}.csv"
            if not path.exists():
                raise FileNotFoundError(f"NASA normalized daily CSV missing: {path}; complete download/normalization first")
            daily = pd.read_csv(path, dtype={"region": str, "node": str})
            if not required.issubset(daily.columns):
                raise ValueError(f"{path}: missing columns {sorted(required - set(daily.columns))}")
            for region in args.regions:
                nodes = pd.read_csv(PROJECT / "experiment" / region / "nodes.csv", dtype={"id": str})
                if args.nodes:
                    nodes = nodes.loc[nodes.id.isin(args.nodes)]
                for node in nodes.itertuples():
                    results.append(build_node(model, year, region, node, daily, path, args.packs_root))
    if not results:
        raise ValueError("No requested nodes matched the selected regions")
    index = BUNDLE / "validation" / "last_build.json"
    atomic_json({"schema_version": 1, "status": "validated", "selection": _paths.selection_json(args), "outputs": results}, index)
    print(f"Validated {len(results)} annual node scenarios. Index: {index}", flush=True)


if __name__ == "__main__":
    main()
