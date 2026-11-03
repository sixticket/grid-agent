# Reproducing experiments and paper analysis

The portable source tree contains current scientific engines, inference/climate adapters, preparation scripts, launchers, cohort settings, paper analysis source and original-to-release hashes. The companion research release supplies archived inputs, source reports, completed outcomes, dispatch files, API journals and available paper material; see [RESEARCH_PACKAGE.md](RESEARCH_PACKAGE.md). Credentials, virtual environments and model weights are excluded.

## Carbon-constrained study

The primary cohort contains 40 trajectories. Use `--co2` with either portable launcher, and the builders in [analysis/carbon](../analysis/carbon/README.md). The 96-case analysis below is the historical reference cohort.

## New experiments

1. Supply baseline and shared inputs described in [INPUT_DATA.md](INPUT_DATA.md).
2. For future cases, extract annual demand targets, build demand-only intermediates, then use `preprocessing/nasa_climate` to reconstruct resources and build matching NASA packs.
3. Inspect `tools/run_case.py` with explicit pack/shared paths. `--check-inputs` validates structure, matching metadata and present manifests; `--execute` starts a scientific run.
4. Preserve resolved settings, input/source hashes, journals, solver bounds, committed candidates and acceptance checks. `tools/run_campaign.py` runs selected cases sequentially.

The reference matrix has 96 cases: eight systems, three years, two deployments and two architectures. ERA5 supplies 2023 resources; 2030/2035 use MPI-ESM1-2-HR / SSP2-4.5 daily projections reconstructed using ERA5 intraday templates. The profile clock remains 2023. Opus 2023 and later hosted runs use different preserved inference ceilings/runtime sources.

## Historical paper reconstruction

Use the external archive and contracts in [analysis/README.md](../analysis/README.md). Analysis verifies committed evidence, extracts hosted80, combines retained Opus 2023 controls, selects the final 96 cases and rebuilds tables and supported figures. Frozen upstream evidence retains provenance; excluded cohorts are not silently included in totals.

Exact response replay additionally needs original source/input manifests and recovery records. Portable adaptations are explicitly recorded. Model READMEs distinguish public launchers from campaign/recovery reference sources.

Preparation needs curated plant allocations, topology drafts, cost dictionaries, extracted forecast records and geographic mappings. Publisher links alone do not supply these transformations. Preserve source bytes, coordinates, clocks, software versions and hashes with reconstructed inputs. New model responses and solver outcomes can differ even with matching inputs.

## Packaging verification

Hygiene, manifest integrity and standard-library unit tests run offline in CI. Optional scientific checks require their dependencies. Packaging does not restart experiments, call model APIs, run annual optimization or redownload data. Annual numerical reproduction remains a separate run with supplied inputs and the recorded inference/solver environment.
