# Input packs and reconstruction

This release contains code and documentation, without the study datasets. A fresh checkout cannot run the real eight-region experiments until compatible input packs and shared numerical inputs are supplied. Source references are listed in [DATA_SOURCES.md](DATA_SOURCES.md). This document describes the files consumed by the code, the original preprocessing paths, and the boundary between an independent reconstruction and exact study reproduction.

The public runner is `tools/run_case.py`. Provider runtimes live under `models/sol` and `models/opus`; preprocessing code retains its original directory structure under the external `GRID_DATA_ROOT` (default `data/`); code stays under `preprocessing/`. Prepared datasets live outside those code directories. Do not assume a dataset is included merely because its original filename is documented here.

## Runtime pack layout

```text
data/experiment/                 # example external data location
  shared/
    costs_us.json
    costs_za.json
    electricity_policies.json
  <region>/
    config.json
    assumptions.json
    network.json
    inventory.json
    profiles/
      <node-id>.csv
```

Pass the external pack and shared-input directories explicitly with `--pack` and `--shared`. For example, `data/experiment/miso` and `data/experiment/shared` are user-supplied directories; neither is populated by this release. `--shared` names the actual directory containing the cost JSON and `electricity_policies.json`, not its parent. Its basename must be exactly `shared`, preserving the original runtime's sibling-directory lookup. The release wrapper supplies these paths to the selected runtime. Moving a pack alone does not supply its shared dependencies.

```bash
python tools/run_case.py --deployment sol --region miso --year 2023 \
  --agent single --pack data/experiment/miso \
  --shared data/experiment/shared --output runs/miso-sol-2023-single
```

Without `--execute`, this is the runner's preparation/review path. Add `--execute` only after the inputs, runtime configuration, solver, and provider setup are ready. The real agent run may use paid model APIs. For a future example, use `--year 2030 --pack data/nasa_climate/packs/MPI-ESM1-2-HR/ssp245/2030/miso` with the same shared directory and the corresponding `--data-root`. Demand-only packs under `prediction_local` are intermediate preparation inputs. South Africa 2030 requires its corrected prepared pack and the appropriate calendar runtime profile; an uncorrected baseline directory is not equivalent.

| File | Runtime contents |
|---|---|
| `config.json` | Region `id`, `description`, `cost_file`, `global_new_capacity_limits_GW` for wind/solar/nuclear, and `distribution` settings. The IDs in config, network, and inventory must agree. |
| `assumptions.json` | Numerical assumptions including `voll_usd_per_MWh`, `unserved_fraction_limit`, and `co2_limit_Mt`; interpretation metadata for demand, clocks, baseline year, fleet date, technology operations, storage boundary, costs, network, and spatial uncertainty. Preserve these fields when building a new pack. |
| `network.json` | Versioned network object, node IDs, profile paths, capacity bounds, corridors, and optional interface/zone/project limits. Details below. |
| `inventory.json` | Required for brownfield runs. Existing generation/storage assets, provenance, and fixed operating assumptions. A missing fleet must not silently be treated as an empty fleet. |
| `profiles/<node-id>.csv` | Exactly the four columns `time,demand_MW,solar_cf,wind_cf`, with 8,760 rows per node. |
| `shared/<cost_file>` | Prepared numerical cost dictionary referenced by config. The study uses `costs_us.json` or `costs_za.json`. Raw cost reports do not replace this file. |
| `shared/electricity_policies.json` | Source-backed policy mapping used by the policy loader, including baseline reporting and explicitly selected published-policy scenarios. |

Preparation and audit also use `demand.csv`, `nodes.csv`, `inventory_audit.csv`, `input_audit.json`, input/source manifests, and `profiles/<node-id>.provenance.json`. These are useful reconstruction evidence but are not substitutes for the profiles, network, and inventory consumed by the runtime. Future packs also preserve a `prediction_manifest.json` and demand audit.

For future packs, `config.json.prediction` must equal `prediction_manifest.json.scenario`. This object must identify the requested `region` and `planning_year`, with `weather_year` equal to the planning year, `weather_template_year: 2023`, and `time_axis_year: 2023`. Its nested `climate` identifies MPI-ESM1-2-HR / SSP2-4.5 and the matching projection year. A required `climate_manifest.json` pins payload, source-pack, hourly provenance and preparation-runtime hashes. The launcher checks those fields and adds this scenario metadata to the agent's state; it does not infer a forecast from the folder name.

## Data-root and climate manifest

Baseline/demand/NASA preparation reads and writes below `GRID_DATA_ROOT`. The NASA tools also accept `--data-root`. The public runner accepts explicit pack/shared/data-root paths. NASA pack hashes are verified against the inherited demand pack, saved hourly provenance and raw-source dependencies; preserve those external dependencies alongside the pack. New manifests use `data://` and `repo://` references. Historical manifests retain their original pinned paths; a release path adaptation does not rewrite those bytes.

## Node identifiers

Treat identifiers as strings so that MISO leading zeros are preserved.

| Region | IDs |
|---|---|
| `miso` | `0001`, `0004`, `0006`, `0027`, `0035`, `8910` |
| `ercot` | `COAS`, `EAST`, `FWES`, `NCEN`, `NRTH`, `SCEN`, `SOUT`, `WEST` |
| `nyiso` | `ZONA`, `ZONB`, `ZONC`, `ZOND`, `ZONE`, `ZONF`, `ZONG`, `ZONH`, `ZONI`, `ZONJ`, `ZONK` |
| `south_africa` | `EC`, `FS`, `GP`, `KZN`, `LP`, `MP`, `NW`, `NC`, `WC`, `HYDRA` |
| `australia` | `NSW1`, `QLD1`, `VIC1`, `SA1`, `TAS1` |
| `new_zealand` | `UNI`, `CNI`, `LNI`, `USI`, `LSI` |
| `mexico` | `BCA`, `BCS`, `NOR`, `NTE`, `NES`, `OCC`, `CEN`, `ORI`, `PEN` |
| `brazil` | `N`, `NE`, `SE`, `S` |

## Profile and time conventions

`time` must parse as a unique datetime series shared exactly by all nodes. The preparation code expects an aligned 8,760-hour year; run-time short windows select contiguous rows from it. `demand_MW` is finite, nonnegative hourly mean power. `solar_cf` and `wind_cf` are finite fractions in [0, 1]. Each row represents one hour, so zonal annual energy is `sum(demand_MW) / 1000` GWh. All node demand shares must sum to one, and node demand must reproduce the pack total every hour.

Preserve each regional demand clock and the corresponding conversion used to align weather. Do not interpret every timezone-naive CSV as UTC. The source builders retain UTC for the US, SAST interpretation for South Africa, and declared local-standard-time conventions for other systems. In the future scenarios, timestamps still describe the retained 2023 template; the planning year is recorded separately. A 2030/2035 pack is not formed by relabelling dates and introducing leap hours.

The generic future transformation is:

```text
baseline_GWh[z] = sum(D_2023_MW[z, h]) / 1000
D_future_MW[z, h] = D_2023_MW[z, h] * target_GWh[z] / baseline_GWh[z]
```

Use full-precision targets and factors. The demand-only intermediate audit confirms unchanged timestamps and resource columns. The subsequent NASA stage replaces solar/wind capacity factors, preserves demand and the 8,760-row model clock, and records target-year resource metadata. No separate load blocks are added. Australia and New Zealand targets require the definition/growth conversions in DATA_SOURCES.md; do not replace them with a direct reading of a differently defined forecast total.

## Network schema

The reported regional models use `schema_version: 2` and `flow_model: "transport"`. Required top-level fields are `schema_version`, `scenario`, `provenance`, `voltage_kV`, `flow_model`, `greenfield_policy`, `nodes`, and `corridors`. Optional v2 structures include `interfaces`, `zone_limits`, and `projects`. `provenance` requires a nonempty source description and `kind` of `synthetic`, `reported`, or `derived`. The engine also supports a legacy v1 DC-candidate schema; it is not an interchangeable reconstruction of the study's v2 networks.

Each study node has `id`, `demand_share`, `profiles_csv`, and `new_capacity_limits_MW`. The latter has exactly seven keys: `wind`, `solar`, `nuclear`, `battery`, `electrolyzer`, `fuel_cell`, and `h2_storage_MWh`. Power limits are MW; hydrogen storage is MWh. Limits must be finite and nonnegative. Node IDs are unique alphanumeric/underscore/hyphen strings.

Each v2 corridor records `id`, `bus0`, `bus1`, `length_km`, `circuit_rating_MW`, `max_new_circuits`, `existing_circuits`, `new_annual_cost_per_circuit_usd`, `existing_annual_fixed_cost_per_circuit_usd`, and `source`. Directional ratings, explicit existing MW capacities, voltage, notes, and planned-project IDs are optional. Circuit counts are nonnegative integers; corridor endpoints must be distinct known nodes. A `dc` flow model additionally requires reactance and an angle bound. Interface cuts, direction-specific limits, and project membership are validated by the engine and must be reconstructed from the archived mappings, not inferred solely from two endpoint names.

Preserve the distinction between reported transfer limits, derived ratings, nonbinding proxies, and generic expansion assumptions. The transport model does not represent a detailed AC network. For South Africa 2030, project-year eligibility is a documented special runtime/input treatment; copying the original project permissions to every year does not reproduce those two corrected cases.

## Brownfield inventory schema

`inventory.json` has exactly the top-level keys `schema_version`, `scenario`, `provenance`, and `assets`. Its version is 1. `provenance` contains `kind`, a nonempty `source`, and an integer `year`. Every asset has a unique `id`, supported `technology`, `annual_fixed_cost_usd`, and `capacity_MW`, except hydrogen storage, which uses `energy_MWh`. In a zonal network, each retained asset must have a valid `bus`; the engine does not silently relocate it.

Battery, pumped-storage, and other-storage assets also require `energy_MWh` and `b1_fraction`, with optional storage/dispatch efficiencies. Other technology-specific fields include availability, marginal cost in USD/MWh, emissions in tonnes/MWh, annual capacity factor, nuclear minimum output, or conversion efficiency. Power/energy are positive when present; fixed cost is nonnegative; fractional fields are bounded appropriately. The strict validator in the selected `models/<deployment>/runtime_<profile>/engine/planning.py` is authoritative for accepted keys. Archived Qwen sources have separate provenance and are outside the active paper.

Prepared assets preserve operating and non-operating inventory distinctions, annual energy constraints, missing-storage-energy assumptions, and geographical uncertainty. Do not present nominal capacity, effective power, and nameplate capacity as equivalent source measurements. Storage has a cyclic boundary condition; the inventory does not grant free initial/final stored energy.

## Costs and representative distribution inputs

Prepared cost dictionaries use technology keys `wind`, `solar`, `nuclear`, `battery`, `electrolyzer`, `fuel_cell`, and `h2_storage`. Generation/conversion `capital` values are annualized USD/MW/year; storage energy costs are annualized USD/MWh/year. Battery costs split `capital_power` and `capital_energy`; `marginal` uses USD/MWh, and conversion `eff` is a fraction. Retain `_provenance` and `_basis` annotations even though underscore-prefixed metadata is removed for numerical evaluation.

`config.json` distribution settings describe a representative circuit: feeder voltage and peak kW, line resistance/reactance per km and section length, voltage band, reactive-control power factor/deadband, line-upgrade impedance factor and annual per-feeder cost, and normal/upgraded amp ratings. These are study assumptions, not a census of physical feeders. The model scales representative-feeder intervention costs using scenario peak demand. Editing feeder calibration or costs changes the experiment.

## Original preprocessing dependencies

The following script paths are relative to `preprocessing/` in this release and preserve the original workflow layout. They are archival preparation code and expect the original neighbouring input directories; the public runner’s `--pack`/`--shared` flags do not automatically relocate preprocessing dependencies. Scripts must be run only in a new working copy after supplying and checking the excluded raw inputs. Some stages overwrite prepared pack files. The table is a dependency guide, not a claim that the code-only checkout is a complete downloader.

| Stage and original script | Required excluded inputs | Output and important boundary |
|---|---|---|
| US/South African baseline: `experiment/scripts/build_inputs.py` | US `raw/demand_2023.csv`, `zone_centroids.csv`, `plant_zone_matches.csv`, `generator_inventory_with_zones.csv`; `shared/raw/eia860_storage_2023.csv`. South Africa: `raw/demand_ref_2023_modelled.csv`, `provinces.geojson`, `wri_south_africa.csv`, and the code's explicit report transcriptions. | Demand, node, inventory, assumption, and config files. This stage writes a legacy full-mesh network; it must be followed by the intended zonal-network construction. It does not acquire or recreate all upstream US spatial joins. |
| US/South African zonal network: `experiment/scripts/build_zonal_network.py` | Prepared baseline nodes and archived `raw/topology/` extractions, reports, project mappings, and derived South African line ratings. | Study-style v2 network plus separately labelled full-mesh comparison networks. Original raw GIS and every mapping are not supplied by a report citation. |
| Australia: `experiment/scripts/build_country_pack.py australia` | `raw/PRICE_AND_DEMAND_2023*_<REGION>.csv`, `nem-registration-and-exemption-list.xlsx`, `PUBLIC_DVD_DUDETAILSUMMARY_202312010000.CSV`, network report evidence. | Five-zone pack, DUID/fleet reconciliation, hourly demand, explicit network assumptions. |
| New Zealand: `experiment/scripts/build_country_pack.py new_zealand` | `raw/2023*_Grid_export.csv`, `NetworkSupplyPointsTable.csv`, `DispatchedGenerationPlant.csv`, `2023*_Generation_MD.csv`, TPR evidence. | Five-zone offtake, fleet and hydro-energy aggregation; POC mappings and clock handling are essential. |
| Mexico collection: `experiment/scripts/fetch_cenace_demand.py`; build: `experiment/scripts/build_country_pack.py mexico` | Daily `raw/demanda_real/demanda_real_balance_2023-*.csv` and `raw/prodesen24-38A1.pdf`. Extraction uses `pdftotext`. | Nine-region demand/inventory/network. Fetching current latest settlements may differ from the archived series. |
| Brazil: `experiment/scripts/build_country_pack.py brazil` | `raw/CURVA_CARGA_2023.csv`, `CAPACIDADE_GERACAO.csv`, monthly `GERACAO_USINA-2_2023_*.csv`, interchange and PEN evidence. | Four-subsystem pack and hydro-energy budgets. Mutable generation-register updates must be pinned. |
| Weather/resource profiles: `experiment/scripts/build_weather.py` | Prepared `nodes.csv`, `demand.csv`, `shared/raw/NREL_Reference_4MW_150_powercurve.csv`, and cached `raw/weather/<node-id>.json`. | `profiles/*.csv` and provenance JSON. Missing weather caches trigger external downloads; archive queries and response hashes. Default invocation covers only the original four systems, so name additional regions explicitly. |
| Feeder preparation: `experiment/scripts/calibrate_feeders.py` | Prepared regional config and numerical distribution dependencies. | Explicit representative-feeder calibration; preserve study parameters and record changes instead of silently recalibrating an archived pack. |
| Future extraction: `prediction_local/prepare_nyiso.py`, `prediction_local/prepare_all.py` | Original forecast workbooks/PDFs/ZIPs under `prediction_documents/`, source indexes, the prepared 2023 packs, and source audit/extraction metadata. | Zonal annual targets and source coordinates/hashes. Extraction uses `openpyxl`, `pyxlsb`, and `pypdf` in addition to the relevant baseline dependencies. |
| Future pack construction: `prediction/scenarios.py`, invoked by `prediction_local/prepare_all.py` | Prepared base packs; `prediction_documents/derived/summary_2030_2035.csv`; scenario and forecast JSON specifications with citations and hashed source dependencies. | New packs with scaled demand and prediction manifests. Existing output directories are not silently overwritten. A forecast workbook alone is insufficient. |
| Verification: `experiment/scripts/verify_inputs.py`, `prediction_local/verify_all.py` | Prepared files and matching input/source manifests. | Checksums and structural/annual-energy checks. Writing a new manifest after changing inputs records a new scenario; it does not prove equality to the study. |

Original future pack names are `prediction_local/packs/nyiso_<year>_official` for NYISO and `prediction_local/packs/<region>_<year>_baseline` for the other seven regions, where `<year>` is 2030 or 2035. The `growth: baseline` label can coexist with different publisher-specific scenario names, such as MISO Current Trajectory, AEMO Step Change, Transpower Medium, and ESRG REF.

## Verification before claiming reproduction

1. Pin each source edition, retrieval date, file SHA-256, extraction sheet/cell/table, and mapping revision. Preserve source licenses and attribution without bundling data that are outside the release scope.
2. Verify identifiers and counts, unique aligned 8,760-hour profiles, finite/nonnegative demand, bounded resource factors, and each zone's annual GWh. Verify future non-demand columns match the baseline and energy targets match their manifest within the stated tolerance.
3. Verify inventory conservation, reported versus assumed asset locations, direction-specific network constraints, fixed policy, cost derivations, and case-specific project-year eligibility.
4. Load inputs with the intended numerical runtime and record software, solver, threads, tolerances, source hashes, and scenario hashes. Different code/runtime snapshots can change provenance even when high-level filenames agree.
5. Treat a short-window smoke test as an integration check. It does not establish annual reliability or reproduce a completed annual study trajectory. New model runs can differ because the study contains one trajectory per model/system/year/architecture cell.

Without the excluded prepared packs and their complete source archive, this release supports inspecting the method and constructing new inputs. It does not by itself provide byte-identical input reconstruction or exact reproduction of published numerical results.

## Final future-resource preparation

After demand extraction and correction, use [NASA preparation](../preprocessing/nasa_climate/README.md). Final future packs are `nasa_climate/packs/MPI-ESM1-2-HR/ssp245/<year>/<region>`. They include `climate_manifest.json`, `prediction_manifest.json`, profile/provenance hashes and copies of source metadata. NASA daily constraints and ERA5 intraday templates produce synthetic resource profiles while the inherited demand, network and fleet remain fixed.
