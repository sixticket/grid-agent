# Reconstructing the 96 reference trajectories

The current primary carbon cohort and its figures are documented in [carbon/README.md](carbon/README.md). This directory retains the 96-case loose-cap reference analysis.

These tools read completed archives. They do not call model APIs, optimize capacity or run power flow. The release supplies source and cohort metadata; results, logs, ledgers, dispatch arrays, workbooks and map data are external.

`cohort_specification.json` identifies the active matrix and historical snapshot hashes. There are 48 GPT-6.1 Sol and 48 Opus cases, with 32 ERA5 2023 and 64 matching NASA 2030/2035 outcomes. Selection retains the lowest-cost physically accepted committed design. Earlier GPT-6 Sol/Qwen and mismatched weather cases are excluded.

## External archive contract

A paper evidence root contains `evidence/base2023_20261005/results.json`, `evidence/completed_weather_20261003/results.json` and their upstream manifests, plus any frozen reporting/figure snapshots used. The raw experiment archive contains `weather_prediction/`, `corrections/`, `experiment/`, NASA and forecast inputs. Result records reference the original `final.json`, `log.jsonl`, API usage/response journals, implementation manifests and input hashes.

Set `GRID_AGENT_EVIDENCE_ROOT` for frozen paper snapshots. Commands accept explicit paths. `--archive-root` roots relative references; repeat `--path-map OLD=NEW` or supply `--path-map-json` for relocated absolute prefixes. No basename guessing occurs. Original files and manifests remain untouched. Default output is a temporary `grid-agent-analysis` directory; set `--out` or `GRID_AGENT_ANALYSIS_OUT` for a durable location outside the source checkout.

```bash
export GRID_AGENT_EVIDENCE_ROOT=/path/to/archive/Nature_Energy
export GRID_AGENT_ANALYSIS_OUT=/path/to/rebuilt-analysis

python analysis/select_reported_cohort.py
python analysis/build_main_results.py \
  --evidence "$GRID_AGENT_ANALYSIS_OUT/evidence/reported96_20261006/results.json"
python analysis/render_results_tables.py --help
python analysis/analyze_rounds.py --evidence-root "$GRID_AGENT_ANALYSIS_OUT" \
  --archive-root /path/to/archive
python analysis/analyze_tokens.py \
  --evidence "$GRID_AGENT_ANALYSIS_OUT/evidence/reported96_20261006/results.json" \
  --archive-root /path/to/archive
python analysis/build_search_effort.py \
  --evidence "$GRID_AGENT_ANALYSIS_OUT/search_tokens/analysis.json"
```

`analyze_rounds.py` verifies candidate hashes, final design/cost, selected evaluation, feeder rechecks and system-only probes from raw logs. `analyze_tokens.py` maps every returned response to its log role, checks cache/input decomposition and duplicate identities, and reconstructs the manuscript's 2,257 responses. Output includes tokens and search metrics; API monetary fields are omitted. Input includes cache reads/writes, output includes reasoning, and recovery copies are not counted twice.

`build_weather_results.py --root /path/to/archive/weather_prediction --out /path/to/rebuilt-analysis` audits the hosted80 queue, pinned runtime/input identities, committed histories, ledger coverage and physical acceptance. It also renders weather tables/figures and needs Matplotlib. `--evidence` renders a frozen hosted80 snapshot. `build_main_results.py`, cohort selection, round/token reconstruction and table builders use the standard library.

## Paper figure and expert-plan source

`manuscript/` preserves the current figure and expert comparison calculations. Set `GRID_AGENT_MANUSCRIPT_ROOT` to a **separate writable manuscript workspace** with copied external evidence under `evidence/`, and `GRID_AGENT_ARCHIVE_ROOT` to the read-only experiment archive. `GRID_AGENT_ARCHIVE_MAP` is an optional JSON old-prefix/new-prefix object. These builders write figures, LaTeX fragments and audit records to that workspace, never to the experiment archive. They require the geographic/expert/dispatch evidence named in their source; missing inputs are not fabricated.

Typical sequence is `build_expert_comparison.py`, `build_geographic_figures.py` (with its explicit project root), `extract_best_regional_designs.py`, `build_professional_artwork.py`, and `build_publication_figures.py`. `build_figures.py` supplies baseline comparisons. All read existing results. `requirements.txt` lists scientific drawing/extraction dependencies; Arial or a deliberate replacement is needed for identical typography.

Figure 1's exact editable PowerPoint source is `manuscript/build_framework_ppt.mjs`, with `prepare_framework_geometry.py` and `build_framework_artwork.py`. This original authoring path additionally requires an externally installed `@oai/artifact-tool` and presentation validation tools. Set `RUNTIME_NODE_MODULES`, `PRESENTATIONS_SKILL_DIR`, `RUNTIME_NODE`, `RUNTIME_PYTHON` as needed. PDF/SVG export uses LibreOffice (`SOFFICE`) and Poppler (`PDFTOPPM`). These components are not vendored or installed by this release. The other Matplotlib figures can be rebuilt without this presentation runtime.

Archived absolute paths are explicitly remappable. Historical snapshot byte hashes can differ after a relocated/sanitized rebuild; preserve scientific values, case identities and raw-source hashes. Exact replay of an interrupted original agent requires its original pinned runtime/recovery records, separate from these analysis tools.
