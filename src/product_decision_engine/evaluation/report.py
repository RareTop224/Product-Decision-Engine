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
    decision_engine_warnings: tuple[str, ...]
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
            decision_engine_warnings=(),
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
        decision_engine_warnings=evaluate_eligibility(
            catalog, decision[0], scenario
        ).warnings,
        data_exclusions=tuple(data_exclusions),
    )


def _money(value: int) -> str:
    return f"{value:,} ₽".replace(",", " ")


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
        "# Отчёт об оценке Фазы 0",
        "",
        "> Отчёт детерминированно сформирован из `data/golden`. Не редактировать вручную.",
        "",
        "## Готовность",
        "",
        f"- Моделей в Golden Dataset: {len(catalog.products)} / целевые 30–50",
        f"- Задано сценариев: {len(scenarios)} / целевые 10–15",
        f"- Сценариев с рассчитанным победителем: {len(complete)} / {len(scenarios)}",
        "- Статус Фазы 0: `INCOMPLETE` — данных пока недостаточно для итогового решения"
        if len(catalog.products) < 30
        else "- Статус Фазы 0: требуется итоговая оценка `GO` / `REASSESS` / `NO-GO`",
        "",
        "## Сводные метрики",
        "",
        f"- Победитель изменился относительно выбора по цене покупки: {len(changed)} / {len(complete)}",
        f"- Доля изменившихся рекомендаций: {len(changed) * 100 // len(complete)}%"
        if complete
        else "- Доля изменившихся рекомендаций: нет данных",
        f"- Медианная экономия при смене победителя: {_money(int(median(savings)))}"
        if savings
        else "- Медианная экономия при смене победителя: нет данных",
        f"- Максимальная экономия: {_money(max(savings))}"
        if savings
        else "- Максимальная экономия: нет данных",
        "",
        "## Результаты по сценариям",
        "",
    ]

    for result in results:
        lines.extend([f"### {result.scenario.name}", ""])
        if result.decision_engine_winner is None:
            lines.append(
                "Нет подходящей модели с полными и подтверждёнными данными для рекомендации."
            )
            if result.data_exclusions:
                lines.extend(
                    ["", "Исключения из-за качества данных:"]
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
                f"- Победитель по цене покупки: **{result.purchase_price_winner.manufacturer} {result.purchase_price_winner.model}** — покупка {_money(result.purchase_price_winner_tco.purchase_cost_rub)}, полный TCO {_money(result.purchase_price_winner_tco.total_cost_rub)}",
                f"- Победитель по упрощённому TCO: **{result.simplified_tco_winner.manufacturer} {result.simplified_tco_winner.model}**",
                f"- Победитель Decision Engine: **{result.decision_engine_winner.manufacturer} {result.decision_engine_winner.model}** — покупка {_money(result.decision_engine_winner_tco.purchase_cost_rub)}, расходники {_money(result.decision_engine_winner_tco.consumables_cost_rub)}, обслуживание {_money(result.decision_engine_winner_tco.maintenance_cost_rub)}, итого {_money(result.decision_engine_winner_tco.total_cost_rub)}",
                f"- Экономия относительно выбора по цене покупки: **{_money(savings_value)}**",
                "- Покрытие источниками: **100% обязательных для расчёта фактов**",
            ]
        )
        if result.decision_engine_warnings:
            lines.extend(
                f"- Предупреждение: {warning}"
                for warning in result.decision_engine_warnings
            )
        lines.extend(["", "Покупки расходников и компонентов для рекомендации:", ""])
        lines.extend(
            f"- `{component.channel}` / `{component.consumable_id}`: {component.units_purchased} × {_money(component.unit_price_rub)} (потребность {component.demand_pages} стр., стартовый/установленный ресурс {component.starter_capacity_pages} стр., ресурс упаковки {component.unit_capacity_pages} стр.)"
            for component in result.decision_engine_winner_tco.components
        )
        lines.append("")

    lines.extend(["## Полнота данных", ""])
    if not catalog.products:
        lines.append("В Golden Dataset пока нет моделей.")
    for product in catalog.products:
        audit = audit_product(catalog, product)
        lines.append(
            f"- `{product.id}`: {audit.verified_facts}/{audit.required_facts} подтверждённых фактов ({audit.coverage_percent}%)"
        )
        lines.extend(f"  - Не хватает: `{item}`" for item in audit.missing)
    lines.append("")
    return "\n".join(lines)


def write_report(catalog: Catalog, scenarios: tuple[UsageScenario, ...], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_report(catalog, scenarios), encoding="utf-8")
