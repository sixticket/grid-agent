"""Offline launcher contracts: annual inputs, routing, isolation and provenance."""
import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import run_case as runner
import run_campaign as campaign

class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pack = self.root / "pack"; self.pack.mkdir()
        self.shared = self.root / "experiment/shared"; self.shared.mkdir(parents=True)
        self.write("config.json", {"id":"miso", "cost_file":"costs_us.json"})
        self.write("network.json", {"nodes":[{"id":"A", "profiles_csv":"profiles.csv"}]})
        self.write("inventory.json", {}); self.write("assumptions.json", {})
        for name in ("costs_us.json", "electricity_policies.json"):
            (self.shared/name).write_text("{}")
        origin=dt.datetime(2023,1,1)
        self.profile="time,demand_MW,solar_cf,wind_cf\n"+"".join(f"{origin+dt.timedelta(hours=h):%Y-%m-%dT%H:%M:%S},10,0.2,0.3\n" for h in range(8760))
        (self.pack/"profiles.csv").write_text(self.profile)

    def write(self, name, value):
        (self.pack/name).write_text(json.dumps(value))

    def args(self, *extra):
        return runner.parser().parse_args(["--deployment","sol","--region","miso","--year","2023","--agent","single","--pack",str(self.pack),"--shared",str(self.shared),"--data-root",str(self.root),"--output",str(self.root/"output"),*extra])

    def plan(self, *extra):
        return runner.make_plan(self.args(*extra))

    def test_plan_neither_reads_nor_writes(self):
        with patch.object(runner,"check_inputs",side_effect=AssertionError("unexpected read")), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(runner.main(["--deployment","opus","--region","miso","--year","2035","--agent","multi","--pack",str(self.root/"missing"),"--shared",str(self.shared),"--output",str(self.root/"out")]),0)
        self.assertEqual(json.loads(output.getvalue())["status"],"plan_only")
        self.assertFalse((self.root/"out").exists())

    def test_routes_and_historical_output_ceilings(self):
        self.assertEqual(set(runner.MODELS),{"sol","opus"})
        for deployment in runner.MODELS:
            self.assertEqual(self.plan("--deployment",deployment,"--region","south_africa","--year","2030")["profile"],"runtime_calendar")
            self.assertEqual(self.plan("--deployment",deployment,"--region","south_africa","--year","2035")["profile"],"runtime_original")
        self.assertEqual(self.plan("--deployment","opus")["output_tokens"],16000)
        self.assertEqual(self.plan("--deployment","opus","--year","2030")["output_tokens"],64000)
        self.assertEqual(self.plan()["output_tokens"],64000)
        self.assertEqual(self.plan("--year","2035")["time_axis_year"],2023)

    def test_hashes_include_runtime_adapter_and_launcher(self):
        inventory=runner.check_inputs(self.plan())
        self.assertIn("pack/profiles.csv",inventory["input_sha256"])
        self.assertIn("shared/costs_us.json",inventory["input_sha256"])
        self.assertIn("models/sol/runtime_original/engine/single_agent.py",inventory["source_sha256"])
        self.assertIn("models/sol/climate_adapter.py",inventory["source_sha256"])
        self.assertIn("tools/run_case.py",inventory["source_sha256"])
        self.assertIsNone(inventory["prediction"])

    def test_rejects_escape_and_duplicate_nodes(self):
        self.write("network.json",{"nodes":[{"id":"A","profiles_csv":"../outside.csv"}]})
        with self.assertRaises(ValueError):runner.check_inputs(self.plan())
        self.write("network.json",{"nodes":[{"id":"A","profiles_csv":"profiles.csv"}]*2})
        with self.assertRaisesRegex(ValueError,"unique"):runner.check_inputs(self.plan())

    def test_rejects_bad_year_size_clock_and_capacity_factor(self):
        path=self.pack/"profiles.csv"
        for text,reason in [(self.profile.replace("2023-","2030-"),"2023"),("\n".join(self.profile.splitlines()[:-1])+"\n","8760"),(self.profile.replace("2023-01-01T01:00:00","2023-01-01T00:00:00"),"contiguous"),(self.profile.replace(",0.2,0.3",",1.2,0.3",1),"invalid")]:
            with self.subTest(reason=reason):
                path.write_text(text)
                with self.assertRaisesRegex(ValueError,reason):runner.check_inputs(self.plan())

    def test_future_demand_only_pack_is_rejected(self):
        scenario={"region":"miso","planning_year":2030,"weather_year":2023}
        self.write("config.json",{"id":"miso","cost_file":"costs_us.json","prediction":scenario})
        self.write("prediction_manifest.json",{"scenario":scenario,"files":{}})
        with self.assertRaisesRegex(ValueError,"years"):runner.check_inputs(self.plan("--year","2030"))
        with self.assertRaisesRegex(ValueError,"baseline"):runner.check_inputs(self.plan())

    def test_execute_preserves_parent_env_and_previous_attempt(self):
        plan=self.plan();inventory=runner.check_inputs(plan)
        inherited={"OPENAI_API_KEY":"test-only","GRID_RESUME":"1","GRID_SOLVER_EXTRA":"{}","OPENAI_BASE_URL":"https://inherited.invalid","ANTHROPIC_BASE_URL":"https://inherited.invalid","GRID_LOCAL_BASE_URL":"http://localhost:1234"}
        def fake_engine(active, _inventory):
            for key in inherited.keys()-{"OPENAI_API_KEY"}:self.assertNotIn(key,os.environ)
            self.assertEqual(os.environ["GRID_DATA_ROOT"],str(self.root.resolve()))
            runner.save(Path(active["output"])/"final.json",{"acceptance":{"passes":True}})
        with patch.dict(os.environ,inherited):
            status=runner.execute_case(plan,inventory,fake_engine)
            self.assertEqual(status["status"],"completed");self.assertTrue(status["accepted"])
            for key,value in inherited.items():self.assertEqual(os.environ[key],value)
            with self.assertRaisesRegex(ValueError,"overwrite"):runner.execute_case(plan,inventory,fake_engine)
        config=json.loads((Path(plan["output"])/"run_config.json").read_text())
        self.assertEqual(config["source_sha256"],inventory["source_sha256"])
        self.assertNotIn("test-only",json.dumps(config))

    def test_changed_input_prevents_any_execution_output(self):
        plan=self.plan();inventory=runner.check_inputs(plan);self.write("assumptions.json",{"changed":True})
        with patch.dict(os.environ,{"OPENAI_API_KEY":"test-only"}):
            with self.assertRaisesRegex(ValueError,"changed"):runner.execute_case(plan,inventory,lambda *_:self.fail("must not execute"))
        self.assertFalse(Path(plan["output"]).exists())

    def test_engine_hook_attaches_metadata_and_accounted_usage(self):
        plan=self.plan();out=Path(plan["output"]);out.mkdir()
        inventory=runner.check_inputs(plan)
        base=types.SimpleNamespace(SCENARIO={"year":2023},SCENARIO_SUMMARY="existing")
        scenario_core=types.SimpleNamespace(load_pack=lambda *a,**kw:base)
        policy=types.SimpleNamespace()
        def main():
            loaded=scenario_core.load_pack(plan["pack"])
            self.assertEqual(loaded.SCENARIO["weather_year"],2023)
            self.assertEqual(sys.argv[sys.argv.index("--model")+1],"gpt-6.1-sol")
            runner.save(out/"final.json",{"acceptance":{"passes":True},"metrics":{"api_calls":9}})
            for name in ("api_usage.jsonl","api_responses.jsonl"):(out/name).write_text("")
        modules={"scenario_core":scenario_core,"policy":policy,"single_agent":types.SimpleNamespace(main=main),"api_usage":types.SimpleNamespace(summarize_ledger=lambda p:{"api_calls":0,"input_tokens":0,"output_tokens":0,"estimated_cost_usd":0}),"reporting":types.SimpleNamespace(write_report=lambda *a:None)}
        old_path,old_argv=sys.path[:],sys.argv[:]
        try:
            with patch.dict(sys.modules,modules):runner.invoke_engine(plan,inventory)
        finally:
            sys.path[:]=old_path;sys.argv=old_argv
        final=runner.read_json(out/"final.json")
        self.assertEqual(final["metrics"]["api_calls"],0)
        self.assertEqual(final["agent_reported_usage"]["api_calls"],9)
        self.assertEqual(final["paper_run_settings"]["output_tokens"],64000)
        self.assertEqual(scenario_core.ROOT,self.shared.resolve().parent)
        self.assertEqual(policy.CATALOG,self.shared.resolve()/"electricity_policies.json")
        self.assertTrue(final["api_response_journal"]["ledger_coverage_complete"])

    def test_missing_final_and_provider_errors_are_recorded_safely(self):
        class ProviderError(Exception):
            status_code=503;request_id="req_test123"
        def fail(*_):raise ProviderError("api_key=TOP_SECRET provider body")
        with patch.dict(os.environ,{"OPENAI_API_KEY":"TOP_SECRET"}):
            plan=self.plan();status=runner.execute_case(plan,runner.check_inputs(plan),fail)
            self.assertEqual(status["error"],{"type":"ProviderError","http_status":503,"request_id":"req_test123"})
            for path in Path(plan["output"]).iterdir():self.assertNotIn("TOP_SECRET",path.read_text())
            plan=self.plan("--output",str(self.root/"missing_final"))
            self.assertEqual(runner.execute_case(plan,runner.check_inputs(plan),lambda *_:None)["status"],"failed")

class CampaignTest(unittest.TestCase):
    def test_default_matrix_unique_and_plan_no_io(self):
        with tempfile.TemporaryDirectory() as d:
            output=Path(d)/"missing"
            args=campaign.parser().parse_args(["--output",str(output),"--data-root",str(Path(d)/"data")])
            jobs=campaign.commands(args)
            self.assertEqual(len(jobs),96);self.assertEqual(len({j["case_id"] for j in jobs}),96)
            with contextlib.redirect_stdout(io.StringIO()) as captured:
                self.assertEqual(campaign.main(["--output",str(output)]),0)
            self.assertEqual(json.loads(captured.getvalue())["cases"],96);self.assertFalse(output.exists())

    def test_explicit_baseline_route(self):
        args=campaign.parser().parse_args(["--output","runs","--years","2023","--baseline-root","corrected","--baseline-pattern","{region}_{year}"])
        jobs=campaign.commands(args)
        first=runner.make_plan(runner.parser().parse_args(jobs[0]["argv"]))
        self.assertEqual(Path(first["pack"]).name,"miso_2023")

if __name__=="__main__":unittest.main()
