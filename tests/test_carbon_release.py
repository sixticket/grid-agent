"""Carbon launch routing and exact 40-trajectory campaign coverage, offline."""
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import run_case
import run_campaign


class CarbonReleaseTests(unittest.TestCase):
    def plan(self, region='nyiso', year=2023, deployment='opus'):
        args = run_case.parser().parse_args([
            '--co2', '--deployment', deployment, '--region', region, '--year', str(year),
            '--agent', 'single', '--pack', '/example/pack', '--shared', '/example/shared',
            '--output', '/example/output'])
        return run_case.make_plan(args)

    def test_carbon_baseline_uses_hosted_64k_runtime(self):
        plan = self.plan()
        self.assertEqual(plan['profile'], 'runtime_original')
        self.assertEqual(plan['output_tokens'], 64000)
        self.assertEqual(plan['co2_case']['id'], '2023/nyiso')
        self.assertAlmostEqual(plan['co2_case']['co2_limit_Mt'], 24.76186511876616)
        self.assertEqual(self.plan('south_africa', 2030)['profile'], 'runtime_calendar')

    def test_unreported_cases_cannot_receive_invented_caps(self):
        with self.assertRaisesRegex(ValueError, 'ten published'):
            self.plan('ercot', 2030)

    def test_campaign_matches_exact_carbon_matrix(self):
        args = run_campaign.parser().parse_args(['--co2', '--output', '/example/output'])
        jobs = run_campaign.commands(args)
        cases = json.loads((ROOT / 'experiments/carbon/cases.json').read_text())['cases']
        expected = {f'{m}/{c["year"]}/{c["region"]}/{a}' for c in cases
                    for m in ('sol', 'opus') for a in ('single', 'multi')}
        self.assertEqual(len(jobs), 40)
        self.assertEqual({j['case_id'] for j in jobs}, expected)
        self.assertTrue(all('--co2' in j['argv'] for j in jobs))
        self.assertEqual(len(run_campaign.commands(run_campaign.parser().parse_args(
            ['--output', '/example/reference']))), 96)
