"""Preserve the best physically accepted committed result without changing search.

Candidate selection is reporting state only: it does not alter prompts, tools,
the committed simulator cache, or the agent's decision to continue or stop.
"""
import copy
import hashlib
import json
import math
from pathlib import Path

from reporting import assess


SELECTION_POLICY = 'lowest_cost_physically_accepted_committed_design'


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _total_cost(results, distribution):
    try:
        system = float(results['total_cost_billion'])
        feeders = float(distribution['distribution_capex_billion'])
    except (KeyError, TypeError, ValueError):
        return None
    total = system + feeders
    return total if all(math.isfinite(x) for x in (system, feeders, total)) else None


def _save(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')
    temporary.replace(path)


class BestAccepted:
    def __init__(self, out, scenario, simulator, audit=False):
        self.out = Path(out)
        self.scenario = scenario
        self.simulator = simulator
        self.audit = audit
        self.best = None
        self.last_source = None

    def _dispatch(self, candidate, replayed):
        # Unique immutable generations ensure a later write cannot invalidate
        # the previous accepted record if interrupted between two file writes.
        identity = hashlib.sha256(_canonical(candidate).encode()).hexdigest()
        relative = Path('accepted_dispatch') / (identity + '.nc')
        path = self.out / relative
        unavailable = dict(available=False, reason='Historical evidence has no retained exact accepted dispatch.')
        if not replayed and not self.audit:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.simulator.save_dispatch_checkpoint(path)
        if not path.is_file() or not path.with_suffix('.json').is_file():
            if replayed or self.audit:
                return unavailable
            raise RuntimeError('Accepted dispatch checkpoint was not saved.')
        metadata = json.loads(path.with_suffix('.json').read_text())
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        expected_key = json.dumps({k: v for k, v in candidate['design'].items()
                                   if not k.startswith('dist_')}, sort_keys=True)
        expected_scenario = getattr(self.simulator, 'SCENARIO_HASH', None)
        if (digest != metadata.get('sha256') or metadata.get('key') != expected_key
                or (expected_scenario and not str(metadata.get('scenario_digest', '')).startswith(expected_scenario))):
            raise RuntimeError('Accepted dispatch checkpoint identity/checksum mismatch.')
        return dict(available=True, path=str(relative), metadata_path=str(relative.with_suffix('.json')),
                    sha256=digest, candidate_sha256=identity)

    def observe(self, design, results, distribution, source, replayed=False):
        """Observe a committed solve+feeder check; never call for verify_design."""
        self.last_source = copy.deepcopy(source)
        acceptance = assess(results, distribution, self.scenario)
        total = _total_cost(results, distribution)
        if not acceptance['passes'] or total is None:
            return False
        if self.best is not None and total >= self.best['all_in_cost_billion']:
            return False
        candidate = copy.deepcopy(dict(design=design, results=results, distribution_check=distribution,
                                       acceptance=acceptance, source=source, all_in_cost_billion=total))
        candidate['dispatch_checkpoint'] = self._dispatch(candidate, replayed)
        self.best = candidate
        if not self.audit and not replayed:
            _save(self.out / 'best_accepted.json', candidate)
        return True

    def apply_to_final(self, final):
        """Select the accepted incumbent for output, retaining the last attempt."""
        last = copy.deepcopy(dict(design=final['final_design'], results=final['final_results'],
                                  distribution_check=final['final_distribution_check'],
                                  acceptance=assess(final['final_results'], final['final_distribution_check'], self.scenario),
                                  source=self.last_source,
                                  all_in_cost_billion=_total_cost(final['final_results'], final['final_distribution_check'])))
        final['last_attempt'] = last
        selected = self.best if self.best is not None else last
        final.update(final_design=copy.deepcopy(selected['design']),
                     final_results=copy.deepcopy(selected['results']),
                     final_distribution_check=copy.deepcopy(selected['distribution_check']),
                     acceptance=copy.deepcopy(selected['acceptance']))
        differs = any(selected[k] != last[k] for k in ('design', 'results', 'distribution_check'))
        final['final_selection'] = dict(
            policy=SELECTION_POLICY,
            selected='best_accepted' if self.best is not None else 'last_attempt_no_accepted_design',
            source=copy.deepcopy(selected['source']),
            all_in_cost_billion=selected['all_in_cost_billion'],
            selected_differs_from_last_attempt=differs,
            dispatch_checkpoint=copy.deepcopy(selected.get('dispatch_checkpoint', dict(
                available=False, reason='No physically accepted committed design was observed.'))))
        return final
