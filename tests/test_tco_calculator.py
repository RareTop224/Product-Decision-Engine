from __future__ import annotations

import unittest

from product_decision_engine.domain.catalog import Catalog, MissingCriticalData
from product_decision_engine.domain.models import (
    ColorMode,
    UsageScenario,
)
from product_decision_engine.tco.calculator import calculate_simplified_tco, calculate_tco

from helpers import make_mono_catalog


def scenario(pages: int, months: int = 1) -> UsageScenario:
    return UsageScenario(
        id=f"s-{pages}-{months}",
        name="Fixture scenario",
        mono_pages_per_month=pages,
        color_pages_per_month=0,
        ownership_months=months,
    )


class TcoCalculatorTests(unittest.TestCase):
    def test_starter_yield_fully_covers_usage(self) -> None:
        catalog = make_mono_catalog(starter_yield=500)

        result = calculate_tco(catalog, "p1", scenario(500))

        self.assertEqual(result.purchase_cost_rub, 10_000)
        self.assertEqual(result.consumables_cost_rub, 0)
        self.assertEqual(result.total_cost_rub, 10_000)
        self.assertEqual(result.components[0].units_purchased, 0)

    def test_usage_exactly_one_replacement_yield_after_starter(self) -> None:
        catalog = make_mono_catalog(starter_yield=500, replacement_yield=1_000)

        result = calculate_tco(catalog, "p1", scenario(1_500))

        self.assertEqual(result.components[0].units_purchased, 1)
        self.assertEqual(result.consumables_cost_rub, 1_000)

    def test_one_page_over_replacement_yield_buys_another_unit(self) -> None:
        catalog = make_mono_catalog(starter_yield=500, replacement_yield=1_000)

        result = calculate_tco(catalog, "p1", scenario(1_501))

        self.assertEqual(result.components[0].units_purchased, 2)
        self.assertEqual(result.consumables_cost_rub, 2_000)

    def test_zero_pages_buys_no_consumables_or_maintenance(self) -> None:
        catalog = make_mono_catalog(drum_yield=1_000)

        result = calculate_tco(catalog, "p1", scenario(0, months=60))

        self.assertEqual(result.consumables_cost_rub, 0)
        self.assertEqual(result.maintenance_cost_rub, 0)
        self.assertEqual(result.total_cost_rub, 10_000)

    def test_one_month_one_year_and_five_years(self) -> None:
        catalog = make_mono_catalog(starter_yield=500, replacement_yield=1_000)
        expected_units = {1: 0, 12: 1, 60: 6}

        for months, units in expected_units.items():
            with self.subTest(months=months):
                result = calculate_tco(catalog, "p1", scenario(100, months))
                self.assertEqual(result.components[0].units_purchased, units)

    def test_drum_is_not_bought_at_exact_installed_yield(self) -> None:
        catalog = make_mono_catalog(drum_yield=1_000, drum_price=2_000)

        result = calculate_tco(catalog, "p1", scenario(1_000))

        self.assertEqual(result.maintenance_cost_rub, 0)

    def test_drum_is_bought_when_installed_yield_is_exceeded(self) -> None:
        catalog = make_mono_catalog(drum_yield=1_000, drum_price=2_000)

        result = calculate_tco(catalog, "p1", scenario(1_001))

        self.assertEqual(result.maintenance_cost_rub, 2_000)
        drum = next(item for item in result.components if item.channel == "drum")
        self.assertEqual(drum.units_purchased, 1)

    def test_missing_color_channel_is_critical_data_error(self) -> None:
        base = make_mono_catalog()
        color_product = base.products[0].__class__(
            **{
                **{field: getattr(base.products[0], field) for field in base.products[0].__dataclass_fields__},
                "color_mode": ColorMode.COLOR,
                "expected_consumable_channels": ("black", "color"),
            }
        )
        catalog = Catalog(
            products=(color_product,),
            consumables=base.consumables,
            product_consumables=base.product_consumables,
            prices=base.prices,
            evidence=base.evidence,
        )
        color_scenario = UsageScenario(
            id="color",
            name="Color",
            mono_pages_per_month=10,
            color_pages_per_month=10,
            ownership_months=12,
        )

        with self.assertRaises(MissingCriticalData):
            calculate_tco(catalog, "p1", color_scenario, require_verified_evidence=False)

    def test_missing_replacement_price_is_critical_data_error(self) -> None:
        base = make_mono_catalog()
        catalog = Catalog(
            products=base.products,
            consumables=base.consumables,
            product_consumables=base.product_consumables,
            prices=(base.prices[0],),
            evidence=base.evidence,
        )

        with self.assertRaises(MissingCriticalData):
            calculate_tco(catalog, "p1", scenario(600), require_verified_evidence=False)

    def test_conflicting_evidence_is_not_treated_as_verified(self) -> None:
        catalog = make_mono_catalog(verified=False)

        with self.assertRaises(MissingCriticalData):
            calculate_tco(catalog, "p1", scenario(600))

    def test_simplified_baseline_ignores_starter(self) -> None:
        catalog = make_mono_catalog(starter_yield=500, replacement_yield=1_000)

        full = calculate_tco(catalog, "p1", scenario(500))
        simplified = calculate_simplified_tco(catalog, "p1", scenario(500))

        self.assertEqual(full.consumables_cost_rub, 0)
        self.assertEqual(simplified.consumables_cost_rub, 1_000)


if __name__ == "__main__":
    unittest.main()

