"""Fixed policy scenarios shared by deterministic, single and multi runs."""
import json
from pathlib import Path

CATALOG = Path(__file__).resolve().parents[1] / "shared/electricity_policies.json"


def load_policy(region, mode="baseline"):
    if mode not in ("baseline", "published"):
        raise ValueError("policy must be baseline or published")
    target = json.loads(CATALOG.read_text()).get(region)
    if mode == "published" and target is None:
        raise ValueError(f"No mapped electricity policy for {region}; supported: australia, nyiso")
    return dict(mode=mode, active=mode == "published", target=target)


def generator_groups(n, target):
    assets = n.meta["planning_assets"]
    primary = [g for g in n.generators.index if g.split("::")[0] != "load_shedding"]
    renewable = [g for g in primary if assets.get(g, {}).get("technology", g.split("::")[0])
                 in target["eligible_technologies"]]
    return primary, renewable


def add_constraints(n, scenario):
    policy = scenario.get("electricity_policy", {})
    if not policy.get("active"):
        return
    target = policy["target"]
    primary, renewable = generator_groups(n, target)
    dispatch = n.model.variables["Generator-p"]
    denominator = (dispatch.sel(name=primary).sum() if target["denominator"] == "generation"
                   else float(sum(scenario["demand_MW"])))
    n.model.add_constraints(dispatch.sel(name=renewable).sum() >= target["renewable_fraction"] * denominator,
                            name="fixed_renewable_policy")


def report(n, scenario):
    policy = scenario.get("electricity_policy", {})
    target = policy.get("target")
    if not target:
        return dict(active=False, passes=True, meets_target=None)
    primary, renewable = generator_groups(n, target)
    actual = float(n.generators_t.p[renewable].to_numpy().sum())
    denominator = (float(n.generators_t.p[primary].to_numpy().sum())
                   if target["denominator"] == "generation" else float(sum(scenario["demand_MW"])))
    required = target["renewable_fraction"] * denominator
    meets = denominator > 0 and actual + 1e-3 >= required
    return dict(active=policy["active"], target_id=target["id"],
                denominator=target["denominator"], denominator_MWh=denominator,
                renewable_MWh=actual, required_MWh=required,
                renewable_fraction=actual / denominator if denominator > 0 else None,
                target_fraction=target["renewable_fraction"], meets_target=meets,
                passes=meets if policy["active"] else True)
