"""Offline tests of immutable overlays and independent carbon acceptance."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "experiments/carbon"))
import co2_adapter


class CO2OverlayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.pack = Path(self.temp.name)
        self.case = dict(region="nyiso", year=2023, co2_limit_Mt=24.0,
                         source_url="https://example.test/source", source_unit="Mt CO2/year")
        self.assumptions = dict(co2_limit_Mt=1000.0, unserved_fraction_limit=0.001,
                               co2_policy="Permissive baseline; edit this fixed assumption for a policy scenario. Agents cannot relax the cap.")
        (self.pack / "config.json").write_text(json.dumps(dict(id="nyiso")))
        (self.pack / "assumptions.json").write_text(json.dumps(self.assumptions))
        self.source = SimpleNamespace(
            NAME="nyiso", SCENARIO=dict(assumptions=self.assumptions, weather_year=2023,
                                        demand_MW=[1.0, 1.0]),
            SCENARIO_SUMMARY="Original assumptions: " + json.dumps(self.assumptions, sort_keys=True),
            DESIGN_SPACE={"wind": {"type": "bool"}}, DEFAULT_DESIGN={"wind": True},
            SNAPSHOTS=["2023-01-01T00:00", "2023-01-01T01:00"],
            validate_changes=lambda changes: [],
        )
        self.module = SimpleNamespace(ROOT=self.pack.parent, _nasa_adapter_installed=True,
                                      load_pack=lambda name, policy="baseline": self.source)

    def install(self):
        self.receipt = co2_adapter.install(self.module, self.case, self.pack)
        return self.module.load_pack(str(self.pack))

    def test_overlay_preserves_original_files_and_namespace_and_is_fixed(self):
        before = {p.name: p.read_bytes() for p in self.pack.iterdir()}
        base = self.install()
        self.assertEqual(base.SCENARIO["assumptions"]["co2_limit_Mt"], 24.0)
        self.assertEqual(self.source.SCENARIO["assumptions"]["co2_limit_Mt"], 1000.0)
        self.assertIn("Permissive baseline", self.source.SCENARIO["assumptions"]["co2_policy"])
        self.assertIn("cited official source", base.SCENARIO["assumptions"]["co2_policy"])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.pack.iterdir()})
        self.assertNotIn('"co2_limit_Mt": 1000', base.SCENARIO_SUMMARY)
        self.assertNotIn("Permissive baseline", base.SCENARIO_SUMMARY)
        self.assertIn(self.case["source_url"], base.SCENARIO_SUMMARY)
        self.assertTrue(base.validate_changes(dict(co2_limit_Mt=1000)))
        self.case["co2_limit_Mt"] = 900
        self.receipt["source_case"]["co2_limit_Mt"] = 800
        self.assertEqual(self.module.load_pack(str(self.pack)).SCENARIO["assumptions"]["co2_limit_Mt"], 24)

    def test_rejects_changed_pack_and_unselected_pack(self):
        self.install()
        with self.assertRaisesRegex(ValueError, "unselected"):
            self.module.load_pack(str(self.pack / "other"))
        (self.pack / "assumptions.json").write_text(json.dumps(dict(co2_limit_Mt=900)))
        with self.assertRaisesRegex(ValueError, "changed"):
            self.module.load_pack(str(self.pack))

    def test_invalid_caps_identity_and_order(self):
        for value in [-1, float("nan"), float("inf"), True, "24"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                co2_adapter.install(self.module, dict(self.case, co2_limit_Mt=value), self.pack)
        for update in [dict(region="ercot"), dict(year=2024), dict(year=True), dict(region="miso", year=2030)]:
            with self.subTest(update=update), self.assertRaises(ValueError):
                co2_adapter.install(self.module, dict(self.case, **update), self.pack)
        self.module._nasa_adapter_installed = False
        with self.assertRaisesRegex(ValueError, "climate adapter"):
            co2_adapter.install(self.module, self.case, self.pack)

    def test_existing_digest_refreshed_but_planned_closures_rejected(self):
        self.source.SCENARIO_HASH = "old"
        self.source.SCENARIO_DIGEST = "old"
        base = self.install()
        self.assertNotEqual(base.SCENARIO_HASH, "old")
        self.assertEqual(base.SCENARIO_HASH, base.SCENARIO_DIGEST[:12])
        self.assertNotEqual(co2_adapter._scenario_digest(self.source.SCENARIO, self.source.SNAPSHOTS), base.SCENARIO_DIGEST)
        self.source.run_simulation = lambda _: None
        with self.assertRaisesRegex(ValueError, "before with_planning_mode"):
            self.module.load_pack(str(self.pack))

    def test_independent_gate_checks_window_cap_and_keeps_previous_failures(self):
        base = self.install()
        reporting = SimpleNamespace(assess=lambda result, dist, scenario: dict(passes=True, checks=dict(solved=True)))
        receipt = co2_adapter.install_acceptance_gate(reporting, self.case, 2)
        cap = 24 * 2 / 8760
        self.assertEqual(receipt["horizon_cap_Mt"], cap)
        result = dict(co2_emitted_Mt=cap, hours_modelled=2, model_assumptions=dict(co2_limit_Mt=24))
        self.assertTrue(reporting.assess(result, {}, base.SCENARIO)["passes"])
        for invalid in [dict(result, co2_emitted_Mt=cap + 1e-4),
                        dict(result, co2_emitted_Mt=float("nan")),
                        dict(result, co2_emitted_Mt=float("inf")),
                        dict(result, co2_emitted_Mt=-1),
                        dict(result, co2_emitted_Mt=True),
                        {k: v for k, v in result.items() if k != "co2_emitted_Mt"},
                        dict(result, co2_emitted_Mt=0, hours_modelled=8760)]:
            with self.subTest(invalid=invalid):
                self.assertFalse(reporting.assess(invalid, {}, base.SCENARIO)["passes"])
        base.SCENARIO["assumptions"]["co2_limit_Mt"] = 1000
        self.assertFalse(reporting.assess(result, {}, base.SCENARIO)["co2_budget"]["scenario_cap_matches"])
        base.SCENARIO["assumptions"]["co2_limit_Mt"] = 24
        self.assertFalse(reporting.assess(dict(result, model_assumptions=dict(co2_limit_Mt=1000)), {}, base.SCENARIO)["co2_budget"]["result_cap_matches"])
        base.SCENARIO["co2_constraint"]["source_case_sha256"] = "wrong"
        self.assertFalse(reporting.assess(result, {}, base.SCENARIO)["passes"])
        reporting2 = SimpleNamespace(assess=lambda *args: dict(passes=False, checks=dict(distribution=False)))
        co2_adapter.install_acceptance_gate(reporting2, self.case, 2)
        self.assertFalse(reporting2.assess(result, {}, self.install_source_scenario())["passes"])

    def install_source_scenario(self):
        return self.module.load_pack(str(self.pack)).SCENARIO


class ExistingConstraintTests(unittest.TestCase):
    def test_existing_extension_enforces_bound_in_real_tiny_lp(self):
        """The unchanged runtime constraint must alter solved fossil dispatch."""
        try:
            import linopy
            import pandas as pd
            import highspy  # noqa: F401
        except ImportError as exc:
            self.skipTest(f"Run with experiment/.venv/bin/python for solver integration: {exc}")
        runtime = ROOT / "models/sol/runtime_original/engine"
        spec = importlib.util.spec_from_file_location("_co2_test_extensions", runtime / "extensions.py")
        extension = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(extension)
        old_policy = sys.modules.get("policy")
        sys.modules["policy"] = SimpleNamespace(add_constraints=lambda *args: None)
        try:
            outputs = []
            for annual_cap in (8760.0 / 1e6, 4380.0 / 1e6):
                model = linopy.Model()
                times = pd.Index([0, 1], name="snapshot")
                names = pd.Index(["thermal"], name="name")
                dispatch = model.add_variables(lower=0, upper=10, coords=[times, names], name="Generator-p")
                model.add_objective(-dispatch.sum())
                network = SimpleNamespace(
                    model=model, snapshots=times,
                    generators=pd.DataFrame(index=names), storage_units=pd.DataFrame(),
                    meta=dict(planning_assets=dict(thermal=dict(id="thermal", emissions_t_per_MWh=1.0))),
                )
                extension.physical_constraints(network, dict(assumptions=dict(co2_limit_Mt=annual_cap)), {})
                self.assertIn("operational_co2_budget", model.constraints)
                status, condition = model.solve(solver_name="highs", log_to_console=False)
                self.assertEqual((status, condition), ("ok", "optimal"))
                outputs.append(float(dispatch.solution.sum()))
            self.assertAlmostEqual(outputs[0], 2.0)
            self.assertAlmostEqual(outputs[1], 1.0)
        finally:
            if old_policy is None:
                sys.modules.pop("policy", None)
            else:
                sys.modules["policy"] = old_policy


if __name__ == "__main__":
    unittest.main()
