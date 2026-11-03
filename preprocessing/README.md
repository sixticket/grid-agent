# Preparing external study inputs

Preparation code uses `GRID_DATA_ROOT`, defaulting to repository `data/`. Set an external root before executing builders. Code imports remain anchored to the checkout; raw inputs and generated packs live under the data root. Preparation commands can download, write data or calibrate feeders only when explicitly run.

| Source | Role |
| --- | --- |
| `experiment/scripts/build_inputs.py` | Baseline demand, inventory, assumptions and legacy network |
| `experiment/scripts/build_country_pack.py` | Australia, New Zealand, Mexico and Brazil packs |
| `experiment/scripts/build_zonal_network.py` | Subsequent zonal transport network construction |
| `experiment/scripts/build_weather.py` | ERA5 baseline weather and renewable output |
| `experiment/scripts/fetch_cenace_demand.py` | CENACE hourly settlement source acquisition |
| `experiment/scripts/calibrate_feeders.py` | Representative feeder calibration; updates config |
| `experiment/scripts/verify_inputs.py` | Offline hash inventory/verification of external baseline archive |
| `prediction/scenarios.py` and `prediction/run.py` | Demand-only future intermediate build/verification |
| `prediction_local/prepare_all.py`, `prepare_nyiso.py` | Archived forecast extraction and annual targets |
| `prediction_local/verify_all.py` | Demand-only targets and unchanged template-resource checks |
| [nasa_climate](nasa_climate/README.md) | Target-year NASA resources, final packs and independent audits |

Install scientific preparation dependencies from `requirements.txt`; the XLSB extractor additionally needs `pyxlsb`. NASA has its own additional requirements. The original source inputs, manual mappings and layout are described in [INPUT_DATA.md](../docs/INPUT_DATA.md). Baseline scripts consume `experiment/<region>/raw`, `experiment/shared/raw`, coordinates and curated mappings. Demand extraction uses `prediction_documents/` and `prediction_local/` below the data root.

```bash
export GRID_DATA_ROOT=/path/to/grid-agent-data
python preprocessing/prediction_local/prepare_nyiso.py --help
python preprocessing/prediction_local/prepare_all.py --help
python preprocessing/prediction/run.py build --spec /path/to/scenario.json --output /path/to/new-demand-pack
python preprocessing/prediction/run.py verify --pack /path/to/demand-pack
```

Demand-only transformations retain ERA5 2023 resource columns. These packs are **intermediate inputs** for `nasa_climate/build_packs.py`; the active future experiment launcher requires matching NASA 2030/2035 resource metadata. `prediction_local/verify_all.py` verifies the intermediate, not the final NASA packs.

`build_inputs.py` and `build_weather.py` load required source tables at import time, so supply those inputs before executing them or importing dependent builders. The first writes the initial legacy network; run the zonal builder subsequently. Calibration imports the released scientific distribution engine and updates external configs. Use it only when intentionally preparing a new dataset, not to modify the archived study packs.

`prediction/run.py` provides preparation only; root `tools/run_case.py` and `tools/run_campaign.py` are the supported experiment commands. Generated data, CSVs, caches and results remain excluded from the release and Git.
