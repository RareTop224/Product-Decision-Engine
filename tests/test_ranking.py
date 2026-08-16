from __future__ import annotations

import unittest

from product_decision_engine.domain.models import ProductType, UsageScenario
from product_decision_engine.ranking import find_break_even, rank_products

from helpers import make_mono_catalog, merge_catalogs


class RankingTests(unittest.TestCase):
    def test_hard_constraint_excludes_cheaper_unsuitable_product(self) -> None:
        cheap = make_mono_catalog(
            prefix="cheap",
            purchase_price=5_000,
            wifi=False,
        )
        suitable = make_mono_catalog(
            prefix="suitable",
            purchase_price=10_000,
            wifi=True,
        )
        catalog = merge_catalogs(cheap, suitable)
        usage = UsageScenario(
            id="wifi",
            name="Wi-Fi required",
            mono_pages_per_month=100,
            color_pages_per_month=0,
            ownership_months=12,
            require_wifi=True,
        )

        ranked = rank_products(catalog, usage)

        self.assertEqual([item.product.id for item in ranked], ["suitable"])

    def test_monthly_volume_excludes_undersized_product(self) -> None:
        weak = make_mono_catalog(
            prefix="weak",
            purchase_price=5_000,
            recommended_monthly_volume=100,
        )
        capable = make_mono_catalog(
            prefix="capable",
            purchase_price=10_000,
            recommended_monthly_volume=1_000,
        )
        usage = UsageScenario(
            id="high",
            name="High volume",
            mono_pages_per_month=500,
            color_pages_per_month=0,
            ownership_months=12,
        )

        ranked = rank_products(merge_catalogs(weak, capable), usage)

        self.assertEqual([item.product.id for item in ranked], ["capable"])

    def test_tco_ranking_can_reverse_purchase_price_order(self) -> None:
        cheap = make_mono_catalog(
            prefix="cheap",
            purchase_price=5_000,
            starter_yield=100,
            replacement_yield=500,
            replacement_price=2_000,
        )
        economical = make_mono_catalog(
            prefix="economical",
            purchase_price=12_000,
            starter_yield=1_000,
            replacement_yield=5_000,
            replacement_price=1_000,
        )
        usage = UsageScenario(
            id="long",
            name="Long horizon",
            mono_pages_per_month=500,
            color_pages_per_month=0,
            ownership_months=60,
        )

        ranked = rank_products(merge_catalogs(cheap, economical), usage)

        self.assertEqual(ranked[0].product.id, "economical")
        self.assertLess(ranked[0].tco.total_cost_rub, ranked[1].tco.total_cost_rub)

    def test_break_even_search_finds_discrete_crossing(self) -> None:
        cheap = make_mono_catalog(
            prefix="cheap",
            purchase_price=1_000,
            starter_yield=1,
            replacement_yield=100,
            replacement_price=100,
        )
        economical = make_mono_catalog(
            prefix="economical",
            purchase_price=2_000,
            starter_yield=1,
            replacement_yield=1_000,
            replacement_price=100,
        )
        template = UsageScenario(
            id="break-even",
            name="Break-even",
            mono_pages_per_month=0,
            color_pages_per_month=0,
            ownership_months=12,
        )

        point = find_break_even(
            merge_catalogs(cheap, economical),
            "cheap",
            "economical",
            template,
            max_mono_pages_per_month=500,
        )

        self.assertIsNotNone(point)
        assert point is not None
        self.assertGreater(point.pages_per_month, 0)


if __name__ == "__main__":
    unittest.main()

