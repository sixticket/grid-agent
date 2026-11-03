"""Opt-in observed-prefix comparison; never changes prompts or stopping rules."""
import copy
import json
import os
from pathlib import Path

BASELINE = {'max_cycles': 6, 'max_turns': 40, 'max_runs': 12}
_context = {}
_extended = False


def enabled():
    return os.environ.get('GRID_COMPARE_LIMITS') == '1'


def context():
    return dict(_context, phase='extended' if _extended else 'original_limit') if enabled() else {}


def begin(**position):
    _context.update(position)


def save(path, value):
    path = Path(path)
    tmp = path.with_suffix('.tmp')
    with tmp.open('w') as f:
        json.dump(value, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def checkpoint(out, design, results, distribution, metrics, scenario, *, cycles=None, turns=None, terminal=False, replaying=False):
    global _extended
    if not enabled():
        return
    from reporting import assess
    reached = ((cycles is not None and cycles >= BASELINE['max_cycles']) or
               (turns is not None and turns >= BASELINE['max_turns']) or
               (turns is not None and metrics.get('sim_runs', 0) >= BASELINE['max_runs']))
    snapshot = dict(final_design=copy.deepcopy(design), final_results=results,
                    final_distribution_check=distribution, metrics=dict(metrics),
                    cycles_completed=cycles, turns_completed=turns,
                    comparison_limits=BASELINE, comparison=context(),
                    acceptance=assess(results, distribution, scenario),
                    scope='Observed prefix of this extended run, not a separately rerun baseline.',
                    boundary_reached=reached, early_stop=terminal and not reached)
    if not replaying:
        save(Path(out) / 'progress.json', snapshot)
        baseline = Path(out) / 'original_limit_result.json'
        if (reached or terminal) and not _extended and not baseline.exists():
            save(baseline, snapshot)
    if reached:
        _extended = True


def annotate_final(final, limits):
    final['limits'] = limits
    if enabled():
        final['comparison'] = dict(original_limits=BASELINE, final_phase=context()['phase'],
                                   original_result='original_limit_result.json',
                                   scope='Observed prefix; changed run limits can affect agent behavior.')
