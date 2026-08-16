from __future__ import annotations

from dataclasses import dataclass, replace

from product_decision_engine.domain.catalog import Catalog, MissingCriticalData
from product_decision_engine.domain.models import UsageScenario
from product_decision_engine.ranking.engine import evaluate_eligibility
from product_decision_engine.tco.calculator import calculate_tco


@dataclass(frozen=True, slots=True)
class BreakEvenPoint:
    pages_per_month: int
    product_a_tco_rub: int
    product_b_tco_rub: int


def find_break_even(
    catalog: Catalog,
    product_a_id: str,
    product_b_id: str,
    scenario_template: UsageScenario,
    *,
    min_mono_pages_per_month: int = 0,
    max_mono_pages_per_month: int = 5_000,
    step: int = 1,
    require_verified_evidence: bool = True,
) -> BreakEvenPoint | None:
    """Find the first volume where the initially costlier option is no longer costlier."""
    if min_mono_pages_per_month < 0 or max_mono_pages_per_month < min_mono_pages_per_month:
        raise ValueError("invalid break-even search range")
    if step <= 0:
        raise ValueError("step must be positive")

    product_a = catalog.product(product_a_id)
    product_b = catalog.product(product_b_id)
    previous_difference: int | None = None

    for pages in range(min_mono_pages_per_month, max_mono_pages_per_month + 1, step):
        scenario = replace(scenario_template, mono_pages_per_month=pages)
        if not evaluate_eligibility(catalog, product_a, scenario).eligible:
            continue
        if not evaluate_eligibility(catalog, product_b, scenario).eligible:
            continue
        try:
            a_tco = calculate_tco(
                catalog,
                product_a_id,
                scenario,
                require_verified_evidence=require_verified_evidence,
            )
            b_tco = calculate_tco(
                catalog,
                product_b_id,
                scenario,
                require_verified_evidence=require_verified_evidence,
            )
        except MissingCriticalData:
            return None

        difference = a_tco.total_cost_rub - b_tco.total_cost_rub
        if previous_difference is not None and (
            difference == 0
            or (previous_difference < 0 < difference)
            or (previous_difference > 0 > difference)
        ):
            return BreakEvenPoint(
                pages_per_month=pages,
                product_a_tco_rub=a_tco.total_cost_rub,
                product_b_tco_rub=b_tco.total_cost_rub,
            )
        previous_difference = difference
    return None

