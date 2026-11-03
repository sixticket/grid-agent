# Complete research package

The [research-2026-11-03 release](https://github.com/sixticket/grid-agent/releases/tag/research-2026-11-03) supplements the Git source tree with the material retained for writing and auditing the paper. The latest working manuscript, bibliography, journal styles and referenced figures are also versioned under [manuscript](../manuscript/README.md).

## Download and verify

```bash
gh release download research-2026-11-03 --repo sixticket/grid-agent \
  --dir /path/to/research-assets
python -B tools/verify_research.py --root /path/to/research-assets
mkdir -p /path/to/research-archive
for archive in /path/to/research-assets/research-archive-*.tar.gz; do
  tar -xzf "$archive" -C /path/to/research-archive
done
cp /path/to/research-assets/RESEARCH_FILES.json /path/to/research-archive/
cp /path/to/research-assets/PACKAGE_ORIGIN.json /path/to/research-archive/
python -B tools/verify_research.py --root /path/to/research-archive --extracted
```

Each numbered tarball is independent; extract **all** parts into the same directory. `RESEARCH_ASSETS.json` lists asset hashes and sizes. `RESEARCH_FILES.json` lists every archived research file, its original byte hash and containing asset. `SHA256SUMS` is also provided. The original complete supplied manuscript directory is preserved separately in `manuscript-source.tar.gz`; it can be extracted into a separate working directory.

## Included material

| Archive location | Contents |
| --- | --- |
| `experiment/` | Eight regional input libraries, hourly profiles, raw data, cost/policy sources, baseline engine, preparation and validation code |
| `nasa_climate/` | Downloaded projection data, processed/hourly resources, profiles, packs, provenance and preparation scripts |
| `prediction_documents/`, `prediction_local/`, `prediction/` | Forecast reports, source workbooks, extracted demand targets and preparation inputs/code |
| `weather_prediction/` | Original hosted runtimes, future input packs, campaign records, response/usage journals and dispatch checkpoints |
| `corrections/` | Corrected runtime and input records, baseline campaign history, recovery records and dispatch supplements |
| `co2_constraint/` | Fixed budgets, original controller/worker, all four carbon campaigns, execution revisions, selected results and dispatch/response evidence |
| `Nature_Energy/` | Reference-cohort manuscript sources, bibliography, tables, figures, source catalogues, 96-case evidence and search/token analysis |
| `figure_carbon/` | Forty-case data, figures, maps/atlases, expert-plan comparison, revision fragments and audits |
| Other provenance paths | Additional existing source files referenced by the retained evidence |

The archive preserves original scientific artifact bytes, including recorded absolute paths. `PACKAGE_ORIGIN.json` maps the original research root; the portable carbon builders resolve it automatically. Reference analysis tools accept explicit archive prefix mappings. Generated rebuild outputs should go to a new external directory.

Credentials, private-key files, virtual environments, caches, model weights and obsolete manuscript backup trees are excluded. Existing source attributions and licenses are retained. The source reports remain subject to their original terms. Hosted model access and a configured solver environment are still needed for new inference/optimization; stored outcomes and figure rebuilding do not require new model calls.

## Scientific scope

The carbon cohort contains ten system–year cases and all four deployment/architecture outcomes per case. The reference cohort contains the complete 96 year-matched trajectories, with earlier excluded cohorts retained only where required by provenance. Original attempts and execution revisions are preserved so final accepted candidates can be distinguished from intermediate trials. The manuscript's reported all-in costs, acceptance checks, resource years and model settings are not changed by packaging.

The original 96-case `Nature_Energy` manuscript predates the carbon revision. Continue writing from `manuscript/1_Nature.tex` in Git, or the complete supplied `manuscript-source.tar.gz`.
