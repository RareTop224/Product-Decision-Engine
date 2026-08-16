from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from product_decision_engine.dataio import load_catalog, load_scenarios
from product_decision_engine.evidence import audit_product
from product_decision_engine.evaluation.report import build_report


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class DataAndReportTests(unittest.TestCase):
    def test_pilot_golden_dataset_and_scenarios_load(self) -> None:
        data_dir = PROJECT_ROOT / "data" / "golden"

        catalog = load_catalog(data_dir)
        scenarios = load_scenarios(data_dir / "scenarios.json")

        self.assertEqual(len(catalog.products), 5)
        self.assertEqual(len(catalog.prices), 18)
        self.assertEqual(len(catalog.evidence), 94)
        self.assertTrue(all(not audit_product(catalog, item).missing for item in catalog.products))
        self.assertEqual(len(scenarios), 15)

    def test_incomplete_report_does_not_claim_go(self) -> None:
        data_dir = PROJECT_ROOT / "data" / "golden"
        catalog = load_catalog(data_dir)
        scenarios = load_scenarios(data_dir / "scenarios.json")

        report = build_report(catalog, scenarios)

        self.assertIn("Статус Фазы 0: `INCOMPLETE`", report)
        self.assertNotIn("Статус Фазы 0: `GO`", report)
        self.assertIn("рекомендуемый месячный объём не опубликован в датасете", report)
        self.assertNotIn("Phase 0 evaluation report", report)


if __name__ == "__main__":
    unittest.main()
