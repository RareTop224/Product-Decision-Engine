from __future__ import annotations

import tempfile
import unittest
from collections import Counter
from dataclasses import replace
from pathlib import Path

from product_decision_engine.dataio import load_catalog, load_scenarios
from product_decision_engine.domain.models import UsageScenario
from product_decision_engine.evidence import audit_product
from product_decision_engine.evaluation.report import (
    build_report,
    evaluate_price_robustness,
    evaluate_scenario,
)
from product_decision_engine.tco import calculate_tco

from helpers import make_mono_catalog, merge_catalogs


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class DataAndReportTests(unittest.TestCase):
    def test_pilot_golden_dataset_and_scenarios_load(self) -> None:
        data_dir = PROJECT_ROOT / "data" / "golden"

        catalog = load_catalog(data_dir)
        scenarios = load_scenarios(data_dir / "scenarios.json")

        self.assertEqual(len(catalog.products), 30)
        self.assertEqual(len(catalog.prices), 108)
        self.assertEqual(len(catalog.evidence), 548)
        self.assertTrue(all(not audit_product(catalog, item).missing for item in catalog.products))
        primary_prices = Counter(
            (item.entity_type, item.entity_id)
            for item in catalog.prices
            if item.is_primary
        )
        all_price_entities = {
            (item.entity_type, item.entity_id) for item in catalog.prices
        }
        self.assertEqual(set(primary_prices), all_price_entities)
        self.assertTrue(all(count == 1 for count in primary_prices.values()))
        repeated_price_counts = Counter(
            (item.entity_type, item.entity_id) for item in catalog.prices
        )
        self.assertEqual(sum(count >= 2 for count in repeated_price_counts.values()), 21)
        price_stress_targets = {
            ("product", "canon-pixma-g1411"),
            ("consumable", "canon-gi-490-black"),
            ("consumable", "canon-gi-490-cyan"),
            ("consumable", "canon-gi-490-magenta"),
            ("consumable", "canon-gi-490-yellow"),
            ("product", "hp-smart-tank-580"),
            ("consumable", "hp-gt53xl-black"),
            ("consumable", "hp-gt52-cyan"),
            ("consumable", "hp-gt52-magenta"),
            ("consumable", "hp-gt52-yellow"),
            ("product", "epson-ecotank-l3250"),
            ("consumable", "epson-103-black"),
            ("consumable", "epson-103-cyan"),
            ("consumable", "epson-103-magenta"),
            ("consumable", "epson-103-yellow"),
            ("product", "pantum-p2500w"),
            ("consumable", "pantum-pc211p"),
        }
        self.assertTrue(
            all(repeated_price_counts[target] >= 2 for target in price_stress_targets)
        )
        conflict_audit = audit_product(
            catalog,
            catalog.product("canon-isensys-mf655cdw"),
        )
        self.assertEqual(len(conflict_audit.conflicts), 1)
        xerox_conflict = audit_product(catalog, catalog.product("xerox-b225"))
        self.assertEqual(len(xerox_conflict.conflicts), 1)
        xerox_b230_conflict = audit_product(catalog, catalog.product("xerox-b230"))
        self.assertEqual(len(xerox_b230_conflict.conflicts), 1)
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

    def test_new_bundles_preserve_exact_starter_capacity(self) -> None:
        catalog = load_catalog(PROJECT_ROOT / "data" / "golden")
        scenario = UsageScenario(
            id="new-bundle-check",
            name="New bundle check",
            mono_pages_per_month=200,
            color_pages_per_month=100,
            ownership_months=60,
        )

        g1411 = calculate_tco(catalog, "canon-pixma-g1411", scenario)
        hp_580 = calculate_tco(catalog, "hp-smart-tank-580", scenario)
        hp_720 = calculate_tco(catalog, "hp-smart-tank-720", scenario)

        g1411_black = next(item for item in g1411.components if item.channel == "black")
        hp_580_cyan = next(item for item in hp_580.components if item.channel == "cyan")
        hp_720_cyan = next(item for item in hp_720.components if item.channel == "cyan")
        self.assertEqual(g1411_black.starter_capacity_pages, 12_000)
        self.assertEqual(g1411_black.units_purchased, 1)
        self.assertEqual(hp_580_cyan.starter_capacity_pages, 6_000)
        self.assertEqual(hp_720_cyan.starter_capacity_pages, 8_000)

    def test_phase0_report_marks_current_sample_for_reassessment(self) -> None:
        data_dir = PROJECT_ROOT / "data" / "golden"
        catalog = load_catalog(data_dir)
        scenarios = load_scenarios(data_dir / "scenarios.json")

        report = build_report(catalog, scenarios)

        self.assertIn("Статус Фазы 0: `REASSESS`", report)
        self.assertNotIn("Статус Фазы 0: `GO`", report)
        self.assertIn("рекомендуемый месячный объём не опубликован в датасете", report)
        self.assertIn("Конкурентных сценариев", report)
        self.assertIn("## Состав промежуточной выборки", report)
        self.assertIn("Уникальных экономических конфигураций расходников", report)
        self.assertIn("Рекомендация процесса: **`REASSESS BEFORE PRODUCT BUILD`**", report)
        self.assertIn("Медианный отрыв победителя от второго места", report)
        self.assertIn("Полный расчёт меняет победителя упрощённого TCO: 1 / 15", report)
        self.assertIn("Концентрация baseline по цене покупки", report)
        self.assertIn("Ближайшая альтернатива", report)
        self.assertIn("Чувствительность ranking к объёму", report)
        self.assertIn("## Break-even", report)
        self.assertIn("Источники рекомендации", report)
        self.assertIn("Публикационный пробел: `recommended_monthly_volume`", report)
        self.assertIn("Конфликт источников:", report)
        self.assertIn("## Ограничения текущей модели", report)
        self.assertIn("Предварительный стресс-тест не меняет рекомендацию", report)
        self.assertIn("Концентрация бренда среди победителей", report)
        self.assertIn("Покрытие повторными ценовыми наблюдениями", report)
        self.assertIn("Проверка ценового диапазона", report)
        self.assertIn("Покрытие повторными ценовыми наблюдениями: 21 / 85", report)
        self.assertIn("Чувствительность к доле цветной печати и горизонту владения", report)
        self.assertIn("фиксированные 750 страниц в месяц", report)
        self.assertIn("1 / 12", report)
        self.assertIn("переключение победителя внутри 0 из 2", report)
        self.assertIn("предварительную устойчивость в 4 из 15", report)
        self.assertIn("Ценовая устойчивость той же матрицы", report)
        self.assertIn("0 / 12 точек с диапазоном", report)
        self.assertIn("5 / 12 точек с диапазоном", report)
        self.assertNotIn("Phase 0 evaluation report", report)

    def test_price_robustness_detects_observed_winner_flip(self) -> None:
        winner_catalog = make_mono_catalog(prefix="winner", purchase_price=10_000)
        challenger_catalog = make_mono_catalog(prefix="challenger", purchase_price=11_000)
        primary_price = winner_catalog.prices[0]
        secondary_price = replace(
            primary_price,
            id="winner-product-price-secondary",
            price_rub=12_000,
            source_id="winner-product-price-secondary-evidence",
            is_primary=False,
        )
        primary_evidence = next(
            item
            for item in winner_catalog.evidence
            if item.entity_type == "price_observation"
            and item.entity_id == primary_price.id
        )
        secondary_evidence = replace(
            primary_evidence,
            id=secondary_price.source_id,
            entity_id=secondary_price.id,
        )
        winner_catalog = replace(
            winner_catalog,
            prices=(*winner_catalog.prices, secondary_price),
            evidence=(*winner_catalog.evidence, secondary_evidence),
        )
        catalog = merge_catalogs(winner_catalog, challenger_catalog)
        scenario = UsageScenario(
            id="price-range",
            name="Price range",
            mono_pages_per_month=0,
            color_pages_per_month=0,
            ownership_months=60,
        )

        result = evaluate_scenario(catalog, scenario)
        robustness = evaluate_price_robustness(catalog, result)

        self.assertEqual(result.decision_engine_winner.id, "winner")
        self.assertIsNotNone(robustness)
        assert robustness is not None
        self.assertTrue(robustness.has_observed_range)
        self.assertFalse(robustness.robust)
        self.assertEqual(robustness.winner_tco_max_rub, 12_000)
        self.assertEqual(robustness.best_challenger_id, "challenger")
        self.assertEqual(robustness.best_challenger_tco_min_rub, 11_000)


if __name__ == "__main__":
    unittest.main()
