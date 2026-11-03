"""Pure recorded-evidence checks; no model, optimizer or campaign execution."""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

CUTOFF = '2026-09-24T16:40:53+00:00'
POLICY = 'lowest_cost_physically_accepted_committed_design'
REGIONS = ['miso', 'ercot', 'nyiso', 'south_africa', 'australia', 'new_zealand', 'mexico', 'brazil']
LABELS = dict(miso='MISO', ercot='ERCOT', nyiso='NYISO', south_africa='South Africa', australia='Australia', new_zealand='New Zealand', mexico='Mexico', brazil='Brazil')

def sha(data):
    return hashlib.sha256(data).hexdigest()

def canonical(value):
    return sha(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode())

def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')

def read_source(root, path, sources):
    raw = path.read_bytes()
    relative = str(path.relative_to(root))
    sources[relative] = dict(path=relative, sha256=sha(raw), bytes=len(raw))
    return raw

def physical_acceptance(result, dist, final):
    """Exact criteria in reporting.assess, using the archived annual threshold."""
    result = result or {}; dist = dist or {}
    tx = result.get('transmission') or {}
    checks = dict(
        solved=result.get('status') in ('optimal', 'feasible_time_limit'),
        reliable=result.get('unmet_demand_GWh', float('inf')) <= final['acceptance']['unmet_limit_GWh'],
        distribution=dist.get('passes') is True and dist.get('thermal_check_complete') is True,
        energy_budgets=all(x['passes'] for x in result.get('energy_budget_checks', [])),
        storage_balance=result.get('max_storage_balance_residual_MWh', float('inf')) < 1e-3,
        nodal_balance=result.get('max_nodal_balance_residual_MW', float('inf')) < 1e-3,
        transmission_limits=max(tx.get('max_interface_violation_MW', 0) or 0, tx.get('max_zone_limit_violation_MW', 0) or 0) < 1e-3,
        policy=(result.get('policy_check', {}).get('passes') is True) if final.get('electricity_policy', {}).get('active') else True)
    return dict(passes=all(checks.values()), checks=checks,
                unmet_limit_GWh=final['acceptance']['unmet_limit_GWh'])

def summarize_candidate(design, result, dist, final, source, line):
    result = result or {}; dist = dist or {}
    acceptance = physical_acceptance(result, dist, final)
    cost = result.get('total_cost_billion')
    dc = dist.get('distribution_capex_billion')
    allin = cost + dc if cost is not None and dc is not None else None
    lb = result.get('cost_lower_bound_billion')
    distribution = {k: dist.get(k) for k in ['passes', 'overvoltage_hours', 'undervoltage_hours', 'voltage_violation_hours', 'nonconverged_hours', 'thermal_check_complete', 'thermal_overload_hours', 'worst_low_pu', 'worst_high_pu', 'distribution_capex_billion']}
    return dict(source=source, log_line=line, design=copy.deepcopy(design),
                candidate_sha256=canonical(dict(design=design, results=result, distribution_check=dist)),
                acceptance=acceptance, status=result.get('status'),
                system_cost_billion=cost, distribution_cost_billion=dc, all_in_cost_billion=allin,
                system_cost_lower_bound_billion=lb,
                all_in_cost_lower_bound_billion=lb + dc if lb is not None and dc is not None else None,
                mip_gap=result.get('mip_gap'), unmet_demand_GWh=result.get('unmet_demand_GWh'),
                hours_modelled=result.get('hours_modelled'), capacities_GW=result.get('capacities_GW'),
                new_capacities_GW=result.get('new_capacities_GW'), distribution=distribution,
                energy_budget_checks=result.get('energy_budget_checks'),
                max_storage_balance_residual_MWh=result.get('max_storage_balance_residual_MWh'),
                max_nodal_balance_residual_MW=result.get('max_nodal_balance_residual_MW'),
                transmission_checks={k: (result.get('transmission') or {}).get(k) for k in ['max_interface_violation_MW', 'max_zone_limit_violation_MW']})

def replay(log, agent, final):
    design, result, dist = None, None, None
    candidates = []; count = 0; verification = 0; first_state = None; meta = None
    for line, raw in enumerate(log.splitlines(), 1):
        row = json.loads(raw); event = row['event']; p = row['payload']
        if event == 'meta':
            meta = p
        if agent == 'multi':
            if event == 'verification':
                verification += 1
            if event == 'simulation':
                count += 1; design = p['design']; result = p['results']; dist = p.get('distribution_check')
                source = dict(cycle=p['cycle'], event=event)
            elif event == 'distribution_check' and not p['results'].get('error'):
                dist = p['results']; source = dict(cycle=p['cycle'], event=event)
            else:
                continue
        else:
            if event != 'tool':
                continue
            name = p['name']; value = p['result']
            if isinstance(value, str):
                value = json.loads(value)
            if name == 'get_state':
                if first_state is None:
                    first_state = value
                if design is None:
                    design = value['current_design']
                assert design == value['current_design'], (line, 'single design state drift')
                continue
            if name == 'update_design':
                if value.get('ok') is True:
                    changes = p['input'].get('changes', {})
                    if design is not None:
                        assert dict(design, **changes) == value['current_design'], (line, 'incomplete design update')
                    design = value['current_design']; dist = None
                    if any(not k.startswith('dist_') for k in changes):
                        result = None
                continue
            if name == 'run_simulation' and 'results' in value:
                count += 1; result = value['results']; dist = value.get('distribution_check')
            elif name == 'run_distribution_check' and not value.get('error'):
                assert result is not None, (line, 'feeder result without matching system result')
                dist = value
            else:
                continue
            source = dict(turn=p['turn'], tool=name, simulation=count)
        assert design is not None, (line, 'candidate without full design')
        candidates.append(summarize_candidate(design, result, dist, final, source, line))
    accepted = [c for c in candidates if c['acceptance']['passes']]
    assert accepted, 'No accepted committed candidate'
    best = min(accepted, key=lambda c:c['all_in_cost_billion'])
    return dict(candidates=candidates, selected=best, first_accepted=accepted[0], initial=candidates[0],
                logged_simulations=count, logged_verifications=verification,
                initial_state_sha256=canonical(first_state) if first_state is not None else None,
                meta=meta)
