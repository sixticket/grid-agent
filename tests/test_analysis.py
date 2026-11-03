"""Offline evidence-contract tests; no private archive or scientific stack needed."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest

ANALYSIS = Path(__file__).resolve().parents[1] / "analysis"
sys.path.insert(0, str(ANALYSIS))
import common
from analyze_tokens import normalized_tokens


def reported_cohort():
    return [
        {
            "model": model,
            "year": year,
            "demand_year": year,
            "weather_year": year,
            "region": region,
            "agent": agent,
            "completed": True,
            "selected": {
                "acceptance": {"passes": True},
                "all_in_cost_billion": 10.25,
                "system_cost_billion": 10.0,
                "distribution_cost_billion": 0.25,
            },
        }
        for model in common.MODELS
        for year in common.YEARS
        for region in common.REGIONS
        for agent in ("single", "multi")
    ]


class CohortValidationTests(unittest.TestCase):
    def test_complete_model_qualified_matrix(self):
        cases = reported_cohort()
        identities = common.validate_cohort(cases)
        self.assertEqual(len(identities), 96)
        self.assertIn("gpt-6.1-sol/2030/miso/single", identities)
        self.assertIn("claude-opus-5-5/2030/miso/single", identities)

    def test_missing_duplicate_or_excluded_model_is_rejected(self):
        complete = reported_cohort()
        duplicate = copy.deepcopy(complete)
        duplicate[-1] = copy.deepcopy(duplicate[0])
        excluded = copy.deepcopy(complete)
        excluded[0]["model"] = "archived-deployment"
        for cases in (complete[:-1], duplicate, excluded):
            with self.subTest(cases=len(cases), model=cases[0]["model"]):
                with self.assertRaises(ValueError):
                    common.validate_cohort(cases)

    def test_weather_and_demand_years_must_match_cell(self):
        for field in ("weather_year", "demand_year"):
            cases = reported_cohort()
            cases[0][field] = 2030
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, "year mismatch"):
                    common.validate_cohort(cases)

    def test_incomplete_or_rejected_outcome_is_rejected(self):
        for field in ("completed", "accepted"):
            cases = reported_cohort()
            if field == "completed":
                cases[0]["completed"] = False
            else:
                cases[0]["selected"]["acceptance"]["passes"] = False
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, "Incomplete or rejected"):
                    common.validate_cohort(cases)

    def test_cost_must_be_finite_positive_numeric(self):
        for cost in (float("nan"), float("inf"), -1.0, 0.0, True, "10.25"):
            cases = reported_cohort()
            cases[0]["selected"]["all_in_cost_billion"] = cost
            with self.subTest(cost=cost):
                with self.assertRaisesRegex(ValueError, "Invalid selected annual cost"):
                    common.validate_cohort(cases)

    def test_cost_decomposition_must_reconcile(self):
        cases = reported_cohort()
        cases[0]["selected"]["distribution_cost_billion"] = 0.50
        with self.assertRaisesRegex(ValueError, "decomposition mismatch"):
            common.validate_cohort(cases)


class ArchiveAndPaperOutputTests(unittest.TestCase):
    def test_longest_prefix_override_even_when_original_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            old = root / "original"
            existing = old / "weather" / "case.json"
            existing.parent.mkdir(parents=True)
            existing.write_text("original source remains readable")
            resolver = common.ArchiveResolver(root / "relative", {
                str(old): str(root / "general-relocation"),
                str(old / "weather"): str(root / "specific-relocation"),
            })
            self.assertEqual(resolver(existing), root / "specific-relocation/case.json")
            self.assertEqual(resolver(old / "other.json"), root / "general-relocation/other.json")
            self.assertEqual(resolver("inputs/case.json"), root / "relative/inputs/case.json")
            self.assertEqual(existing.read_text(), "original source remains readable")

    def test_prefix_mapping_matches_path_components(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            resolver = common.ArchiveResolver(root, {str(root / "old"): str(root / "new")})
            self.assertEqual(resolver(root / "old-extra/case.json"), root / "old-extra/case.json")

    def test_paper_output_omits_api_money_and_preserves_power_objective(self):
        evidence = {
            "selected": {"all_in_cost_billion": 10.25, "system_cost_billion": 10.0},
            "metrics": {"estimated_llm_cost_usd": 4.2, "legacy_estimated_llm_cost_usd": 3.1,
                        "input_tokens": 100},
            "responses": [{"estimated_cost_usd": 1.5, "output_cost_usd": 0.2,
                           "pricing_source": "provider", "output_tokens": 25}],
        }
        original = copy.deepcopy(evidence)
        cleaned = common.paper_data(evidence)
        self.assertEqual(cleaned, {
            "selected": {"all_in_cost_billion": 10.25, "system_cost_billion": 10.0},
            "metrics": {"input_tokens": 100},
            "responses": [{"output_tokens": 25}],
        })
        self.assertEqual(evidence, original)


class TokenAccountingTests(unittest.TestCase):
    def setUp(self):
        self.row = {"input_tokens": 100, "uncached_input_tokens": 30,
                    "cache_read_tokens": 50, "cache_write_tokens": 20,
                    "output_tokens": 40, "reasoning_tokens": 25}

    def test_cache_is_in_input_and_reasoning_is_in_output(self):
        counts = normalized_tokens(self.row)
        self.assertEqual(counts["input_tokens"] + counts["output_tokens"], 140)
        self.assertEqual(counts["reasoning_tokens"], 25)
        self.assertEqual(set(counts), set(self.row))

    def test_cache_decomposition_mismatch_is_rejected(self):
        self.row["cache_read_tokens"] = 51
        with self.assertRaisesRegex(ValueError, "decomposition"):
            normalized_tokens(self.row)

    def test_reasoning_cannot_exceed_output(self):
        self.row["reasoning_tokens"] = 41
        with self.assertRaisesRegex(ValueError, "subset of output"):
            normalized_tokens(self.row)

    def test_token_counts_reject_negative_fractional_and_boolean_values(self):
        for value in (-1, 1.5, True, "1"):
            row = dict(self.row, reasoning_tokens=value)
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "nonnegative integers"):
                    normalized_tokens(row)


if __name__ == "__main__":
    unittest.main()
