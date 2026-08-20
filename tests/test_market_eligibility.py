from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date

from product_decision_engine.domain.models import (
    AvailabilityObservation,
    OfferAvailability,
    ProductLifecycleObservation,
    ProductLifecycleStatus,
    VerificationStatus,
)
from product_decision_engine.market import FreshnessPolicy, audit_market_eligibility

from helpers import make_mono_catalog


AS_OF = date(2026, 8, 20)


def availability(
    *,
    state: OfferAvailability = OfferAvailability.IN_STOCK,
    observed_at: str = "2026-08-20",
    source: str = "fixture-a",
) -> AvailabilityObservation:
    return AvailabilityObservation(
        id=f"availability-{source}-{observed_at}-{state.value}",
        product_id="p1",
        source_provider_key=source,
        availability=state,
        observed_at=observed_at,
        verification_status=VerificationStatus.VERIFIED,
        source_url="fixture://availability",
    )


class MarketEligibilityTests(unittest.TestCase):
    def test_fresh_verified_basket_and_in_stock_device_are_eligible(self) -> None:
        catalog = make_mono_catalog()

        result = audit_market_eligibility(
            catalog,
            catalog.product("p1"),
            (availability(),),
            as_of=AS_OF,
        )

        self.assertTrue(result.eligible)
        self.assertEqual(result.blocking_reasons, ())
        self.assertEqual(result.warnings, ("lifecycle_unknown",))

    def test_stale_device_price_blocks_product(self) -> None:
        catalog = make_mono_catalog()
        stale_prices = tuple(
            replace(item, observed_at="2026-07-01")
            if item.entity_type == "product"
            else item
            for item in catalog.prices
        )
        catalog = replace(catalog, prices=stale_prices)

        result = audit_market_eligibility(
            catalog,
            catalog.product("p1"),
            (availability(),),
            as_of=AS_OF,
        )

        self.assertFalse(result.eligible)
        self.assertIn("stale_price:product:p1:50d", result.blocking_reasons)

    def test_stale_consumable_price_blocks_product(self) -> None:
        catalog = make_mono_catalog()
        stale_prices = tuple(
            replace(item, observed_at="2026-07-01")
            if item.entity_type == "consumable"
            else item
            for item in catalog.prices
        )
        catalog = replace(catalog, prices=stale_prices)

        result = audit_market_eligibility(
            catalog,
            catalog.product("p1"),
            (availability(),),
            as_of=AS_OF,
        )

        self.assertFalse(result.eligible)
        self.assertIn(
            "stale_price:consumable:p1-replacement:50d",
            result.blocking_reasons,
        )

    def test_missing_or_stale_availability_blocks_product(self) -> None:
        catalog = make_mono_catalog()

        missing = audit_market_eligibility(
            catalog,
            catalog.product("p1"),
            (),
            as_of=AS_OF,
        )
        stale = audit_market_eligibility(
            catalog,
            catalog.product("p1"),
            (availability(observed_at="2026-08-01"),),
            as_of=AS_OF,
        )

        self.assertIn("missing_verified_availability", missing.blocking_reasons)
        self.assertIn("stale_availability:19d", stale.blocking_reasons)

    def test_unavailable_product_blocks_even_with_fresh_price(self) -> None:
        catalog = make_mono_catalog()

        result = audit_market_eligibility(
            catalog,
            catalog.product("p1"),
            (availability(state=OfferAvailability.UNAVAILABLE),),
            as_of=AS_OF,
        )

        self.assertFalse(result.eligible)
        self.assertIn("not_in_stock:unavailable", result.blocking_reasons)

    def test_one_verified_in_stock_source_is_sufficient(self) -> None:
        catalog = make_mono_catalog()

        result = audit_market_eligibility(
            catalog,
            catalog.product("p1"),
            (
                availability(
                    state=OfferAvailability.UNAVAILABLE,
                    source="fixture-a",
                ),
                availability(source="fixture-b"),
            ),
            as_of=AS_OF,
        )

        self.assertTrue(result.eligible)

    def test_verified_discontinued_signal_blocks_product(self) -> None:
        catalog = make_mono_catalog()
        lifecycle = ProductLifecycleObservation(
            id="lifecycle-p1",
            product_id="p1",
            source_provider_key="manufacturer",
            lifecycle_status=ProductLifecycleStatus.DISCONTINUED,
            observed_at="2026-08-19",
            verification_status=VerificationStatus.VERIFIED,
            source_url="fixture://lifecycle",
        )

        result = audit_market_eligibility(
            catalog,
            catalog.product("p1"),
            (availability(),),
            (lifecycle,),
            as_of=AS_OF,
        )

        self.assertFalse(result.eligible)
        self.assertIn("product_discontinued", result.blocking_reasons)
        self.assertNotIn("lifecycle_unknown", result.warnings)

    def test_future_observations_do_not_pass_freshness_gate(self) -> None:
        catalog = make_mono_catalog()
        future_prices = tuple(
            replace(item, observed_at="2026-08-21") for item in catalog.prices
        )
        catalog = replace(catalog, prices=future_prices)

        result = audit_market_eligibility(
            catalog,
            catalog.product("p1"),
            (availability(observed_at="2026-08-21"),),
            as_of=AS_OF,
            policy=FreshnessPolicy(),
        )

        self.assertFalse(result.eligible)
        self.assertIn("future_price:product:p1", result.blocking_reasons)
        self.assertIn("future_availability_observation", result.blocking_reasons)


if __name__ == "__main__":
    unittest.main()
