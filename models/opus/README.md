# Claude Opus 5.5

Model `claude-opus-5-5`, adaptive thinking with high effort, standard service tier, 5-minute cache. Set `ANTHROPIC_API_KEY` in the environment. The current paper reports 48 cases.

| Cases | Runtime | Default output tokens |
| --- | --- | ---: |
| 2023, all regions/architectures | `runtime_baseline2023` | 16,000 |
| 2030/2035 except South Africa 2030 | `runtime_original` | 64,000 |
| South Africa 2030 | `runtime_calendar` | 64,000 |

The baseline preserves `corrections/20260925-v2/claude_opus_thinking_v1`. Future engines come from `weather_prediction/claude_opus_5_5`. Numerical source is preserved; future provider adapter lookup is adjusted for the release layout. Source hashes and adaptations are explicit.

Use root `tools/run_case.py` or `tools/run_campaign.py`: default plan, offline `--check-inputs`, real `--execute`. Future cases require matching NASA packs and `GRID_DATA_ROOT` identifies their external provenance root. The climate adapter exposes target-year metadata while preserving the 2023 hourly clock and case-specific physical route.

The provider retains `thinking-binding-controls-2026-08-01` and `prefix_mismatch_behavior = "drop_block"`. `opus_protocol.py` verifies response/transformation journal coverage. Role-specific output ceilings remain in the agent source.

`campaign_source` contains original controller/worker/preflight/probe references. Preserved history/recovery logic requires original pinned manifests, journals and paths; the portable campaign helper starts fresh attempts and preserves earlier files. This release does not mutate or restart the completed research campaign. See [EXPERIMENTS.md](../../docs/EXPERIMENTS.md).
