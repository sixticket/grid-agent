# NASA future resource preparation

The paper uses NASA NEX-GDDP-CMIP6 v2.0, **MPI-ESM1-2-HR / r1i1p1f1 / SSP2-4.5**, for 2030 and 2035. All commands default to this GCM. ACCESS-CM2 and MRI-ESM2-0 remain optional sensitivity configurations. NASA provides daily constraints; ERA5 2023 supplies intraday templates. Time-index year is 2023; resource-weather/planning years match the target year.

Install `requirements.txt` in a separate environment. Set `GRID_DATA_ROOT` or `--data-root` to the external root containing `experiment/`, `prediction_local/`, `corrections/`, and `nasa_climate/`. Every command has `--plan` to print operations without input reads, downloads, scientific imports or file writes.

```bash
python preprocessing/nasa_climate/download_nasa.py download --plan
python preprocessing/nasa_climate/download_nasa.py download --workers 4
python preprocessing/nasa_climate/download_nasa.py companion --workers 2
python preprocessing/nasa_climate/download_nasa.py normalize
python preprocessing/nasa_climate/build_hourly.py
python preprocessing/nasa_climate/build_packs.py --plan
python preprocessing/nasa_climate/build_packs.py
python preprocessing/nasa_climate/build_packs.py --validate-only
python preprocessing/nasa_climate/audit_outputs.py
```

Supply the baseline 58-node coordinates, archived ERA5 responses, turbine curve, prepared future-demand packs, forecast/correction manifests and shared costs/policies first. The South Africa 2030 source is `corrections/20260925-v2/packs/south_africa_2030`; NYISO uses `prediction_local/packs/nyiso_<year>_official`; other regions use `<region>_<year>_baseline`. These are external datasets. No one-command reconstruction from citations alone is implied.

`download_nasa.py` uses NASA THREDDS subset requests. A common valid land grid within 75 km is selected for daily tasmin/tasmax/rsds/sfcWind, with boundary days retained. Companion ERA5 fills 10m wind and year-boundary hours. `build_hourly.py` maps temperature extrema onto ERA5 intraday shapes, allocates daily radiation energy subject to nighttime/TOA bounds, decomposes GHI into DNI/DHI, and scales ERA5 10m/100m winds by the NASA daily surface-wind ratio. PVWatts v8 and the inherited 4-MW turbine curve produce resource capacity factors. Numerical reconstruction and validation calculations are retained from the research source.

The annual profile preserves the source pack's time and demand values. `build_packs.py` adds truthful climate metadata, copies the inherited fleet/network/demand, records all file hashes and validates the new pack before publication to the output directory. Existing packs are checked and reused, never silently overwritten. `--dry-load` additionally constructs a short planning instance without optimization or API calls; it requires the scientific engine dependencies.

New provenance uses `data://` and `repo://` root-relative references. Original absolute archived references are accepted only when they still point inside the explicitly supplied data/source roots. Original packs pin their preparation runtime and its bytes; new packs pin the released runtime. Execution sources are independently hashed by the public launcher. Relocating an old input archive requires an explicit, provenance-preserving migration; paths/hashes must not be silently guessed.

Independent `audit_outputs.py` checks raw NetCDF, derived hourly profiles, daily conservation, provenance, demand retention and resource reconstruction. Full paper scope is 16 packs and 116 annual node profiles; the optional three-GCM scope is 48 packs and 348 profiles. Wind scaling/extreme-wind flags are retained rather than clipped. Reconstructed hourly chronology is a supply-resource sensitivity, not a deterministic target-year forecast or a new weather-dependent demand model.

Sources: [dataset DOI](https://doi.org/10.7917/OFSG3345), [NASA v2 technical note](https://www.nccs.nasa.gov/wp-content/uploads/2025/06/NEX-GDDP-CMIP6-v2-Tech_Note.pdf), [Thrasher et al. 2022](https://doi.org/10.1038/s41597-022-01393-4), [ERA5 acquisition](https://open-meteo.com/en/docs/historical-weather-api). Third-party source bytes are not distributed here.
