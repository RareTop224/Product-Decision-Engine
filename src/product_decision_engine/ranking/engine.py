from __future__ import annotations

from dataclasses import dataclass

from product_decision_engine.domain.catalog import Catalog, MissingCriticalData
from product_decision_engine.domain.models import (
    ColorMode,
    MaintenanceDataStatus,
    Product,
    ProductType,
    UsageScenario,
)
from product_decision_engine.tco.calculator import TcoBreakdown, calculate_tco


@dataclass(frozen=True, slots=True)
class Eligibility:
    eligible: bool
    reasons: tuple[str, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RankedProduct:
    rank: int
    product: Product
    tco: TcoBreakdown


def evaluate_eligibility(
    catalog: Catalog,
    product: Product,
    scenario: UsageScenario,
) -> Eligibility:
    reasons: list[str] = []
    warnings: list[str] = []

    if scenario.color_pages_per_month > 0 and product.color_mode != ColorMode.COLOR:
        reasons.append("color printing required")
    if scenario.require_mfp and product.product_type != ProductType.MFP:
        reasons.append("MFP required")
    if scenario.require_wifi and not product.wifi:
        reasons.append("Wi-Fi required")
    if scenario.require_auto_duplex and not product.auto_duplex:
        reasons.append("automatic duplex required")
    if (
        product.recommended_monthly_volume is not None
        and scenario.monthly_pages > product.recommended_monthly_volume
    ):
        reasons.append("monthly usage exceeds published recommended volume")
    if product.recommended_monthly_volume is None:
        warnings.append("recommended monthly volume is not published in dataset")
    if product.maintenance_data_status == MaintenanceDataStatus.NOT_PUBLISHED:
        warnings.append("maintenance schedule is not published in dataset")

    try:
        purchase_price = catalog.latest_price("product", product.id).price_rub
    except MissingCriticalData:
        reasons.append("purchase price is missing")
    else:
        if (
            scenario.max_purchase_price_rub is not None
            and purchase_price > scenario.max_purchase_price_rub
        ):
            reasons.append("purchase price exceeds budget")

    return Eligibility(eligible=not reasons, reasons=tuple(reasons), warnings=tuple(warnings))


def rank_products(
    catalog: Catalog,
    scenario: UsageScenario,
    *,
    require_verified_evidence: bool = True,
) -> tuple[RankedProduct, ...]:
    calculated: list[tuple[Product, TcoBreakdown]] = []
    for product in catalog.products:
        if product.status != "active":
            continue
        eligibility = evaluate_eligibility(catalog, product, scenario)
        if not eligibility.eligible:
            continue
        try:
            tco = calculate_tco(
                catalog,
                product.id,
                scenario,
                require_verified_evidence=require_verified_evidence,
            )
        except MissingCriticalData:
            continue
        calculated.append((product, tco))

    calculated.sort(
        key=lambda item: (item[1].total_cost_rub, item[1].purchase_cost_rub, item[0].id)
    )
    return tuple(
        RankedProduct(rank=index, product=product, tco=tco)
        for index, (product, tco) in enumerate(calculated, start=1)
    )
