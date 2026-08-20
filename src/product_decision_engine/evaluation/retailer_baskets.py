from __future__ import annotations

from dataclasses import dataclass

from product_decision_engine.domain.catalog import Catalog, MissingCriticalData
from product_decision_engine.domain.models import (
    Product,
    RetailerBasketAudit,
    UsageScenario,
    VerificationStatus,
)
from product_decision_engine.ranking.engine import evaluate_eligibility
from product_decision_engine.tco.calculator import (
    TcoBreakdown,
    calculate_simplified_tco,
    calculate_tco,
)


@dataclass(frozen=True, slots=True)
class RetailerProductResult:
    product: Product
    full_tco: TcoBreakdown
    simplified_tco: TcoBreakdown


@dataclass(frozen=True, slots=True)
class RetailerBasketResult:
    audit: RetailerBasketAudit
    scenario: UsageScenario
    products: tuple[RetailerProductResult, ...]
    purchase_price_winner: RetailerProductResult
    simplified_tco_winner: RetailerProductResult
    decision_engine_winner: RetailerProductResult

    def product_result(self, product_id: str) -> RetailerProductResult:
        matches = [item for item in self.products if item.product.id == product_id]
        if len(matches) != 1:
            raise ValueError(f"Expected one basket result for {product_id!r}")
        return matches[0]


def evaluate_retailer_basket(
    catalog: Catalog,
    scenario: UsageScenario,
    audit: RetailerBasketAudit,
) -> RetailerBasketResult:
    if audit.scenario_id != scenario.id:
        raise ValueError("retailer basket audit and scenario ids must match")
    if audit.verification_status != VerificationStatus.VERIFIED:
        raise MissingCriticalData("retailer basket audit is not verified")
    if not audit.complete:
        raise MissingCriticalData("retailer basket audit is incomplete")

    calculated: list[RetailerProductResult] = []
    for offer in audit.offers:
        product = catalog.product(offer.product_id)
        eligibility = evaluate_eligibility(catalog, product, scenario)
        if not eligibility.eligible:
            raise MissingCriticalData(
                f"{product.id} is not eligible for {scenario.id}: "
                + "; ".join(eligibility.reasons)
            )

        assert offer.device_price_rub is not None
        price_overrides = {
            ("product", product.id): offer.device_price_rub,
            **{
                ("consumable", consumable_id): price_rub
                for consumable_id, price_rub in offer.consumable_prices_rub
            },
        }
        full = calculate_tco(
            catalog,
            product.id,
            scenario,
            price_overrides_rub=price_overrides,
        )
        simplified = calculate_simplified_tco(
            catalog,
            product.id,
            scenario,
            price_overrides_rub=price_overrides,
        )
        scenario_required = {
            component.consumable_id
            for breakdown in (full, simplified)
            for component in breakdown.components
            if component.units_purchased > 0
        }
        declared_required = set(offer.required_consumable_ids)
        if scenario_required != declared_required:
            raise ValueError(
                f"scenario-specific basket mismatch for {product.id}: "
                f"calculated {sorted(scenario_required)}, "
                f"declared {sorted(declared_required)}"
            )
        calculated.append(
            RetailerProductResult(
                product=product,
                full_tco=full,
                simplified_tco=simplified,
            )
        )

    purchase_winner = min(
        calculated,
        key=lambda item: (item.full_tco.purchase_cost_rub, item.product.id),
    )
    simplified_winner = min(
        calculated,
        key=lambda item: (item.simplified_tco.total_cost_rub, item.product.id),
    )
    decision_winner = min(
        calculated,
        key=lambda item: (
            item.full_tco.total_cost_rub,
            item.full_tco.purchase_cost_rub,
            item.product.id,
        ),
    )
    return RetailerBasketResult(
        audit=audit,
        scenario=scenario,
        products=tuple(calculated),
        purchase_price_winner=purchase_winner,
        simplified_tco_winner=simplified_winner,
        decision_engine_winner=decision_winner,
    )
