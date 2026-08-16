from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from product_decision_engine.dataio import load_catalog, load_scenarios
from product_decision_engine.domain.models import UsageScenario
from product_decision_engine.evidence import audit_product
from product_decision_engine.evaluation.report import build_report
from product_decision_engine.tco import calculate_tco


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class DataAndReportTests(unittest.TestCase):
    def test_pilot_golden_dataset_and_scenarios_load(self) -> None:
        data_dir = PROJECT_ROOT / "data" / "golden"

        catalog = load_catalog(data_dir)
        scenarios = load_scenarios(data_dir / "scenarios.json")

        self.assertEqual(len(catalog.products), 12)
        self.assertEqual(len(catalog.prices), 41)
        self.assertEqual(len(catalog.evidence), 226)
        self.assertTrue(all(not audit_product(catalog, item).missing for item in catalog.products))
        conflict_audit = audit_product(
            catalog,
            catalog.product("canon-isensys-mf655cdw"),
        )
        self.assertEqual(len(conflict_audit.conflicts), 1)
        self.assertEqual(len(scenarios), 15)

    def test_g3411_exact_bundle_uses_two_starter_black_bottles(self) -> None:
        catalog = load_catalog(PROJECT_ROOT / "data" / "golden")
        exactly_covered = UsageScenario(
            id="g3411-exact-starter",
            name="Exact G3411 starter capacity",
            mono_pages_per_month=200,
            color_pages_per_month=0,
            ownership_months=60,
        )
        one_page_per_month_more = UsageScenario(
            id="g3411-over-starter",
            name="G3411 over starter capacity",
            mono_pages_per_month=201,
            color_pages_per_month=0,
            ownership_months=60,
        )

        covered = calculate_tco(catalog, "canon-pixma-g3411", exactly_covered)
        over = calculate_tco(catalog, "canon-pixma-g3411", one_page_per_month_more)
        covered_black = next(item for item in covered.components if item.channel == "black")
        over_black = next(item for item in over.components if item.channel == "black")

        self.assertEqual(covered_black.starter_capacity_pages, 12_000)
        self.assertEqual(covered_black.units_purchased, 0)
        self.assertEqual(over_black.units_purchased, 1)

    def test_incomplete_report_does_not_claim_go(self) -> None:
        data_dir = PROJECT_ROOT / "data" / "golden"
        catalog = load_catalog(data_dir)
        scenarios = load_scenarios(data_dir / "scenarios.json")

        report = build_report(catalog, scenarios)

        self.assertIn("Статус Фазы 0: `INCOMPLETE`", report)
        self.assertNotIn("Статус Фазы 0: `GO`", report)
        self.assertIn("рекомендуемый месячный объём не опубликован в датасете", report)
        self.assertIn("Конкурентных сценариев", report)
        self.assertIn("## Состав промежуточной выборки", report)
        self.assertIn("Рекомендация процесса: **`CONTINUE PHASE 0`**", report)
        self.assertIn("Медианный отрыв победителя от второго места", report)
        self.assertIn("Ближайшая альтернатива", report)
        self.assertIn("Чувствительность ranking к объёму", report)
        self.assertIn("## Break-even", report)
        self.assertIn("Источники рекомендации", report)
        self.assertIn("Публикационный пробел: `recommended_monthly_volume`", report)
        self.assertIn("Конфликт источников:", report)
        self.assertIn("## Ограничения текущей модели", report)
        self.assertNotIn("Phase 0 evaluation report", report)


if __name__ == "__main__":
    unittest.main()
