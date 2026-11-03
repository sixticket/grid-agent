# Archived Qwen implementation

This directory preserves the earlier Qwen experiment source for provenance. Qwen is excluded from the current 96-case manuscript matrix and is not supported by the current case/campaign launcher.

`model.json`, both historical physical runtimes, adapters and locked dependencies are retained. Direct historical execution requires the original input packs, pinned archive and local serving configuration; these files do not claim to replay interrupted runs in a fresh checkout. `tools/serve_qwen.py` is an optional server planning helper. Its default only prints a command and does not start a server or download weights.

Use `tools/run_case.py` or `tools/run_campaign.py` from the repository root for the active Sol/Opus experiments. See [the release overview](../../../README.md) and [reproducibility notes](../../../docs/REPRODUCIBILITY.md).
