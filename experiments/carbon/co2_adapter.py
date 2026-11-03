"""Fixed, auditable CO2 overlays for the selected regional sensitivity cases.

Install after the original climate adapter has validated the original pack and
before importing either agent. No input pack or frozen runtime file is modified.
The existing PyPSA constraint reads the overlaid ``co2_limit_Mt`` assumption.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
import re


SELECTED_CASES = frozenset({
    ("nyiso", 2023), ("nyiso", 2030), ("nyiso", 2035),
    ("miso", 2030), ("miso", 2035),
    ("south_africa", 2030), ("south_africa", 2035),
    ("mexico", 2030), ("brazil", 2030), ("brazil", 2035),
})
# A floor of one kilogram and a relative tolerance of 1e-8 of the horizon
# budget are an explicit numerical reporting tolerance, not a relaxed LP cap.
ABSOLUTE_TOLERANCE_MT = 1e-9
RELATIVE_TOLERANCE = 1e-8
SCOPE = (
    "Operational direct emissions from existing generators with a supplied "
    "emissions_t_per_MWh coefficient; lifecycle and imported-power emissions excluded."
)


def _canonical(value):
    return json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":"))


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _case(case):
    if not isinstance(case, dict):
        raise ValueError("A CO2 case must be an object")
    region, year = case.get("region"), case.get("year")
    if not isinstance(region, str) or type(year) is not int or (region, year) not in SELECTED_CASES:
        raise ValueError("CO2 case is outside the selected region/year pairs")
    cap = case.get("co2_limit_Mt")
    if not _finite_number(cap) or cap < 0:
        raise ValueError("co2_limit_Mt must be a finite non-negative number")
    # JSON round-trip both isolates caller mutations and requires serializable,
    # finite provenance. Do not normalize source numbers or omit source fields.
    fixed = json.loads(_canonical(case))
    digest = hashlib.sha256(_canonical(fixed).encode()).hexdigest()
    return fixed, float(cap), digest


def _scenario_digest(scenario, snapshots):
    """Use the frozen framework's planning.with_planning_mode digest format.

    The framework has no public hash helper. This mirrors its exact JSON call;
    normal installation precedes planning and lets that function hash naturally.
    """
    def serial(value):
        # The framework's sole non-JSON scenario values are NumPy arrays.
        if type(value).__module__.split(".")[0] == "numpy" and hasattr(value, "tolist"):
            return value.tolist()
        raise TypeError(type(value).__name__)

    return hashlib.sha256(json.dumps(
        dict(scenario=scenario, snapshots=list(map(str, snapshots))),
        default=serial, sort_keys=True, allow_nan=False,
    ).encode()).hexdigest()


def install(scenario_core, case, pack):
    """Bind ``load_pack(name, policy='baseline')`` to a fixed CO2 overlay.

    ``case`` requires region, year and co2_limit_Mt; all additional fields are
    retained as provenance. The original climate adapter must be installed first.
    The returned receipt is JSON serializable and contains no secrets.
    """
    if getattr(scenario_core, "_co2_adapter_installed", False):
        raise ValueError("CO2 adapter already installed; use a fresh worker")
    if not getattr(scenario_core, "_nasa_adapter_installed", False):
        raise ValueError("Validate original inputs with the climate adapter before installing CO2")
    fixed, cap, case_digest = _case(case)
    pack = Path(pack).resolve()
    cfg = json.loads((pack / "config.json").read_text())
    prediction = cfg.get("prediction")
    year = prediction.get("planning_year") if isinstance(prediction, dict) else 2023
    if cfg.get("id") != fixed["region"] or year != fixed["year"]:
        raise ValueError("CO2 case identity differs from the bound input pack")
    config_digest = _sha(pack / "config.json")
    assumptions_path = pack / "assumptions.json"
    assumptions_digest = _sha(assumptions_path)
    original_assumptions = json.loads(assumptions_path.read_text())
    original_cap = original_assumptions.get("co2_limit_Mt")
    if not _finite_number(original_cap) or original_cap < 0:
        raise ValueError("Original pack has no valid CO2 assumption")
    receipt = dict(
        schema_version=1, region=fixed["region"], year=fixed["year"],
        case_id=fixed.get("case_id", fixed.get("id", f'{fixed["region"]}_{fixed["year"]}')),
        original_cap_Mt=original_cap, annual_cap_Mt=cap,
        source_case_sha256=case_digest, source_case=fixed,
        original_pack=str(pack), original_assumptions_sha256=assumptions_digest,
        original_config_sha256=config_digest,
        constraint="operational_co2_budget", annual_hours=8760,
        horizon_rule="annual_cap_Mt * modelled_hours / 8760",
        model_emissions_scope=SCOPE,
        application="Fixed researcher-selected scenario input; not an agent design variable",
    )
    original_load = scenario_core.load_pack

    def load_with_co2(name, policy="baseline"):
        requested = Path(name)
        requested = requested.resolve() if requested.is_absolute() else (scenario_core.ROOT / requested).resolve()
        if requested != pack:
            raise ValueError("CO2 worker attempted to load an unselected input pack")
        if _sha(assumptions_path) != assumptions_digest or _sha(pack / "config.json") != config_digest:
            raise ValueError("Original CO2 input pack changed after installation")
        source = original_load(name, policy=policy)
        if source.NAME != fixed["region"] or source.SCENARIO.get("weather_year", 2023) != fixed["year"]:
            raise ValueError("Loaded scenario identity differs from the fixed CO2 case")
        if source.SCENARIO["assumptions"] != original_assumptions:
            raise ValueError("Loaded assumptions differ from the validated original pack")
        if "co2_limit_Mt" in source.DESIGN_SPACE or "co2_limit_Mt" in source.DEFAULT_DESIGN:
            raise ValueError("CO2 cannot be exposed as an agent design variable")
        # A planned simulator closes over its original scenario/digest. Reject
        # those closures rather than claim rehashing them updates their physics.
        if hasattr(source, "run_simulation"):
            raise ValueError("Install CO2 before with_planning_mode creates simulator closures")
        base = copy.copy(source)
        base.SCENARIO = copy.deepcopy(source.SCENARIO)
        base.SCENARIO["assumptions"]["co2_limit_Mt"] = cap
        base.SCENARIO["assumptions"]["co2_policy"] = (
            "Fixed researcher-selected annual CO2 ceiling based on the cited official source "
            "and recorded source-to-model mapping. Agents cannot relax the cap. "
            "The source's legal or planning status is recorded in co2_constraint.source_case."
        )
        base.SCENARIO["co2_constraint"] = copy.deepcopy(receipt)
        base.DESIGN_SPACE = copy.deepcopy(source.DESIGN_SPACE)
        base.DEFAULT_DESIGN = copy.deepcopy(source.DEFAULT_DESIGN)
        old_json = json.dumps(original_assumptions, sort_keys=True)
        new_json = json.dumps(base.SCENARIO["assumptions"], sort_keys=True)
        summary = source.SCENARIO_SUMMARY.replace(old_json, new_json)
        if isinstance(original_assumptions.get("co2_policy"), str):
            summary = summary.replace(
                json.dumps(original_assumptions["co2_policy"]),
                json.dumps(base.SCENARIO["assumptions"]["co2_policy"]),
            )
        summary = re.sub(
            r'("co2_limit_Mt"\s*:\s*)-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?',
            lambda match: match.group(1) + json.dumps(cap), summary,
        )
        base.SCENARIO_SUMMARY = summary + (
            f"\nFixed CO2 scenario: {fixed['region']} {fixed['year']}, {cap:g} Mt/year. "
            "The PyPSA operational_co2_budget constraint enforces this value; "
            "short windows receive modelled_hours/8760 of the annual budget. "
            "Agents may not modify or relax this assumption. " + SCOPE +
            "\nCO2 source and mapping: " + json.dumps(fixed, ensure_ascii=False, sort_keys=True)
        )
        original_validate = source.validate_changes

        def validate_changes(changes):
            errors = list(original_validate(changes))
            if isinstance(changes, dict) and "co2_limit_Mt" in changes:
                errors.append("co2_limit_Mt is a fixed scenario input")
            return errors

        base.validate_changes = validate_changes
        if hasattr(base, "SCENARIO_HASH") or hasattr(base, "SCENARIO_DIGEST"):
            digest = _scenario_digest(base.SCENARIO, base.SNAPSHOTS)
            base.SCENARIO_HASH, base.SCENARIO_DIGEST = digest[:12], digest
        return base

    scenario_core.load_pack = load_with_co2
    scenario_core._co2_adapter_installed = True
    return copy.deepcopy(receipt)


def install_acceptance_gate(reporting, case, hours):
    """Add an independent cap audit to every ``reporting.assess`` invocation.

    Install before either agent or best_accepted imports ``assess``. The original
    acceptance checks and signature are preserved. Missing numerical emissions,
    changed caps/provenance, or a different horizon fail this additional gate.
    """
    if getattr(reporting, "_co2_gate_installed", False):
        raise ValueError("CO2 acceptance gate already installed; use a fresh worker")
    fixed, cap, case_digest = _case(case)
    if type(hours) is not int or not 2 <= hours <= 8760:
        raise ValueError("CO2 gate hours must be an integer in [2, 8760]")
    horizon_cap = cap * hours / 8760
    tolerance = max(ABSOLUTE_TOLERANCE_MT, horizon_cap * RELATIVE_TOLERANCE)
    original_assess = reporting.assess

    def assess(result, dist, scenario):
        result = result or {}
        acceptance = copy.deepcopy(original_assess(result, dist, scenario))
        emitted = result.get("co2_emitted_Mt")
        finite_emitted = _finite_number(emitted)
        actual_cap = scenario.get("assumptions", {}).get("co2_limit_Mt")
        reported_cap = result.get("model_assumptions", {}).get("co2_limit_Mt")
        provenance = scenario.get("co2_constraint", {})
        scenario_cap_matches = _finite_number(actual_cap) and actual_cap == cap
        result_cap_matches = _finite_number(reported_cap) and reported_cap == cap
        provenance_matches = (
            provenance.get("source_case_sha256") == case_digest
            and provenance.get("region") == fixed["region"]
            and provenance.get("year") == fixed["year"]
        )
        horizon_matches = len(scenario.get("demand_MW", [])) == hours and result.get("hours_modelled") == hours
        emissions_within_cap = bool(finite_emitted and -tolerance <= emitted <= horizon_cap + tolerance)
        passes = scenario_cap_matches and result_cap_matches and provenance_matches and horizon_matches and emissions_within_cap
        details = dict(
            passes=passes, annual_cap_Mt=cap, hours_modelled=hours,
            horizon_cap_Mt=horizon_cap, emitted_Mt=float(emitted) if finite_emitted else None,
            slack_Mt=horizon_cap - emitted if finite_emitted else None,
            tolerance_Mt=tolerance, finite_emissions=finite_emitted,
            emissions_within_cap=emissions_within_cap, scenario_cap_matches=scenario_cap_matches,
            result_cap_matches=result_cap_matches,
            provenance_matches=provenance_matches, horizon_matches=horizon_matches,
            source_case_sha256=case_digest, model_emissions_scope=SCOPE,
        )
        acceptance.setdefault("checks", {})["co2_budget"] = bool(passes)
        acceptance["passes"] = bool(acceptance.get("passes")) and bool(passes)
        acceptance["co2_budget"] = details
        return acceptance

    reporting.assess = assess
    reporting._co2_gate_installed = True
    return dict(
        schema_version=1, source_case_sha256=case_digest, annual_cap_Mt=cap,
        hours_modelled=hours, horizon_cap_Mt=horizon_cap, tolerance_Mt=tolerance,
        acceptance_check="co2_budget",
    )
