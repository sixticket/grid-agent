# Carbon-constrained study

The primary cohort is ten fixed system–year cases × two deployments × single/multi architecture, totalling 40 trajectories. `cases.json` records exact budgets and their source-to-model mappings. `co2_adapter.py` is copied byte-for-byte from the completed experiment.

The adapter overlays the annual operational CO₂ ceiling after original input validation and before agent imports. It preserves input files, blocks cap changes through the agent design space, and adds a separate acceptance gate checking emissions, horizon, cap and source identity. The PyPSA constraint scales the annual budget by modelled hours / 8760.

Use `python -B tools/run_case.py --co2 ...` or `python -B tools/run_campaign.py --co2 ...` from the repository root. Both plan by default. Hosted runs use a 64,000-token ceiling, and only South Africa 2030 uses the calendar-aware runtime. The older Opus 2023 reference has a different 16,000-token ceiling.

`archive_controller/` preserves the original controller, worker and pause/queue implementation for methods review. Those sources expect the original research tree and are **not standalone release launchers**. Their complete surrounding source, configuration and runtime records are included in the research archive. Use the portable tools for new runs; historical recovery requires the original pinned environment and execution history.

The budgets include allowance allocations, planning constraints and emissions pathways, with their different meanings recorded in the catalogue. Mexico and Brazil use CO₂e source numbers as explicit direct-CO₂ model proxies. The reported scope excludes lifecycle and imported-power emissions. Read each case's mapping and caveats before interpreting the ceiling.
