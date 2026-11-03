# GPT-6.1 Sol

Model `gpt-6.1-sol`, Responses API, high reasoning effort, 64,000 default output tokens with smaller role-specific ceilings. Set `OPENAI_API_KEY` in the environment. All 48 Sol cases in the current paper use this deployment.

`runtime_original` and `runtime_calendar` copy the latest `weather_prediction/gpt_6_1_sol` snapshots. Only South Africa 2030 uses the calendar runtime. Numerical algorithms, prompts and acceptance logic are preserved; provider adapter lookup is adjusted for the release layout and recorded in `SOURCE_MANIFEST.json`.

Use `tools/run_case.py` or `tools/run_campaign.py` from the repository root. Defaults print plans. Input checking is offline; execution requires `--execute`. Baseline packs use ERA5 2023; future packs require MPI-ESM1-2-HR / SSP2-4.5 NASA metadata and hashes. Set `GRID_DATA_ROOT` for the external provenance root.

`climate_adapter.py` attaches target-year metadata without changing physical equations. `adapters/request_retry.py` preserves transport/provider retries and response accounting. History adapters retain the original replay logic. `campaign_source` holds source references for the original controller/worker/preflight/API probes; these are not the portable public commands. Their original recovery records pin particular job paths and exact historical source/input hashes. Public campaign retries start fresh attempts and preserve earlier attempts; they do not silently reinterpret an interrupted historical trajectory as a new run.

Recorded dependencies accompany each runtime. New executions record resolved model/settings and input/source hashes. See [EXPERIMENTS.md](../../docs/EXPERIMENTS.md).
