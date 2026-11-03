# Grid planning agents

Research code for electricity-system planning with **GPT-6.1 Sol and Claude Opus 5.5**. Agents propose investment and operating bounds, PyPSA solves capacity expansion and chronological dispatch, and OpenDSS checks representative distribution feeders. Numerical acceptance determines which committed designs can be retained.

This release follows the manuscript's **96 reported trajectories**: eight systems × three matching demand/resource-weather years × two deployments × single/multi agent. It includes the latest experiment engines, NASA weather preparation, portable launchers and paper analysis source.

| Demand / resource-weather condition | GPT-6.1 Sol | Opus 5.5 | Total |
| --- | ---: | ---: | ---: |
| 2023 / ERA5 2023 | 16 | 16 | 32 |
| 2030 / NASA 2030 | 16 | 16 | 32 |
| 2035 / NASA 2035 | 16 | 16 | 32 |
| Total | 48 | 48 | 96 |

NASA NEX-GDDP-CMIP6 v2.0 daily projections use **MPI-ESM1-2-HR, r1i1p1f1, SSP2-4.5**. ERA5 2023 supplies intraday templates for synthetic hourly resources. The profile clock remains 2023; resource-weather and planning years are recorded separately. Earlier future-demand runs retaining ERA5 2023, GPT-6 Sol runs and Qwen runs are outside the active reporting cohort.

This is a **code-only release**. Regional input packs, raw documents, weather data, completed results, API journals, credentials and model weights are external. The source references and preparation scripts require curated intermediate inputs; a fresh checkout alone cannot reconstruct the exact archived study inputs.

## Repository layout

| Path | Contents |
| --- | --- |
| [models/sol](../models/sol/README.md) | Current GPT-6.1 Sol engines and adapters |
| [models/opus](../models/opus/README.md) | Future Opus engines and separate 2023 baseline runtime |
| [tools/run_case.py](../tools/run_case.py) | Portable single-case launcher; prints a plan by default |
| [tools/run_campaign.py](../tools/run_campaign.py) | Sequential matrix launcher for new cases |
| [preprocessing](../preprocessing/README.md) | Baseline inputs and regional demand outlook extraction |
| [preprocessing/nasa_climate](../preprocessing/nasa_climate/README.md) | NASA download, hourly reconstruction, pack building and audit |
| [analysis](../analysis/README.md) | Evidence extraction, 96-case selection, results and search/token analysis |
| [docs/EXPERIMENTS.md](EXPERIMENTS.md) | Recorded settings and runtime routing |
| [docs/INPUT_DATA.md](INPUT_DATA.md) | External input contract |
| [SOURCE_MANIFEST.json](../SOURCE_MANIFEST.json) | Original source identities, hashes and release adaptations |
| [RELEASE_FILES.json](../RELEASE_FILES.json) | Complete release file hashes |

Qwen sources under `models/legacy/qwen` are archival. They do not contribute to the 96-case study or active hosted launchers.

## Install

Use Python 3.11 or newer and an environment outside the checkout:

```bash
python3.11 -m venv ../grid-agent-venv
source ../grid-agent-venv/bin/activate
python -m pip install -r models/sol/requirements.txt
```

Recorded dependency snapshots accompany each runtime. Study runs use Gurobi with a separately configured license. HiGHS is available for independent runs, whose numerical outcomes may differ. NASA preprocessing and optional paper figures have additional dependencies in their directories. Lock snapshots describe the research environment; installation on every platform is not implied.

Export `OPENAI_API_KEY` for Sol or `ANTHROPIC_API_KEY` for Opus. [.env.example](../.env.example) contains empty variable names; launchers do not load that file. Hosted runs use the SDK's default endpoints.

## Prepare and inspect a case

Set `GRID_DATA_ROOT` to an external directory containing `experiment/`, `prediction_local/`, `prediction_documents/`, `corrections/` and `nasa_climate/` as needed. Preparation uses this root; the default is `data/` under the checkout. Read the [preparation guide](../preprocessing/README.md) before running builders.

```bash
export GRID_DATA_ROOT="$(pwd)/../grid-agent-data"
python tools/run_case.py \
  --deployment sol --region miso --year 2030 --agent single \
  --pack "$GRID_DATA_ROOT/nasa_climate/packs/MPI-ESM1-2-HR/ssp245/2030/miso" \
  --shared "$GRID_DATA_ROOT/experiment/shared" \
  --output runs/sol/miso_2030_single
```

This prints a plan. Add `--check-inputs` to validate prepared files without inference or optimization. Add `--execute` to start a real model/solver run. A 2023 case uses `experiment/<region>`. Future cases require matching NASA packs; demand-only intermediates are insufficient.

Opus 2023 retains its original 16,000-token default. The later hosted campaign uses 64,000 with smaller role-specific ceilings. South Africa 2030 uses the calendar-aware runtime. New executions use fresh output directories and record input/source hashes and status. Historical replay needs the original pinned archive; see the model READMEs.

## Offline checks and publication

These checks do not call models, run solvers or download data:

```bash
python -B tools/check_release.py --manifest SOURCE_MANIFEST.json
python -B tools/check_release.py --manifest RELEASE_FILES.json
python -B -m unittest discover -s tests -v
```

Run hygiene checks on the clean code-only tree. They reject datasets, caches, credentials, machine-specific paths and stale hashes even when Git ignores those files. CI runs the same checks. See [REPRODUCIBILITY.md](REPRODUCIBILITY.md) and [GITHUB.md](GITHUB.md). No remote repository is created by packaging. The source project supplied no first-party code license; the copyright holder can choose one before granting reuse rights. Attribution is in [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).
