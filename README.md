# Grid planning agents

Research software and supporting material for agent-guided electricity-system planning. GPT-6.1 Sol and Claude Opus 5.5 propose designs; PyPSA evaluates chronological capacity expansion and dispatch, and OpenDSS checks representative distribution feeders.

This repository covers the **40 carbon-constrained trajectories** used in the current study and the **96 loose-cap reference trajectories** used for historical comparison. The two cohorts have separate selection rules and inference settings.

| Cohort | Systems / cases | Deployments × architectures | Trajectories |
| --- | --- | --- | ---: |
| Carbon-constrained | 5 systems, 10 system–year cases | 2 × 2 | 40 |
| Loose-cap reference | 8 systems × 2023, 2030, 2035 | 2 × 2 | 96 |

Carbon cases cover NYISO (2023/2030/2035), MISO (2030/2035), South Africa (2030/2035), Mexico (2030), and Brazil (2030/2035). Exact budgets, official sources, units and mapping qualifications are in [cases.json](experiments/carbon/cases.json). The research ceiling is fixed outside the agent design space and independently audited at acceptance.

## Contents

| Directory | Purpose |
| --- | --- |
| [models/sol](models/sol/README.md), [models/opus](models/opus/README.md) | Preserved numerical engines and portable provider adapters |
| [experiments/carbon](experiments/carbon/README.md) | Fixed carbon cases, overlay and acceptance gate |
| [tools](tools/run_case.py) | Plan, validate and launch new experiments |
| [preprocessing](preprocessing/README.md) | Regional input and future-demand preparation |
| [preprocessing/nasa_climate](preprocessing/nasa_climate/README.md) | NASA weather processing and input-pack validation |
| [manuscript](manuscript/README.md) | Latest supplied LaTeX manuscript, bibliography, journal style and figures |
| [analysis/carbon](analysis/carbon/README.md) | Current carbon figures, maps and expert-plan comparisons |
| [analysis](analysis/README.md) | Reference cohort selection and analysis |
| [docs/RESEARCH_PACKAGE.md](docs/RESEARCH_PACKAGE.md) | Downloadable inputs, original runs, evidence and paper assets |
| [SOURCE_MANIFEST.json](SOURCE_MANIFEST.json) | Original source hashes and release adaptations |
| [RELEASE_FILES.json](RELEASE_FILES.json) | Source-tree file hashes |

Large research material accompanies the tagged [research release](https://github.com/sixticket/grid-agent/releases/tag/research-2026-11-03). Download the complete release assets for the archived study, including inputs, raw source material, original runtimes, result/response journals, dispatch checkpoints, evidence, and available manuscript/figure material. A Git clone contains the portable source package; the research archive is separately checksummed. See the package guide for scope and any missing source items.

## Install

Use Python 3.11 or newer and a virtual environment outside the checkout:

```bash
python3.11 -m venv ../grid-agent-venv
source ../grid-agent-venv/bin/activate
python -m pip install -r models/sol/requirements.txt
```

Recorded dependency snapshots accompany the runtimes. Study runs used Gurobi with a separately configured license. HiGHS supports independent runs; numerical outcomes may differ. Figure dependencies are in `analysis/requirements.txt`.

Export `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` for the selected deployment. The empty [.env.example](.env.example) lists the variable names; launchers do not automatically load it. Model identifiers preserve the archived study; access to those deployments is required for new inference.

## Inspect or run a carbon case

After extracting the research package into an external directory:

```bash
export GRID_DATA_ROOT=/path/to/research-archive
python -B tools/run_case.py --co2 \
  --deployment opus --region nyiso --year 2023 --agent single \
  --pack "$GRID_DATA_ROOT/experiment/nyiso" \
  --shared "$GRID_DATA_ROOT/experiment/shared" \
  --output /path/to/new-runs/opus_nyiso_2023_single
```

The default prints a plan without inference or optimization. Add `--check-inputs` to validate inputs and hashes. Add `--execute` to run the model and solver. Output directories must be new or empty.

```bash
# All 40 carbon trajectories: plan only by default.
python -B tools/run_campaign.py --co2 \
  --data-root "$GRID_DATA_ROOT" --output /path/to/new-carbon-campaign
```

Omit `--co2` for the reference cohort. Carbon runs use the later 64,000-token hosted configuration, including NYISO 2023; the historical Opus 2023 reference uses 16,000. South Africa 2030 uses the calendar-aware runtime. These differences are retained rather than interpreted as effects of the carbon cap alone. See [experiment settings](docs/EXPERIMENTS.md) and the [reference cohort guide](docs/REFERENCE_COHORT.md).

## Verification

These commands perform source, hygiene and offline unit checks:

```bash
python -B tools/check_release.py --manifest SOURCE_MANIFEST.json
python -B tools/check_release.py --manifest RELEASE_FILES.json
python -B -m unittest discover -s tests -v
```

CI runs the same checks without model credentials. The optional tiny carbon LP test runs when Linopy, pandas and HiGHS are installed. Full annual optimization is a separate scientific run. The archived evidence supports inspection of reported outcomes without rerunning inference.

[Reproducibility](docs/REPRODUCIBILITY.md), [input contracts](docs/INPUT_DATA.md), and [third-party notices](THIRD_PARTY_NOTICES.md) describe dependencies and source terms. No first-party license has been selected in the source project.
