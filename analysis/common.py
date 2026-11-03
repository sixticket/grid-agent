"""Shared portable paths and strict reporting-cohort validation."""
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile

REGIONS = ('miso', 'ercot', 'nyiso', 'south_africa', 'australia', 'new_zealand', 'mexico', 'brazil')
MODELS = ('gpt-6.1-sol', 'claude-opus-5-5')
YEARS = (2023, 2030, 2035)
BASE_SNAPSHOT = Path('evidence/base2023_20261005/results.json')
WEATHER_SNAPSHOT = Path('evidence/completed_weather_20261003/results.json')
REPORTED_SNAPSHOT = Path('evidence/reported96_20261006/results.json')

def require(value, message):
    if not value:
        raise ValueError(message)

def evidence_root():
    return Path(os.environ.get('GRID_AGENT_EVIDENCE_ROOT', 'external-evidence')).resolve()

def output_root():
    # Never write generated tables, candidate data or journals into this source package by default.
    return Path(os.environ.get('GRID_AGENT_ANALYSIS_OUT', str(Path(tempfile.gettempdir()) / 'grid-agent-analysis'))).resolve()

def read_json(path):
    return json.loads(Path(path).read_text())

def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n')

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def identity(row):
    return f"{row['model']}/{row['year']}/{row['region']}/{row.get('agent', row.get('architecture'))}"

def validate_cohort(runs):
    expected = {f'{m}/{y}/{r}/{a}' for m in MODELS for y in YEARS for r in REGIONS for a in ('single', 'multi')}
    ids = [identity(r) for r in runs]
    require(len(ids) == 96 and len(set(ids)) == 96 and set(ids) == expected, 'Expected exactly the 96 model-qualified, year-matched paper cells')
    for r in runs:
        require(r['year'] == r['demand_year'] == r['weather_year'], 'Demand/resource year mismatch: ' + identity(r))
        require(r['completed'] is True and r['selected']['acceptance']['passes'] is True, 'Incomplete or rejected reported case: ' + identity(r))
        selected = r['selected']
        high = selected['all_in_cost_billion']
        require(isinstance(high, (float, int)) and not isinstance(high, bool) and math.isfinite(high) and high > 0, 'Invalid selected annual cost: ' + identity(r))
        require(math.isclose(high, selected['system_cost_billion'] + selected['distribution_cost_billion'], rel_tol=0, abs_tol=1e-8), 'All-in cost decomposition mismatch: ' + identity(r))
    return set(ids)

def add_archive_arguments(parser):
    parser.add_argument('--archive-root', type=Path, default=evidence_root(), help='Root of relocated raw experiment archives and relative archive references.')
    parser.add_argument('--path-map', action='append', default=[], metavar='OLD=NEW', help='Repeat to remap absolute archived path prefixes; longest prefix wins.')
    parser.add_argument('--path-map-json', type=Path, help='JSON object mapping old archive prefixes to new prefixes.')

def resolver_from_args(args):
    mappings = json.loads(os.environ.get('GRID_AGENT_ARCHIVE_MAP', '{}'))
    if args.path_map_json:
        mappings.update(read_json(args.path_map_json))
    for pair in args.path_map:
        require('=' in pair, '--path-map requires OLD=NEW')
        old, new = pair.split('=', 1)
        require(old and new, 'Archive path-map prefixes cannot be empty')
        mappings[old] = new
    return ArchiveResolver(args.archive_root, mappings)

class ArchiveResolver:
    """Resolve exact prefixes without guessing or basename-based archive discovery.

    An explicit mapping takes precedence even if the old file still exists. Unmapped absolute
    references can be read in their original archive location. Relative references are rooted
    at archive_root. Analysis never edits an archive or expands environment variables in it.
    """
    def __init__(self, root, mappings=None):
        self.root = Path(root).resolve()
        self.mappings = sorted(((Path(k), Path(v)) for k, v in (mappings or {}).items()), key=lambda p: len(p[0].parts), reverse=True)

    def __call__(self, reference):
        path = Path(reference)
        for old, new in self.mappings:
            try:
                return (new / path.relative_to(old)).resolve()
            except ValueError:
                continue
        return path.resolve() if path.is_absolute() else (self.root / path).resolve()

# Omit provider prices/charges from scientific paper outputs; retain power-system objectives.
_API_MONEY = {'estimated_llm_cost_usd', 'legacy_estimated_llm_cost_usd', 'estimated_cost_usd', 'estimated_api_cost_usd', 'api_cost_usd', 'rates_usd_per_million',
              'pricing_as_of', 'pricing_source', 'pricing_context_tier', 'pricing_tier',
              'uncached_cost_usd', 'cache_read_cost_usd', 'cache_write_cost_usd', 'output_cost_usd'}
def paper_data(value):
    if isinstance(value, dict):
        return {k: paper_data(v) for k, v in value.items() if k not in _API_MONEY}
    if isinstance(value, list):
        return [paper_data(v) for v in value]
    return value
