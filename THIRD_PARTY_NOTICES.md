# Third-party software and data

The research code uses separately installed software including PyPSA, Linopy, HiGHS or Gurobi, NREL PySAM, OpenDSSDirect.py, NumPy, pandas, SciPy, the OpenAI/Anthropic SDKs, and (for optional legacy Qwen serving) vLLM and its CUDA dependencies. Their own licenses govern those packages. Dependency source or binary distributions are not vendored here.

Qwen weights and tokenizer/template assets are obtained separately from the named model repository at the recorded revision. No model weights are included.

Input data and reports belong to their respective providers. [DATA_SOURCES.md](docs/DATA_SOURCES.md) identifies the sources used by the study. A source citation is not a grant of redistribution rights. The Git source tree omits datasets and raw documents; the companion research archive preserves the study materials and their existing attribution and license files. Their original terms continue to apply.

The private research input tree contains South African spatialization material credited to Gregory Ireland and a separately attributed topology replication utility. Those materials retain their original notices in the supporting archive. The upstream data notice is not used as a license for this repository's first-party code.

No root-level first-party code license was present in the source project when this release was assembled. No new license is assigned by this preparation. The copyright holder can add the intended code license before publication or subsequent reuse.

NASA NEX-GDDP-CMIP6 and ERA5 underpin the future-resource preparation. NASA NetCDF subsets, ERA5 responses and derived regional profiles are externally obtained and retain their source terms. Figure 1 authoring additionally references an external artifact-tool/presentation runtime; no runtime package or private validation-tool implementation is vendored. Natural Earth supplies public-domain geographic context.
