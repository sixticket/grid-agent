"""Resolve preserved provenance paths without rewriting archived evidence."""
import json
import os
from pathlib import Path
import sys

_PROJECT = None
_ANALYSIS = None
_PREFIXES = {}


def configure(project, output):
    global _PROJECT, _ANALYSIS, _PREFIXES
    _PROJECT = Path(project).resolve()
    _ANALYSIS = Path(os.environ.get('GRID_AGENT_CARBON_ANALYSIS', output)).resolve()
    origin = _PROJECT / 'PACKAGE_ORIGIN.json'
    recorded = json.loads(origin.read_text()).get('original_roots', []) if origin.exists() else []
    _PREFIXES = {prefix: str(_PROJECT) for prefix in recorded}
    _PREFIXES.update(json.loads(os.environ.get('GRID_AGENT_ARCHIVE_MAP', '{}')))


def resolve(value):
    path = Path(value)
    for old, new in sorted(_PREFIXES.items(), key=lambda x: len(x[0]), reverse=True):
        try:
            return Path(new) / path.relative_to(old)
        except ValueError:
            pass
    if path.is_absolute():
        return path
    if _PROJECT is None:
        raise ValueError('Configure the archive root before resolving relative evidence paths')
    return _PROJECT / path


def analysis_root(project):
    return _ANALYSIS or Path(project) / 'figure_carbon'


def drawing_modules(project):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'manuscript'))
    import build_geographic_figures as geo
    import build_publication_figures as pub
    # Drawing helpers read the preserved Natural Earth inputs, and never rebuild
    # or overwrite the original paper's geographic evidence.
    geo.EV = Path(project) / 'Nature_Energy/evidence/geographic_20261003'
    pub.EV = geo.EV
    return geo, pub
