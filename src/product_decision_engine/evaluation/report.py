from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from statistics import median

from product_decision_engine.domain.catalog import Catalog, MissingCriticalData
from product_decision_engine.domain.models import Product, UsageScenario
from product_decision_engine.evidence.audit import audit_product
from product_decision_engine.ranking.engine import evaluate_eligibility
from product_decision_engine.tco.calculator import (
    TcoBreakdown,
    calculate_simplified_tco,
    calculate_tco,
)


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    scenario: UsageScenario
    purchase_price_winner: Product | None
    simplified_tco_winner: Product | None
    decision_engine_winner: Product | None
    purchase_price_winner_tco: TcoBreakdown | None
    decision_engine_winner_tco: TcoBreakdown | None
    data_exclusions: tuple[tuple[str, str], ...]


def evaluate_scenario(catalog: Catalog, scenario: UsageScenario) -> ScenarioResult:
    calculated: list[tuple[Product, TcoBreakdown, TcoBreakdown]] = []
    data_exclusions: list[tuple[str, str]] = []
    for product in catalog.products:
        if product.status != "active" or not evaluate_eligibility(
            catalog, product, scenario
        ).eligible:
            continue
        try:
            full = calculate_tco(catalog, product.id, scenario)
            simplified = calculate_simplified_tco(catalog, product.id, scenario)
        except MissingCriticalData as error:
            data_exclusions.append((product.id, str(error)))
            continue
        calculated.append((product, full, simplified))

    if not calculated:
        return ScenarioResult(
            scenario=scenario,
            purchase_price_winner=None,
            simplified_tco_winner=None,
            decision_engine_winner=None,
            purchase_price_winner_tco=None,
            decision_engine_winner_tco=None,
            data_exclusions=tuple(data_exclusions),
        )

    purchase = min(calculated, key=lambda item: (item[1].purchase_cost_rub, item[0].id))
    simplified = min(calculated, key=lambda item: (item[2].total_cost_rub, item[0].id))
    decision = min(calculated, key=lambda item: (item[1].total_cost_rub, item[0].id))
    return ScenarioResult(
        scenario=scenario,
        purchase_price_winner=purchase[0],
        simplified_tco_winner=simplified[0],
        decision_engine_winner=decision[0],
        purchase_price_winner_tco=purchase[1],
        decision_engine_winner_tco=decision[1],
        data_exclusions=tuple(data_exclusions),
    )


def _money(value: int) -> str:
    return f"{value:,} RUB".replace(",", " ")


def build_report(catalog: Catalog, scenarios: tuple[UsageScenario, ...]) -> str:
    results = tuple(evaluate_scenario(catalog, scenario) for scenario in scenarios)
    complete = tuple(result for result in results if result.decision_engine_winner is not None)
    changed = tuple(
        result
        for result in complete
        if result.purchase_price_winner != result.decision_engine_winner
    )
    savings = tuple(
        result.purchase_price_winner_tco.total_cost_rub
        - result.decision_engine_winner_tco.total_cost_rub
        for result in changed
        if result.purchase_price_winner_tco and result.decision_engine_winner_tco
    )

    lines = [
        "# Phase 0 evaluation report",
        "",
        "> Generated deterministically from `data/golden`. Do not edit by hand.",
        "",
        "## Readiness",
        "",
        f"- Golden Dataset products: {len(catalog.products)} / 30–50 target",
        f"- Scenarios defined: {len(scenarios)} / 10–15 target",
        f"- Scenarios evaluated with a winner: {len(complete)} / {len(scenarios)}",
        "- Phase 0 verdict: `INCOMPLETE`" if len(catalog.products) < 30 else "- Phase 0 verdict: pending GO/REASSESS/NO-GO review",
        "",
        "## Aggregate metrics",
        "",
        f"- Winner changed vs purchase price: {len(changed)} / {len(complete)}",
        f"- Recommendation Change Rate: {len(changed) * 100 // len(complete)}%" if complete else "- Recommendation Change Rate: N/A",
        f"- Median savings when changed: {_money(int(median(savings)))}" if savings else "- Median savings when changed: N/A",
        f"- Maximum savings: {_money(max(savings))}" if savings else "- Maximum savings: N/A",
        "",
        "## Scenario results",
        "",
    ]

    for result in results:
        lines.extend([f"### {result.scenario.name}", ""])
        if result.decision_engine_winner is None:
            lines.append("No verified, complete candidate produced a recommendation.")
            if result.data_exclusions:
                lines.extend(
                    ["", "Data exclusions:"]
                    + [f"- `{product_id}`: {reason}" for product_id, reason in result.data_exclusions]
                )
            lines.append("")
            continue

        assert result.purchase_price_winner is not None
        assert result.simplified_tco_winner is not None
        assert result.purchase_price_winner_tco is not None
        assert result.decision_engine_winner_tco is not None
        savings_value = (
            result.purchase_price_winner_tco.total_cost_rub
            - result.decision_engine_winner_tco.total_cost_rub
        )
        lines.extend(
            [
                f"- Purchase-price winner: **{result.purchase_price_winner.manufacturer} {result.purchase_price_winner.model}** — purchase {_money(result.purchase_price_winner_tco.purchase_cost_rub)}, full TCO {_money(result.purchase_price_winner_tco.total_cost_rub)}",
                f"- Simplified-TCO winner: **{result.simplified_tco_winner.manufacturer} {result.simplified_tco_winner.model}**",
                f"- Decision Engine winner: **{result.decision_engine_winner.manufacturer} {result.decision_engine_winner.model}** — purchase {_money(result.decision_engine_winner_tco.purchase_cost_rub)}, consumables {_money(result.decision_engine_winner_tco.consumables_cost_rub)}, maintenance {_money(result.decision_engine_winner_tco.maintenance_cost_rub)}, total {_money(result.decision_engine_winner_tco.total_cost_rub)}",
                f"- Savings vs purchase-price baseline: **{_money(savings_value)}**",
                "- Evidence coverage for recommendation: **100% of required calculation facts**",
                "",
                "Purchased units for the recommendation:",
                "",
            ]
        )
        lines.extend(
            f"- `{component.channel}` / `{component.consumable_id}`: {component.units_purchased} × {_money(component.unit_price_rub)} (demand {component.demand_pages}, starter/installed capacity {component.starter_capacity_pages}, package yield {component.unit_capacity_pages})"
            for component in result.decision_engine_winner_tco.components
        )
        lines.append("")

    lines.extend(["## Data completeness", ""])
    if not catalog.products:
        lines.append("Golden Dataset has no products yet.")
    for product in catalog.products:
        audit = audit_product(catalog, product)
        lines.append(
            f"- `{product.id}`: {audit.verified_facts}/{audit.required_facts} verified facts ({audit.coverage_percent}%)"
        )
        lines.extend(f"  - Missing: `{item}`" for item in audit.missing)
    lines.append("")
    return "\n".join(lines)


def write_report(catalog: Catalog, scenarios: tuple[UsageScenario, ...], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_report(catalog, scenarios), encoding="utf-8")

