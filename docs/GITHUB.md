# Repository and release maintenance

The repository is [sixticket/grid-agent](https://github.com/sixticket/grid-agent). The initial research snapshot uses tag `research-2026-11-03`; large supporting material is attached as release assets, each with a SHA-256 digest.

Run source checks and unit tests before committing. Keep environments, generated outputs and research archives outside this source tree. `SOURCE_MANIFEST.json` preserves origin identities; `RELEASE_FILES.json` covers every source-tree file except itself and Git internals. Regenerate release hashes after intentional source edits.

The research package contains original scientific artifacts, while portable source adaptations live in Git. Do not overwrite archived outcomes with reruns. New runs need new output directories and retain source/input hashes. Repository visibility and the first-party license remain the owner's choices.
