# Release validation (2026-11-03)

- 47 tests passed under the recorded Python 3.11 scientific environment, including a solved tiny linear program showing that tightening the carbon budget changes feasible fossil dispatch.
- The standard-library-only suite passes with the optional solver test skipped.
- All 20 carbon input/runtime combinations (ten cases × two deployments) passed portable input and source validation.
- Re-extracted `carbon_data.json` matches the original parsed JSON exactly for all 40 trajectories.
- All five expert-plan capacity comparisons match the original values.
- All ten geographic selections and paired dispatch/emissions maps rebuilt successfully; maximum aggregate CO₂ reconciliation error was below 4e-12 Mt.
- All 19 figure references in the supplied manuscript resolve; Tectonic compiled `1_Nature.tex` successfully. Existing TeX underfull-box and PDF-version warnings remain; scientific text and figure artwork were preserved.
- No new model API calls or full annual optimization were run for packaging.

Source hashes and credential/path hygiene are checked independently. Research asset and per-file hashes accompany the supporting archive.
