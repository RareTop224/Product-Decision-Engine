from __future__ import annotations

import tempfile
import unittest
from collections import Counter
from dataclasses import replace
from pathlib import Path

from product_decision_engine.dataio import (
    load_catalog,
    load_retailer_basket_audits,
    load_scenarios,
)
from product_decision_engine.domain.models import UsageScenario
from product_decision_engine.evidence import audit_product
from product_decision_engine.evaluation.report import (
    build_report,
    evaluate_full_tco_ablation,
    evaluate_price_robustness,
    evaluate_retailer_pair_sensitivity,
    evaluate_scenario,
)
from product_decision_engine.evaluation.retailer_baskets import (
    evaluate_retailer_basket,
)
from product_decision_engine.tco import calculate_tco

from helpers import make_mono_catalog, merge_catalogs


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class DataAndReportTests(unittest.TestCase):
    def test_pilot_golden_dataset_and_scenarios_load(self) -> None:
        data_dir = PROJECT_ROOT / "data" / "golden"

        catalog = load_catalog(data_dir)
        scenarios = load_scenarios(data_dir / "scenarios.json")
        basket_audits = load_retailer_basket_audits(
            data_dir / "retailer_basket_audits.json"
        )

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
        self.assertEqual(len(basket_audits), 7)
        self.assertEqual(sum(audit.complete for audit in basket_audits), 4)
        teacher_kns_audit = next(
            audit for audit in basket_audits if audit.id == "teacher-pair-kns-20260820"
        )
        self.assertTrue(
            all(offer.consumables_covered for offer in teacher_kns_audit.offers)
        )
        self.assertTrue(
            all(not offer.consumables_complete for offer in teacher_kns_audit.offers)
        )
        self.assertFalse(teacher_kns_audit.complete)

    def test_retailer_baskets_preserve_winner_across_two_sellers(self) -> None:
        data_dir = PROJECT_ROOT / "data" / "golden"
        catalog = load_catalog(data_dir)
        scenario = next(
            item
            for item in load_scenarios(data_dir / "scenarios.json")
            if item.id == "home-light-color"
        )
        audits = {
            audit.retailer: audit
            for audit in load_retailer_basket_audits(
                data_dir / "retailer_basket_audits.json"
            )
            if audit.scenario_id == scenario.id
            and {
                offer.product_id for offer in audit.offers
            }
            == {
                "canon-pixma-ts3640",
                "hp-deskjet-ink-advantage-2875",
            }
        }

        expected = {
            "KNS": (39_483, 29_807, 31_954, 9_676),
            "Regard": (45_250, 33_460, 35_860, 11_790),
        }
        for retailer, (
            canon_tco,
            hp_tco,
            hp_simplified_tco,
            savings,
        ) in expected.items():
            with self.subTest(retailer=retailer):
                result = evaluate_retailer_basket(
                    catalog, scenario, audits[retailer]
                )
                self.assertEqual(
                    result.purchase_price_winner.product.id,
                    "canon-pixma-ts3640",
                )
                self.assertEqual(
                    result.simplified_tco_winner.product.id,
                    "hp-deskjet-ink-advantage-2875",
                )
                self.assertEqual(
                    result.decision_engine_winner.product.id,
                    "hp-deskjet-ink-advantage-2875",
                )
                self.assertEqual(
                    result.product_result("canon-pixma-ts3640").full_tco.total_cost_rub,
                    canon_tco,
                )
                hp = result.product_result("hp-deskjet-ink-advantage-2875")
                self.assertEqual(hp.full_tco.total_cost_rub, hp_tco)
                self.assertEqual(
                    hp.simplified_tco.total_cost_rub, hp_simplified_tco
                )
                self.assertEqual(canon_tco - hp_tco, savings)
                self.assertEqual(
                    tuple(
                        (component.consumable_id, component.units_purchased)
                        for component in hp.full_tco.components
                    ),
                    (("hp-653-black", 8), ("hp-653-tricolor", 3)),
                )

    def test_retailer_basket_rejects_scenario_incomplete_consumable_set(self) -> None:
        data_dir = PROJECT_ROOT / "data" / "golden"
        catalog = load_catalog(data_dir)
        scenario = next(
            item
            for item in load_scenarios(data_dir / "scenarios.json")
            if item.id == "home-light-color"
        )
        audit = next(
            item
            for item in load_retailer_basket_audits(
                data_dir / "retailer_basket_audits.json"
            )
            if item.id == "home-light-pair-kns-20260820"
        )
        canon = audit.offers[0]
        incomplete_canon = replace(
            canon,
            required_consumable_ids=("canon-pg-445",),
            covered_consumable_ids=("canon-pg-445",),
            consumable_source_urls=(canon.consumable_source_urls[0],),
            consumable_prices_rub=(canon.consumable_prices_rub[0],),
        )
        misleadingly_complete = replace(
            audit, offers=(incomplete_canon, audit.offers[1])
        )

        self.assertTrue(misleadingly_complete.complete)
        with self.assertRaisesRegex(ValueError, "scenario-specific basket mismatch"):
            evaluate_retailer_basket(catalog, scenario, misleadingly_complete)

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

    def test_retailer_pair_sensitivity_reproduces_switches(self) -> None:
        data_dir = PROJECT_ROOT / "data" / "golden"
        catalog = load_catalog(data_dir)
        scenario = next(
            item
            for item in load_scenarios(data_dir / "scenarios.json")
            if item.id == "home-light-color"
        )
        audits = load_retailer_basket_audits(
            data_dir / "retailer_basket_audits.json"
        )
        expected = {
            frozenset(
                {"canon-pixma-ts3640", "hp-deskjet-ink-advantage-2875"}
            ): (23, 15),
            frozenset({"canon-pixma-ts3640", "hp-smart-tank-580"}): (24, 2),
        }

        for product_ids, (agreement_points, full_changes) in expected.items():
            group = tuple(
                audit
                for audit in audits
                if frozenset(offer.product_id for offer in audit.offers)
                == product_ids
            )
            with self.subTest(product_ids=sorted(product_ids)):
                result = evaluate_retailer_pair_sensitivity(
                    catalog, scenario, group
                )
                self.assertTrue(result.confirmed)
                self.assertEqual(result.agreement_points, agreement_points)
                self.assertEqual(result.total_points, 24)
                self.assertEqual(result.full_vs_simplified_changes, full_changes)
                self.assertEqual(result.result_count, 48)

    def test_phase0_report_marks_current_sample_go_after_pair_gate(self) -> None:
        data_dir = PROJECT_ROOT / "data" / "golden"
        catalog = load_catalog(data_dir)
        scenarios = load_scenarios(data_dir / "scenarios.json")
        basket_audits = load_retailer_basket_audits(
            data_dir / "retailer_basket_audits.json"
        )

        report = build_report(catalog, scenarios, basket_audits)

        self.assertIn("Статус Фазы 0: `GO`", report)
        self.assertNotIn("Статус Фазы 0: `REASSESS`", report)
        self.assertIn("рекомендуемый месячный объём не опубликован в датасете", report)
        self.assertIn("Конкурентных сценариев", report)
        self.assertIn("## Состав промежуточной выборки", report)
        self.assertIn("Уникальных экономических конфигураций расходников", report)
        self.assertIn("Рекомендация процесса: **`GO TO NEXT-PHASE DECISION`**", report)
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
        self.assertIn("Ценовая устойчивость той же матрицы", report)
        self.assertIn("0 / 12 точек с диапазоном", report)
        self.assertIn("5 / 12 точек с диапазоном", report)
        self.assertIn("## Проверка синхронных корзин одного продавца", report)
        self.assertIn("полных одновременно покупаемых корзин — **4 из 7**", report)
        self.assertIn(
            "Смена победителя относительно цены покупки в полных корзинах: 4 / 4",
            report,
        )
        self.assertIn(
            "Смена победителя полного TCO относительно упрощённого в полных корзинах: 0 / 4",
            report,
        )
        self.assertIn("Basket-aware TCO не рассчитан", report)
        self.assertIn("Canon PIXMA TS3640 ↔ HP DeskJet Ink Advantage 2875", report)
        self.assertIn("HP DeskJet Ink Advantage 2875 — 29 807 ₽", report)
        self.assertIn("HP DeskJet Ink Advantage 2875 — 33 460 ₽", report)
        self.assertIn("9 676 ₽ (24,5%)", report)
        self.assertIn("11 790 ₽ (26,1%)", report)
        self.assertIn("полный TCO рассчитан без смешивания магазинов", report)
        self.assertIn("## Решающая многоточечная проверка пар", report)
        self.assertIn("подтверждена для **2 из 2 пар**", report)
        self.assertIn("Совпадение продавцов: 23 / 24 точек", report)
        self.assertIn("Совпадение продавцов: 24 / 24 точек", report)
        self.assertIn("17 из 96 парных расчётов", report)
        self.assertIn("## Ablation полного TCO", report)
        self.assertIn("Упрощённый baseline даёт **Epson EcoTank L4260** преимущество 3 209 ₽", report)
        self.assertIn("уменьшает обязательные покупки на 3 670 ₽ больше", report)
        self.assertIn("выигрывает всего 461 ₽", report)
        self.assertNotIn("Phase 0 evaluation report", report)

    def test_full_tco_ablation_reconstructs_teacher_winner_flip(self) -> None:
        data_dir = PROJECT_ROOT / "data" / "golden"
        catalog = load_catalog(data_dir)
        scenario = next(
            item
            for item in load_scenarios(data_dir / "scenarios.json")
            if item.id == "teacher-mixed"
        )

        hp = evaluate_full_tco_ablation(
            catalog, catalog.product("hp-smart-tank-720"), scenario
        )
        epson = evaluate_full_tco_ablation(
            catalog, catalog.product("epson-ecotank-l4260"), scenario
        )

        self.assertEqual(
            (hp.simplified_tco_rub, hp.starter_credit_rub, hp.maintenance_cost_rub, hp.full_tco_rub),
            (41_927, 6_876, 0, 35_051),
        )
        self.assertEqual(
            (
                epson.simplified_tco_rub,
                epson.starter_credit_rub,
                epson.maintenance_cost_rub,
                epson.full_tco_rub,
            ),
            (38_718, 3_206, 0, 35_512),
        )

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
