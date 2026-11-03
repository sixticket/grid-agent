"""A synthetic annual NASA pack exercises generation and launcher validation offline."""
import datetime as dt
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"tools"))
import run_case
spec=importlib.util.spec_from_file_location("nasa_builder",ROOT/"preprocessing/nasa_climate/build_packs.py")
builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)

class NasaTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();builder.configure_roots(self.root)
        self.source=builder.source_pack("miso",2030);(self.source/"profiles").mkdir(parents=True)
        self.shared=self.root/"experiment/shared";self.shared.mkdir(parents=True)
        for name in ("costs_us.json","electricity_policies.json"):(self.shared/name).write_text("{}")
        scenario=dict(name="miso_2030_baseline",region="miso",planning_year=2030,weather_year=2023,forecast={"load":1.1},fixed_context_sha256="fixed",assumptions=["2023 weather and hourly shape retained"])
        values={"config.json":dict(id="miso",description="Test",cost_file="costs_us.json",prediction=scenario),"network.json":{"nodes":[{"id":"A","profiles_csv":"profiles/A.csv"}]},"inventory.json":{},"assumptions.json":{}}
        for name,value in values.items():builder.write_json(self.source/name,value)
        for name in ("demand.csv","demand_audit.csv"):(self.source/name).write_text("test demand reference\n")
        origin=dt.datetime(2023,1,1)
        before="time,demand_MW,solar_cf,wind_cf\n"+"".join(f"{origin+dt.timedelta(hours=h):%Y-%m-%dT%H:%M:%S},10,0.2,0.3\n" for h in range(8760))
        (self.source/"profiles/A.csv").write_text(before)
        files={str(p.relative_to(self.source)):builder.sha(p) for p in self.source.rglob("*") if p.is_file()}
        builder.write_json(self.source/"prediction_manifest.json",dict(scenario=scenario,files=files,fixed_inputs={},sources={}))
        self.hourly=builder.CLIMATE/"profiles/MPI-ESM1-2-HR/ssp245/2030/miso";self.hourly.mkdir(parents=True)
        (self.hourly/"A.csv").write_text(before.replace(",0.2,0.3",",0.25,0.35"))
        (self.hourly/"hourly.txt").write_text("synthetic test hourly provenance")
        (self.hourly/"source.txt").write_text("synthetic daily/template source")
        builder.write_json(self.hourly/"validation.json",{"status":"validated"})
        def record(name):
            p=self.hourly/name
            return dict(path=builder._paths.reference(p,self.root),sha256=builder.sha(p))
        self.pr=dict(status="validated",source_kind=builder.SOURCE_KIND,model="MPI-ESM1-2-HR",scenario="ssp245",projection_year=2030,region="miso",node="A",profile_file=record("A.csv"),hourly_file=record("hourly.txt"),validation_file=record("validation.json"),source_files=[record("source.txt")])
        builder.write_json(self.hourly/"A.provenance.json",self.pr)

    def build(self):
        receipt=builder.build_pack("MPI-ESM1-2-HR",2030,"miso")
        return Path(receipt["pack"])

    def test_annual_build_reuse_and_case_validation(self):
        source_hash=builder.sha(self.source/"prediction_manifest.json")
        pack=self.build()
        self.assertEqual(builder.build_pack("MPI-ESM1-2-HR",2030,"miso")["action"],"validated_existing")
        args=run_case.parser().parse_args(["--deployment","sol","--region","miso","--year","2030","--agent","single","--pack",str(pack),"--shared",str(self.shared),"--data-root",str(self.root),"--output",str(self.root/"output")])
        inventory=run_case.check_inputs(run_case.make_plan(args))
        self.assertEqual(inventory["climate_validation"]["status"],"validated")
        self.assertEqual(inventory["prediction"]["weather_year"],2030)
        self.assertEqual(inventory["prediction"]["time_axis_year"],2023)
        self.assertEqual(builder.sha(self.source/"prediction_manifest.json"),source_hash)
        self.assertFalse((self.root/"output").exists())
        (pack/"profiles/A.csv").write_text((pack/"profiles/A.csv").read_text().replace(",0.25,",",0.26,",1))
        with self.assertRaisesRegex(ValueError,"checksum"):run_case.check_inputs(run_case.make_plan(args))

    def test_source_tampering_and_demand_change_rejected(self):
        (self.source/"demand.csv").write_text("changed")
        with self.assertRaisesRegex(ValueError,"source changed"):self.build()
        (self.hourly/"A.csv").write_text((self.hourly/"A.csv").read_text().replace(",10,",",11,",1))
        with self.assertRaisesRegex(ValueError,"Demand changed"):builder.compare_profile(self.hourly/"A.csv",self.source/"profiles/A.csv")

    def test_reference_prefixes_and_escape(self):
        path=self.hourly/"A.csv"
        value=builder._paths.reference(path,self.root)
        self.assertTrue(value.startswith("data://"));self.assertEqual(builder._paths.resolve_reference(value,self.root),path)
        for value in ("data://../escape","repo://../escape"):
            with self.assertRaisesRegex(ValueError,"escapes"):builder._paths.resolve_reference(value,self.root)

    def test_statistics_allow_only_roundoff(self):
        a={"A":dict(hours=8760,demand_GWh=124270.847,solar_cf_mean=0.2,wind_cf_mean=0.3,demand_unchanged=True,time_axis_year=2023)}
        b=json.loads(json.dumps(a));b["A"]["demand_GWh"]+=4e-10
        self.assertTrue(builder.summaries_match(a,b))
        b["A"]["demand_GWh"]+=0.01;self.assertFalse(builder.summaries_match(a,b))

    def test_plan_with_missing_data_needs_no_scientific_stack(self):
        missing=self.root/"not_created"
        result=subprocess.run([sys.executable,"-B",str(ROOT/"preprocessing/nasa_climate/build_packs.py"),"--data-root",str(missing),"--plan"],text=True,capture_output=True,check=True)
        data=json.loads(result.stdout)
        self.assertEqual(len(data["packs"]),16);self.assertFalse(data["files_written"]);self.assertFalse(missing.exists())

if __name__=="__main__":unittest.main()
