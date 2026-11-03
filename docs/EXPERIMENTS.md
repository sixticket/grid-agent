# Primary carbon cohort

The current primary study comprises 40 carbon-constrained trajectories. The ten fixed cases and source mappings are in `experiments/carbon/cases.json`; use `--co2` with the portable launchers. Both hosted deployments use 64,000 output tokens for every carbon case. The historical 2023 Opus reference instead retains 16,000. The configuration below documents the 96-trajectory loose-cap reference cohort.

# Experiment configurations and reporting scope

## Reported matrix

The active manuscript contains 96 trajectories: MISO, ERCOT, NYISO, South Africa, Australia, New Zealand, Mexico and Brazil; 2023, 2030 and 2035; GPT-6.1 Sol and Claude Opus 5.5; single and multi agent. Each selected committed outcome passes the implemented system and representative-feeder acceptance checks. There is one recorded trajectory per cell.

| Cohort | Cases | Demand / resource weather | Default output ceiling |
| --- | ---: | --- | ---: |
| GPT-6.1 Sol 2023 | 16 | 2023 / ERA5 2023 | 64,000 |
| GPT-6.1 Sol 2030/2035 | 32 | Target year / matching NASA year | 64,000 |
| Opus 5.5 2023 | 16 | 2023 / ERA5 2023 | 16,000 |
| Opus 5.5 2030/2035 | 32 | Target year / matching NASA year | 64,000 |

The 80 later hosted cases combine Sol's 48 and Opus's 32 future cases. The retained Opus 2023 cohort is separate. Earlier GPT-6 Sol, Qwen and future-demand/ERA5-weather mismatches are excluded.

Runs use inherited assets (`brownfield`), baseline policy, 8,760 chronological hourly intervals, high reasoning effort, up to 120 multi cycles or 120 single turns and 40 system solves. The study uses Gurobi, 18 threads, a requested 0.1% MIP gap and an 86,400-second per-solve guard. Role-specific output ceilings can be smaller than the default. New runs record actual solver, inference settings and hashes.

## Runtime routing and provenance

Sol uses `weather_prediction/gpt_6_1_sol` snapshots. Future Opus uses `weather_prediction/claude_opus_5_5`; Opus 2023 preserves the `corrections/20260925-v2/claude_opus_thinking_v1` baseline runtime. Model READMEs identify their release paths.

Only South Africa 2030 uses the calendar-aware runtime. A project's availability year must not exceed the planning year: a project available in 2033 cannot be built for 2030. All other cases retain their original project permissions. The climate adapter validates NASA inputs and exposes target-year resource metadata while retaining the 2023 hourly index. It does not extend calendar filtering to other cases or change numerical equations.

`SOURCE_MANIFEST.json` distinguishes byte-identical copies from portable path/CLI adaptations. `RELEASE_FILES.json` hashes the complete release. Historical trajectories can include recovery/retry revisions; their original execution manifests remain necessary for exact replay. A public wrapper's new hash does not replace a recorded historical hash.

## Future inputs

Published demand outlooks set annual zonal targets that scale the corresponding 2023 hourly demand template. NASA NEX-GDDP-CMIP6 v2.0 supplies daily minimum/maximum temperature, shortwave radiation and surface wind. The paper uses MPI-ESM1-2-HR / r1i1p1f1 / SSP2-4.5. ERA5 intraday templates reconstruct synthetic hourly weather. PySAM PVWatts v8 and the turbine curve produce resource capacity factors.

The timestamps remain on the aligned 2023 clock; `weather_year` and `planning_year` identify 2030 or 2035. Fleet, cost and policy assumptions remain inherited. Demand is not recomputed from the new weather. Demand-only builders are the intermediate step before NASA resources are attached. Preprocessing supports three optional GCMs; only MPI-ESM1-2-HR contributes to the manuscript.

## Opus protocol and recovery

Opus retains the recorded thinking-block protocol, including `thinking-binding-controls-2026-08-01` and `prefix_mismatch_behavior = "drop_block"`. Response journals preserve provider transformations. Retry adapters handle transient transport/provider errors. Original history replay verifies pinned responses and execution events before requesting new work.

Public launchers start new runs. Campaign retries preserve earlier attempt directories. Preserved campaign/recovery reference sources are distinguished from executable portable commands; exact historical replay requires original manifests and journals.

## Analysis

`analysis/` selects the 96-case cohort, checks committed accepted results and produces result and search/token summaries from external archives. Complete histories count repeated context per request and deduplicate copied recovery responses. Input includes cache reads/writes; reasoning is a subset of output and is not added twice. Paper summaries omit API monetary comparisons.

Physical acceptance, retained annual system cost and the stopping decision are separate outputs. Selection retains the lowest-cost physically accepted committed design. Solver bounds and execution conditions accompany comparisons. Observed trajectories are not repeated-seed distributions or certified global optima.
