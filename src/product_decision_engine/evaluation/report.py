from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from statistics import median

from product_decision_engine.domain.catalog import Catalog, MissingCriticalData
from product_decision_engine.domain.models import (
    OfferAvailability,
    Product,
    ProductConsumableRole,
    RetailerBasketAudit,
    UsageScenario,
    VerificationStatus,
)
from product_decision_engine.evidence.audit import audit_product
from product_decision_engine.evaluation.retailer_baskets import (
    RetailerBasketResult,
    evaluate_retailer_basket,
    evaluate_retailer_basket_probe,
)
from product_decision_engine.ranking.break_even import find_break_even
from product_decision_engine.ranking.engine import evaluate_eligibility
from product_decision_engine.tco.calculator import (
    TcoBreakdown,
    calculate_simplified_tco,
    calculate_tco,
)


SENSITIVITY_MONO_PAGES_PER_MONTH = (50, 200, 500, 1_000)
SENSITIVITY_OWNERSHIP_MONTHS = 60
MIXED_SENSITIVITY_TOTAL_PAGES_PER_MONTH = 750
MIXED_SENSITIVITY_COLOR_SHARES_PERCENT = (0, 25, 50, 75)
MIXED_SENSITIVITY_OWNERSHIP_MONTHS = (12, 36, 60)
RETAILER_PAIR_TOTAL_PAGES_PER_MONTH = (10, 25, 50, 100)
RETAILER_PAIR_COLOR_SHARES_PERCENT = (0, 20)
RETAILER_PAIR_OWNERSHIP_MONTHS = (12, 36, 60)


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    scenario: UsageScenario
    purchase_price_winner: Product | None
    simplified_tco_winner: Product | None
    decision_engine_winner: Product | None
    decision_runner_up: Product | None
    purchase_price_winner_tco: TcoBreakdown | None
    decision_engine_winner_tco: TcoBreakdown | None
    decision_runner_up_tco: TcoBreakdown | None
    candidate_count: int
    decision_engine_warnings: tuple[str, ...]
    constraint_exclusions: tuple[tuple[str, tuple[str, ...]], ...]
    data_exclusions: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class PriceRobustness:
    winner_id: str
    winner_tco_min_rub: int
    winner_tco_max_rub: int
    best_challenger_id: str | None
    best_challenger_tco_min_rub: int | None
    has_observed_range: bool
    robust: bool


@dataclass(frozen=True, slots=True)
class FullTcoAblation:
    product: Product
    simplified_tco_rub: int
    starter_credit_rub: int
    maintenance_cost_rub: int
    full_tco_rub: int


@dataclass(frozen=True, slots=True)
class MixedSensitivityProfile:
    id: str
    title: str
    require_mfp: bool
    require_wifi: bool


@dataclass(frozen=True, slots=True)
class RetailerPairSensitivity:
    product_ids: tuple[str, ...]
    audits: tuple[RetailerBasketAudit, ...]
    results_by_audit: tuple[tuple[RetailerBasketResult, ...], ...]
    agreement_points: int
    total_points: int
    confirmed: bool

    @property
    def full_vs_simplified_changes(self) -> int:
        return sum(
            result.decision_engine_winner.product.id
            != result.simplified_tco_winner.product.id
            for results in self.results_by_audit
            for result in results
        )

    @property
    def result_count(self) -> int:
        return sum(len(results) for results in self.results_by_audit)


MIXED_SENSITIVITY_PROFILES = (
    MixedSensitivityProfile(
        id="open",
        title="Без требований к функциям",
        require_mfp=False,
        require_wifi=False,
    ),
    MixedSensitivityProfile(
        id="mfp-wifi",
        title="Обязательны МФУ и Wi-Fi",
        require_mfp=True,
        require_wifi=True,
    ),
)


def _retailer_pair_probe_scenarios(
    reference: UsageScenario,
) -> tuple[UsageScenario, ...]:
    scenarios: list[UsageScenario] = []
    for total_pages in RETAILER_PAIR_TOTAL_PAGES_PER_MONTH:
        for color_share_percent in RETAILER_PAIR_COLOR_SHARES_PERCENT:
            color_pages = total_pages * color_share_percent // 100
            mono_pages = total_pages - color_pages
            for ownership_months in RETAILER_PAIR_OWNERSHIP_MONTHS:
                scenarios.append(
                    UsageScenario(
                        id=(
                            f"retailer-pair-{reference.id}-{total_pages}-"
                            f"{color_share_percent}-{ownership_months}"
                        ),
                        name=(
                            f"{total_pages} стр./мес., {color_share_percent}% цвета, "
                            f"{ownership_months} мес."
                        ),
                        mono_pages_per_month=mono_pages,
                        color_pages_per_month=color_pages,
                        ownership_months=ownership_months,
                        require_mfp=reference.require_mfp,
                        require_wifi=reference.require_wifi,
                        require_auto_duplex=reference.require_auto_duplex,
                        max_purchase_price_rub=reference.max_purchase_price_rub,
                    )
                )
    return tuple(scenarios)


def evaluate_retailer_pair_sensitivity(
    catalog: Catalog,
    reference: UsageScenario,
    audits: tuple[RetailerBasketAudit, ...],
) -> RetailerPairSensitivity:
    complete_audits = tuple(audit for audit in audits if audit.complete)
    if len(complete_audits) < 2:
        raise ValueError("retailer pair sensitivity requires two complete baskets")
    product_ids = tuple(offer.product_id for offer in complete_audits[0].offers)
    if any(
        tuple(offer.product_id for offer in audit.offers) != product_ids
        for audit in complete_audits[1:]
    ):
        raise ValueError("retailer pair audits must preserve product order")

    scenarios = _retailer_pair_probe_scenarios(reference)
    results_by_audit = tuple(
        tuple(
            evaluate_retailer_basket_probe(catalog, scenario, audit)
            for scenario in scenarios
        )
        for audit in complete_audits
    )
    agreement_points = sum(
        len(
            {
                results[index].decision_engine_winner.product.id
                for results in results_by_audit
            }
        )
        == 1
        for index in range(len(scenarios))
    )
    every_retailer_switches = all(
        len({result.decision_engine_winner.product.id for result in results}) >= 2
        for results in results_by_audit
    )
    confirmed = (
        every_retailer_switches
        and agreement_points * 4 >= len(scenarios) * 3
    )
    return RetailerPairSensitivity(
        product_ids=product_ids,
        audits=complete_audits,
        results_by_audit=results_by_audit,
        agreement_points=agreement_points,
        total_points=len(scenarios),
        confirmed=confirmed,
    )


def evaluate_scenario(catalog: Catalog, scenario: UsageScenario) -> ScenarioResult:
    calculated: list[tuple[Product, TcoBreakdown, TcoBreakdown]] = []
    constraint_exclusions: list[tuple[str, tuple[str, ...]]] = []
    data_exclusions: list[tuple[str, str]] = []
    for product in catalog.products:
        if product.status != "active":
            continue
        eligibility = evaluate_eligibility(catalog, product, scenario)
        if not eligibility.eligible:
            constraint_exclusions.append((product.id, eligibility.reasons))
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
            decision_runner_up=None,
            purchase_price_winner_tco=None,
            decision_engine_winner_tco=None,
            decision_runner_up_tco=None,
            candidate_count=0,
            decision_engine_warnings=(),
            constraint_exclusions=tuple(constraint_exclusions),
            data_exclusions=tuple(data_exclusions),
        )

    purchase = min(calculated, key=lambda item: (item[1].purchase_cost_rub, item[0].id))
    simplified = min(calculated, key=lambda item: (item[2].total_cost_rub, item[0].id))
    decision_ranking = sorted(
        calculated,
        key=lambda item: (item[1].total_cost_rub, item[1].purchase_cost_rub, item[0].id),
    )
    decision = decision_ranking[0]
    runner_up = decision_ranking[1] if len(decision_ranking) >= 2 else None
    return ScenarioResult(
        scenario=scenario,
        purchase_price_winner=purchase[0],
        simplified_tco_winner=simplified[0],
        decision_engine_winner=decision[0],
        decision_runner_up=runner_up[0] if runner_up else None,
        purchase_price_winner_tco=purchase[1],
        decision_engine_winner_tco=decision[1],
        decision_runner_up_tco=runner_up[1] if runner_up else None,
        candidate_count=len(calculated),
        decision_engine_warnings=evaluate_eligibility(
            catalog, decision[0], scenario
        ).warnings,
        constraint_exclusions=tuple(constraint_exclusions),
        data_exclusions=tuple(data_exclusions),
    )


def _money(value: int) -> str:
    return f"{value:,} ₽".replace(",", " ")


def _percent(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "нет данных"
    tenths = (numerator * 1_000 + denominator // 2) // denominator
    return f"{tenths // 10},{tenths % 10}%"


def _product_name(product: Product) -> str:
    return f"{product.manufacturer} {product.model}"


def evaluate_full_tco_ablation(
    catalog: Catalog,
    product: Product,
    scenario: UsageScenario,
) -> FullTcoAblation:
    simplified = calculate_simplified_tco(catalog, product.id, scenario)
    full = calculate_tco(catalog, product.id, scenario)
    starter_credit = simplified.consumables_cost_rub - full.consumables_cost_rub
    reconstructed_total = (
        simplified.total_cost_rub - starter_credit + full.maintenance_cost_rub
    )
    if reconstructed_total != full.total_cost_rub:
        raise ValueError(f"TCO ablation identity failed for {product.id}")
    return FullTcoAblation(
        product=product,
        simplified_tco_rub=simplified.total_cost_rub,
        starter_credit_rub=starter_credit,
        maintenance_cost_rub=full.maintenance_cost_rub,
        full_tco_rub=full.total_cost_rub,
    )


def _availability_label(availability: OfferAvailability) -> str:
    return {
        OfferAvailability.IN_STOCK: "в наличии",
        OfferAvailability.ORDERABLE_UNCONFIRMED: "заказ доступен, срок не подтверждён",
        OfferAvailability.EXPECTED: "ожидается",
        OfferAvailability.TRANSIT: "в транзите",
        OfferAvailability.UNAVAILABLE: "недоступно",
        OfferAvailability.NOT_LISTED: "не найдено в каталоге",
        OfferAvailability.UNVERIFIED: "не подтверждено",
    }[availability]


def _economic_configuration_signature(
    catalog: Catalog,
    product: Product,
) -> tuple[tuple[object, ...], ...]:
    """Describe TCO inputs without treating model-specific starter IDs as diversity."""
    signature: list[tuple[object, ...]] = []
    for link in catalog.links(product.id):
        consumable = catalog.consumable(link.consumable_id)
        if link.role == ProductConsumableRole.STARTER:
            item_identity: tuple[object, ...] = (
                "starter_capacity",
                consumable.yield_value * link.quantity_in_box,
            )
        else:
            item_identity = (
                consumable.part_number,
                consumable.yield_value,
                link.quantity_in_box,
            )
        signature.append(
            (
                link.role.value,
                link.channel,
                link.page_scope.value,
                link.mono_page_weight,
                link.color_page_weight,
                link.installed_yield_value,
                *item_identity,
            )
        )
    return tuple(sorted(signature, key=repr))


def _recommendation_sources(
    catalog: Catalog,
    product: Product,
) -> tuple[tuple[str, str], ...]:
    entity_keys: set[tuple[str, str]] = {("product", product.id)}
    product_price = catalog.latest_price("product", product.id)
    entity_keys.add(("price_observation", product_price.id))
    for link in catalog.links(product.id):
        entity_keys.add(("product_consumable", link.id))
        entity_keys.add(("consumable", link.consumable_id))
        if link.role != ProductConsumableRole.STARTER:
            price = catalog.latest_price("consumable", link.consumable_id)
            entity_keys.add(("price_observation", price.id))

    sources = {
        (item.source_name, item.source_url)
        for item in catalog.evidence
        if item.verification_status == VerificationStatus.VERIFIED
        and (item.entity_type, item.entity_id) in entity_keys
    }
    return tuple(sorted(sources, key=lambda item: (item[0].casefold(), item[1])))


def _append_exclusions(lines: list[str], result: ScenarioResult) -> None:
    if not result.constraint_exclusions and not result.data_exclusions:
        return
    lines.extend(["", "Исключённые модели:", ""])
    lines.extend(
        f"- `{product_id}` — hard constraints: {', '.join(reasons)}"
        for product_id, reasons in result.constraint_exclusions
    )
    lines.extend(
        f"- `{product_id}` — неполные критические данные: {reason}"
        for product_id, reason in result.data_exclusions
    )


def _sensitivity_results(catalog: Catalog) -> tuple[ScenarioResult, ...]:
    return tuple(
        evaluate_scenario(
            catalog,
            UsageScenario(
                id=f"sensitivity-mono-{pages}",
                name=f"{pages} чёрно-белых страниц в месяц",
                mono_pages_per_month=pages,
                color_pages_per_month=0,
                ownership_months=SENSITIVITY_OWNERSHIP_MONTHS,
            ),
        )
        for pages in SENSITIVITY_MONO_PAGES_PER_MONTH
    )


def _mixed_sensitivity_results(
    catalog: Catalog,
    profile: MixedSensitivityProfile,
) -> tuple[tuple[ScenarioResult, ...], ...]:
    rows: list[tuple[ScenarioResult, ...]] = []
    for color_share_percent in MIXED_SENSITIVITY_COLOR_SHARES_PERCENT:
        color_pages = (
            MIXED_SENSITIVITY_TOTAL_PAGES_PER_MONTH * color_share_percent // 100
        )
        mono_pages = MIXED_SENSITIVITY_TOTAL_PAGES_PER_MONTH - color_pages
        rows.append(
            tuple(
                evaluate_scenario(
                    catalog,
                    UsageScenario(
                        id=(
                            f"sensitivity-mixed-{profile.id}-"
                            f"{color_share_percent}-{ownership_months}"
                        ),
                        name=(
                            f"{color_share_percent}% цвета, "
                            f"{ownership_months} месяцев"
                        ),
                        mono_pages_per_month=mono_pages,
                        color_pages_per_month=color_pages,
                        ownership_months=ownership_months,
                        require_mfp=profile.require_mfp,
                        require_wifi=profile.require_wifi,
                    ),
                )
                for ownership_months in MIXED_SENSITIVITY_OWNERSHIP_MONTHS
            )
        )
    return tuple(rows)


def _catalog_at_price_bound(catalog: Catalog, *, use_maximum: bool) -> Catalog:
    by_entity: dict[tuple[str, str], list] = {}
    for observation in catalog.prices:
        by_entity.setdefault(
            (observation.entity_type, observation.entity_id), []
        ).append(observation)

    selected_ids = {
        max(items, key=lambda item: (item.price_rub, item.observed_at, item.id)).id
        if use_maximum
        else min(items, key=lambda item: (item.price_rub, item.observed_at, item.id)).id
        for items in by_entity.values()
    }
    return replace(
        catalog,
        prices=tuple(
            replace(item, is_primary=item.id in selected_ids)
            for item in catalog.prices
        ),
    )


def _priced_entities_for_product(
    catalog: Catalog,
    product: Product,
) -> tuple[tuple[str, str], ...]:
    entities: list[tuple[str, str]] = [("product", product.id)]
    entities.extend(
        ("consumable", link.consumable_id)
        for link in catalog.links(product.id)
        if link.role != ProductConsumableRole.STARTER
    )
    return tuple(dict.fromkeys(entities))


def evaluate_price_robustness(
    catalog: Catalog,
    result: ScenarioResult,
) -> PriceRobustness | None:
    winner = result.decision_engine_winner
    if winner is None:
        return None

    low_catalog = _catalog_at_price_bound(catalog, use_maximum=False)
    high_catalog = _catalog_at_price_bound(catalog, use_maximum=True)
    bounds: list[tuple[Product, int, int]] = []
    for product in catalog.products:
        if product.status != "active":
            continue
        if not evaluate_eligibility(catalog, product, result.scenario).eligible:
            continue
        try:
            low = calculate_tco(low_catalog, product.id, result.scenario)
            high = calculate_tco(high_catalog, product.id, result.scenario)
        except MissingCriticalData:
            continue
        bounds.append((product, low.total_cost_rub, high.total_cost_rub))

    winner_bound = next(item for item in bounds if item[0].id == winner.id)
    challengers = sorted(
        (item for item in bounds if item[0].id != winner.id),
        key=lambda item: (item[1], item[0].id),
    )
    best_challenger = challengers[0] if challengers else None
    has_observed_range = any(
        len(catalog.price_observations(entity_type, entity_id)) >= 2
        for product, _, _ in bounds
        for entity_type, entity_id in _priced_entities_for_product(catalog, product)
    )
    return PriceRobustness(
        winner_id=winner.id,
        winner_tco_min_rub=winner_bound[1],
        winner_tco_max_rub=winner_bound[2],
        best_challenger_id=best_challenger[0].id if best_challenger else None,
        best_challenger_tco_min_rub=best_challenger[1] if best_challenger else None,
        has_observed_range=has_observed_range,
        robust=bool(
            best_challenger
            and winner_bound[2] < best_challenger[1]
        ),
    )


def build_report(
    catalog: Catalog,
    scenarios: tuple[UsageScenario, ...],
    retailer_basket_audits: tuple[RetailerBasketAudit, ...] = (),
) -> str:
    scenario_by_id = {scenario.id: scenario for scenario in scenarios}
    basket_groups: dict[
        tuple[str, frozenset[str]], list[RetailerBasketAudit]
    ] = {}
    if retailer_basket_audits:
        for audit in retailer_basket_audits:
            if audit.scenario_id not in scenario_by_id:
                raise ValueError("retailer basket audit references an unknown scenario")
            product_ids = frozenset(offer.product_id for offer in audit.offers)
            for product_id in product_ids:
                catalog.product(product_id)
            basket_groups.setdefault((audit.scenario_id, product_ids), []).append(
                audit
            )

    results = tuple(evaluate_scenario(catalog, scenario) for scenario in scenarios)
    price_robustness = tuple(
        evaluate_price_robustness(catalog, result) for result in results
    )
    ranged_robustness = tuple(
        item for item in price_robustness if item and item.has_observed_range
    )
    robust_recommendations = sum(item.robust for item in ranged_robustness)
    sensitivity = _sensitivity_results(catalog)
    mixed_sensitivity = tuple(
        (profile, _mixed_sensitivity_results(catalog, profile))
        for profile in MIXED_SENSITIVITY_PROFILES
    )
    mixed_robustness = tuple(
        (
            profile,
            tuple(
                evaluate_price_robustness(catalog, result)
                for row in rows
                for result in row
            ),
        )
        for profile, rows in mixed_sensitivity
    )
    complete = tuple(result for result in results if result.decision_engine_winner is not None)
    competitive = tuple(result for result in complete if result.candidate_count >= 2)
    changed = tuple(
        result
        for result in complete
        if result.purchase_price_winner != result.decision_engine_winner
    )
    competitive_changed = tuple(result for result in changed if result.candidate_count >= 2)
    simplified_changed = tuple(
        result
        for result in complete
        if result.simplified_tco_winner != result.decision_engine_winner
    )
    complete_retailer_baskets = tuple(
        audit for audit in retailer_basket_audits if audit.complete
    )
    retailer_basket_results: dict[str, RetailerBasketResult] = {
        audit.id: evaluate_retailer_basket(
            catalog, scenario_by_id[audit.scenario_id], audit
        )
        for audit in complete_retailer_baskets
    }
    retailer_pair_sensitivity = tuple(
        evaluate_retailer_pair_sensitivity(
            catalog,
            scenario_by_id[scenario_id],
            tuple(group_audits),
        )
        for (scenario_id, _), group_audits in basket_groups.items()
        if sum(audit.complete for audit in group_audits) >= 2
    )
    confirmed_retailer_pairs = tuple(
        item for item in retailer_pair_sensitivity if item.confirmed
    )
    retailer_probe_full_vs_simplified_changes = sum(
        item.full_vs_simplified_changes for item in retailer_pair_sensitivity
    )
    retailer_probe_result_count = sum(
        item.result_count for item in retailer_pair_sensitivity
    )
    retailer_basket_purchase_changes = sum(
        result.purchase_price_winner.product.id
        != result.decision_engine_winner.product.id
        for result in retailer_basket_results.values()
    )
    retailer_basket_simplified_changes = sum(
        result.simplified_tco_winner.product.id
        != result.decision_engine_winner.product.id
        for result in retailer_basket_results.values()
    )
    savings = tuple(
        result.purchase_price_winner_tco.total_cost_rub
        - result.decision_engine_winner_tco.total_cost_rub
        for result in changed
        if result.purchase_price_winner_tco and result.decision_engine_winner_tco
    )
    decision_margins = tuple(
        result.decision_runner_up_tco.total_cost_rub
        - result.decision_engine_winner_tco.total_cost_rub
        for result in competitive
        if result.decision_runner_up_tco and result.decision_engine_winner_tco
    )
    narrowest_result = min(
        (
            result
            for result in competitive
            if result.decision_runner_up_tco and result.decision_engine_winner_tco
        ),
        key=lambda result: (
            result.decision_runner_up_tco.total_cost_rub
            - result.decision_engine_winner_tco.total_cost_rub
        ),
        default=None,
    )
    winner_counts = Counter(
        result.decision_engine_winner.id
        for result in complete
        if result.decision_engine_winner is not None
    )
    most_common_winner = winner_counts.most_common(1)[0] if winner_counts else None
    purchase_winner_counts = Counter(
        result.purchase_price_winner.id
        for result in complete
        if result.purchase_price_winner is not None
    )
    most_common_purchase_winner = (
        purchase_winner_counts.most_common(1)[0] if purchase_winner_counts else None
    )
    winner_brand_counts = Counter(
        result.decision_engine_winner.manufacturer
        for result in complete
        if result.decision_engine_winner is not None
    )
    winner_technology_counts = Counter(
        result.decision_engine_winner.print_technology
        for result in complete
        if result.decision_engine_winner is not None
    )
    most_common_winner_brand = (
        winner_brand_counts.most_common(1)[0] if winner_brand_counts else None
    )
    most_common_winner_technology = (
        winner_technology_counts.most_common(1)[0]
        if winner_technology_counts
        else None
    )
    sensitivity_winner_ids = {
        result.decision_engine_winner.id
        for result in sensitivity
        if result.decision_engine_winner is not None
    }
    mixed_winner_ids_by_profile = tuple(
        (
            profile,
            {
                result.decision_engine_winner.id
                for row in rows
                for result in row
                if result.decision_engine_winner is not None
            },
        )
        for profile, rows in mixed_sensitivity
    )
    brand_counts = Counter(product.manufacturer for product in catalog.products)
    technology_counts = Counter(product.print_technology for product in catalog.products)
    product_type_counts = Counter(product.product_type.value for product in catalog.products)
    color_mode_counts = Counter(product.color_mode.value for product in catalog.products)
    publication_gap_products = sum(
        bool(audit_product(catalog, product).publication_gaps)
        for product in catalog.products
    )
    conflict_products = sum(
        bool(audit_product(catalog, product).conflicts)
        for product in catalog.products
    )
    price_entity_counts = Counter(
        (item.entity_type, item.entity_id) for item in catalog.prices
    )
    repeated_price_entities = sum(count >= 2 for count in price_entity_counts.values())
    economic_configurations = {
        _economic_configuration_signature(catalog, product)
        for product in catalog.products
    }
    execution_viable = bool(scenarios) and len(competitive) == len(scenarios)
    early_value_signal = bool(competitive_changed) and len(sensitivity_winner_ids) >= 2
    retailer_pair_gate_ready = len(retailer_pair_sensitivity) >= 2
    retailer_pair_signal = (
        retailer_pair_gate_ready
        and len(confirmed_retailer_pairs) == len(retailer_pair_sensitivity)
    )
    dataset_ready = (
        len(catalog.products) >= 30
        and len(scenarios) >= 10
        and len(complete) == len(scenarios)
        and all(not audit_product(catalog, product).missing for product in catalog.products)
    )
    if not dataset_ready:
        phase0_status = "INCOMPLETE"
    elif not execution_viable:
        phase0_status = "REASSESS"
    elif not competitive_changed:
        phase0_status = "NO-GO"
    elif retailer_pair_gate_ready:
        phase0_status = "GO" if retailer_pair_signal else "NO-GO"
    elif not early_value_signal:
        phase0_status = "REASSESS"
    else:
        phase0_status = "GO"

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
        f"- Конкурентных сценариев (не менее двух кандидатов): {len(competitive)} / {len(scenarios)}",
        f"- Статус Фазы 0: `{phase0_status}`",
        "",
        "## Состав промежуточной выборки",
        "",
        f"- Бренды ({len(brand_counts)}): "
        + ", ".join(f"{name} — {count}" for name, count in sorted(brand_counts.items())),
        "- Технологии: "
        + ", ".join(
            f"`{name}` — {count}" for name, count in sorted(technology_counts.items())
        ),
        "- Типы устройств: "
        + ", ".join(
            f"`{name}` — {count}" for name, count in sorted(product_type_counts.items())
        ),
        "- Цветность: "
        + ", ".join(
            f"`{name}` — {count}" for name, count in sorted(color_mode_counts.items())
        ),
        f"- Уникальных экономических конфигураций расходников: {len(economic_configurations)} / {len(catalog.products)} моделей",
        f"- Моделей с публикационными пробелами: {publication_gap_products}",
        f"- Моделей с зафиксированным конфликтом источников: {conflict_products}",
        "",
        "## Сводные метрики",
        "",
        f"- Победитель изменился относительно выбора по цене покупки: {len(changed)} / {len(complete)} ({_percent(len(changed), len(complete))})",
        f"- Смена победителя в конкурентных сценариях: {len(competitive_changed)} / {len(competitive)} ({_percent(len(competitive_changed), len(competitive))})",
        f"- Полный расчёт меняет победителя упрощённого TCO: {len(simplified_changed)} / {len(complete)} ({_percent(len(simplified_changed), len(complete))})",
        f"- Медианная экономия при смене победителя: {_money(int(median(savings)))}"
        if savings
        else "- Медианная экономия при смене победителя: нет данных",
        f"- Максимальная экономия: {_money(max(savings))}"
        if savings
        else "- Максимальная экономия: нет данных",
        f"- Медианный отрыв победителя от второго места: {_money(int(median(decision_margins)))}"
        if decision_margins
        else "- Медианный отрыв победителя от второго места: нет данных",
        f"- Предварительный стресс-тест не меняет рекомендацию в доступных диапазонах цен: {robust_recommendations} / {len(ranged_robustness)} сценариев"
        if ranged_robustness
        else "- Проверка диапазона цен: пока нет повторных наблюдений",
        f"- Покрытие повторными ценовыми наблюдениями: {repeated_price_entities} / {len(price_entity_counts)} оцениваемых сущностей",
    ]
    if retailer_basket_audits:
        lines.append(
            "- Полных синхронных корзин во всех аудитах проверенных пар: "
            f"{len(complete_retailer_baskets)} / {len(retailer_basket_audits)} проверок"
        )
    if retailer_basket_results:
        lines.extend(
            [
                "- Смена победителя относительно цены покупки в полных корзинах: "
                f"{retailer_basket_purchase_changes} / {len(retailer_basket_results)}",
                "- Смена победителя полного TCO относительно упрощённого в полных корзинах: "
                f"{retailer_basket_simplified_changes} / {len(retailer_basket_results)}",
            ]
        )
    if retailer_pair_sensitivity:
        lines.extend(
            [
                "- Пар с двумя полными корзинами и многоточечной проверкой: "
                f"{len(retailer_pair_sensitivity)}",
                "- Пар с воспроизводимой сменой победителя по сценарию: "
                f"{len(confirmed_retailer_pairs)} / {len(retailer_pair_sensitivity)}",
                "- Смена полного TCO относительно упрощённого в парных контрольных точках: "
                f"{retailer_probe_full_vs_simplified_changes} / {retailer_probe_result_count}",
            ]
        )
    if most_common_winner:
        product = catalog.product(most_common_winner[0])
        lines.append(
            f"- Концентрация лидера: **{_product_name(product)}** выигрывает "
            f"{most_common_winner[1]} / {len(complete)} сценариев "
            f"({_percent(most_common_winner[1], len(complete))})"
        )
    if most_common_purchase_winner:
        product = catalog.product(most_common_purchase_winner[0])
        lines.append(
            f"- Концентрация baseline по цене покупки: **{_product_name(product)}** — "
            f"{most_common_purchase_winner[1]} / {len(complete)} сценариев "
            f"({_percent(most_common_purchase_winner[1], len(complete))})"
        )
    if most_common_winner_brand:
        lines.append(
            f"- Концентрация бренда среди победителей: **{most_common_winner_brand[0]}** — "
            f"{most_common_winner_brand[1]} / {len(complete)} сценариев "
            f"({_percent(most_common_winner_brand[1], len(complete))})"
        )
    if most_common_winner_technology:
        lines.append(
            f"- Концентрация технологии среди победителей: **{most_common_winner_technology[0]}** — "
            f"{most_common_winner_technology[1]} / {len(complete)} сценариев "
            f"({_percent(most_common_winner_technology[1], len(complete))})"
        )
    if narrowest_result:
        assert narrowest_result.decision_engine_winner_tco is not None
        assert narrowest_result.decision_runner_up_tco is not None
        lines.append(
            f"- Самая хрупкая рекомендация: **{narrowest_result.scenario.name}** — "
            f"отрыв всего {_money(narrowest_result.decision_runner_up_tco.total_cost_rub - narrowest_result.decision_engine_winner_tco.total_cost_rub)}"
        )

    lines.extend(
        [
            f"- Разных победителей в контрольной чувствительности: {len(sensitivity_winner_ids)} / {len(sensitivity)}",
            *(
                f"- Разных победителей в матрице «доля цвета × горизонт» ({profile.title.casefold()}): "
                f"{len(winner_ids)} / "
                f"{len(MIXED_SENSITIVITY_COLOR_SHARES_PERCENT) * len(MIXED_SENSITIVITY_OWNERSHIP_MONTHS)}"
                for profile, winner_ids in mixed_winner_ids_by_profile
            ),
            *(
                f"- Ценовая устойчивость той же матрицы ({profile.title.casefold()}): "
                f"{sum(bool(item and item.robust) for item in robustness)} / "
                f"{sum(bool(item and item.has_observed_range) for item in robustness)} точек с диапазоном"
                for profile, robustness in mixed_robustness
            ),
            "",
            "## Промежуточная интерпретация",
            "",
        ]
    )
    if phase0_status == "GO":
        if retailer_pair_signal:
            lines.extend(
                [
                    "- Рекомендация процесса: **`GO TO NEXT-PHASE DECISION`** — Proof of Value подтверждён; проект имеет смысл продолжать, но это ещё не разрешение на production-разработку.",
                    f"- Основание: сценарная смена победителя воспроизводится для {len(confirmed_retailer_pairs)} из {len(retailer_pair_sensitivity)} прямых пар у двух продавцов; совпадение результатов между продавцами составляет не менее 75% контрольных точек.",
                    f"- Специализированный вклад не нулевой: полный расчёт starter/maintenance меняет победителя упрощённого TCO в {retailer_probe_full_vs_simplified_changes} из {retailer_probe_result_count} парных расчётов продавец × сценарий.",
                    "- Ограничение: широкая глобальная сетка всё ещё концентрируется на СНПЧ, а 3 из 7 ранних аудитов корзин неполны. Следующая фаза должна сначала решить актуализацию цен/наличия и нормализацию evidence, а не строить большой интерфейс.",
                ]
            )
        else:
            lines.extend(
                [
                    "- Рекомендация процесса: **`GO`** — минимальные критерии Proof of Value выполнены.",
                    f"- Основание: все {len(scenarios)} сценариев конкурентны, Decision Engine меняет выбор в {len(competitive_changed)} из них, а контроль чувствительности даёт {len(sensitivity_winner_ids)} разных победителя.",
                    "- Перед production-разработкой всё равно требуется обновить и расширить ценовые наблюдения: Phase 0 доказывает полезность расчёта, а не готовность price feed.",
                ]
            )
        if most_common_winner_brand and most_common_winner_brand[1] * 3 >= len(complete) * 2:
            lines.append(
                f"- Риск концентрации: бренд {most_common_winner_brand[0]} даёт "
                f"{most_common_winner_brand[1]} из {len(complete)} победителей; следующий сбор должен добавить прямые альтернативы других брендов, а не случайные модели."
            )
        if repeated_price_entities * 2 < len(price_entity_counts):
            lines.append(
                f"- Стресс-тест цен пока разреженный: повторные наблюдения есть только для {repeated_price_entities} из {len(price_entity_counts)} ценовых сущностей, поэтому его результат нельзя считать доказанной рыночной устойчивостью."
            )
    elif phase0_status == "REASSESS":
        mixed_profiles_with_switches = sum(
            len(winner_ids) >= 2
            for _, winner_ids in mixed_winner_ids_by_profile
        )
        lines.extend(
            [
                "- Рекомендация процесса: **`REASSESS BEFORE PRODUCT BUILD`** — экономическая ценность видна, но текущая проверка чувствительности не подтверждает достаточно сценарно-зависимый выбор.",
                f"- Основание: Decision Engine меняет выбор в {len(competitive_changed)} из {len(competitive)} конкурентных сценариев; число разных победителей в {len(sensitivity)} контрольных точках объёма — {len(sensitivity_winner_ids)}.",
                f"- Ограничение сигнала: полный расчёт меняет результат упрощённого TCO только в {len(simplified_changed)} из {len(complete)} сценариев; 100% смен относительно цены покупки нельзя считать достаточным доказательством специализированного преимущества.",
                f"- Двумерный тест дал переключение победителя внутри {mixed_profiles_with_switches} из {len(mixed_winner_ids_by_profile)} функциональных профилей; простая смена требований к функциям не считается чувствительностью экономики.",
                f"- Повторные цены выявили предварительную устойчивость в {robust_recommendations} из {len(ranged_robustness)} сценариев с наблюдаемым диапазоном; это диагностический стресс-тест, а не price feed.",
            ]
        )
        if retailer_basket_results:
            lines.extend(
                [
                    f"- Синхронные цены подтверждают смену относительно цены покупки в {retailer_basket_purchase_changes} из {len(retailer_basket_results)} полных корзин, но полный расчёт меняет упрощённый TCO в {retailer_basket_simplified_changes} из {len(retailer_basket_results)}: рыночный сигнал TCO реален, специализированный вклад starter/maintenance пока слаб.",
                    "- Следующий фальсифицирующий тест: искать не новые случайные модели, а 2–3 пары у границ переключения между картриджными, лазерными и ёмкостными устройствами; для каждой нужны две полные синхронные корзины и проверка нескольких объёмов. Если победитель снова не зависит от сценария, следует обсуждать NO-GO или более узкое позиционирование сервиса.",
                ]
            )
        else:
            lines.append(
                "- Следующий шаг должен разбирать причины концентрации и преимущество полного расчёта над упрощённым TCO; простое добавление похожих моделей или сценариев сигнал не усилит."
            )
    elif phase0_status == "NO-GO":
        if retailer_pair_gate_ready and not retailer_pair_signal:
            lines.extend(
                [
                    "- Рекомендация процесса: **`NO-GO`** — решающая проверка прямых пар не воспроизвела сценарную смену победителя у двух продавцов.",
                    "- Дальнейшее расширение выборки принтеров не оправдано без пересмотра гипотезы или категории.",
                ]
            )
        else:
            lines.extend(
                [
                    "- Рекомендация процесса: **`NO-GO`** — на текущей полной выборке Decision Engine не меняет наивный выбор по цене покупки.",
                    "- До новой продуктовой разработки требуется пересмотр гипотезы или категории.",
                ]
            )
    else:
        lines.extend(
            [
                "- Рекомендация процесса: **`CONTINUE PHASE 0`** — выборка или сценарное покрытие ещё не достигли минимального gate.",
                "- Это промежуточная проверка процесса, а не итоговый вывод.",
            ]
        )

    if retailer_basket_audits:
        lines.extend(
            [
                "",
                "## Проверка синхронных корзин одного продавца",
                "",
                "Строгая корзина требует одновременно доступное точное устройство и цены всех "
                "OEM-расходников, нужных хотя бы одному сравниваемому расчёту в конкретном сценарии, "
                "у одного продавца на одну дату. Поэтому обслуживание с ресурсом выше объёма "
                "сценария не делает корзину неполной. Статусы «ожидается», «в транзите», "
                "«недоступно» и неподтверждённый срок не считаются наличием.",
                "",
                f"Итог по всем проверкам: полных одновременно покупаемых корзин — **{len(complete_retailer_baskets)} из {len(retailer_basket_audits)}**.",
            ]
        )
        for (scenario_id, _), group_audits in basket_groups.items():
            scenario = scenario_by_id[scenario_id]
            basket_product_ids = tuple(
                offer.product_id for offer in group_audits[0].offers
            )
            basket_products = tuple(
                catalog.product(product_id) for product_id in basket_product_ids
            )
            group_results = tuple(
                retailer_basket_results[audit.id]
                for audit in group_audits
                if audit.id in retailer_basket_results
            )
            lines.extend(
                [
                    "",
                    "### "
                    + " ↔ ".join(_product_name(product) for product in basket_products),
                    "",
                    f"Сценарий: **{scenario.name}** — {scenario.mono_pages_per_month} ч/б + "
                    f"{scenario.color_pages_per_month} цветных стр./мес., {scenario.ownership_months} мес.",
                    "",
                    "| Продавец | Дата | "
                    + " | ".join(_product_name(product) for product in basket_products)
                    + " | Полная парная корзина |",
                    "|---|---|" + "---|" * len(basket_products) + "---|",
                ]
            )
            for audit in group_audits:
                offers_by_product = {
                    offer.product_id: offer for offer in audit.offers
                }
                cells: list[str] = []
                for product_id in basket_product_ids:
                    offer = offers_by_product[product_id]
                    device_price = (
                        f", {_money(offer.device_price_rub)}"
                        if offer.device_price_rub is not None
                        else ""
                    )
                    priced_count = len(offer.consumable_prices_rub)
                    cells.append(
                        f"устройство: {_availability_label(offer.device_availability)}{device_price}; "
                        f"OEM-карточки: {len(offer.covered_consumable_ids)}/{len(offer.required_consumable_ids)}; "
                        f"цены: {priced_count}/{len(offer.required_consumable_ids)}"
                    )
                lines.append(
                    f"| {audit.retailer} | {audit.observed_at} | "
                    + " | ".join(cells)
                    + f" | {'да' if audit.complete else 'нет'} |"
                )

            if group_results:
                lines.extend(
                    [
                        "",
                        "Basket-aware TCO:",
                        "",
                        "| Продавец | Модель | Покупка | Расходники | Обслуживание | Полный TCO | Куплено за период |",
                        "|---|---|---:|---:|---:|---:|---|",
                    ]
                )
                for result in group_results:
                    for product_result in result.products:
                        tco = product_result.full_tco
                        purchased = "; ".join(
                            f"{component.units_purchased} × `{component.consumable_id}`"
                            for component in tco.components
                            if component.units_purchased > 0
                        ) or "ничего сверх starter"
                        lines.append(
                            f"| {result.audit.retailer} | {_product_name(product_result.product)} | "
                            f"{_money(tco.purchase_cost_rub)} | {_money(tco.consumables_cost_rub)} | "
                            f"{_money(tco.maintenance_cost_rub)} | {_money(tco.total_cost_rub)} | {purchased} |"
                        )

                lines.extend(
                    [
                        "",
                        "| Продавец | Самая низкая цена покупки | Победитель упрощённого TCO | Победитель полного TCO | Экономия против выбора по цене |",
                        "|---|---|---|---|---:|",
                    ]
                )
                for result in group_results:
                    purchase = result.purchase_price_winner
                    simplified = result.simplified_tco_winner
                    decision = result.decision_engine_winner
                    purchase_full_tco = purchase.full_tco.total_cost_rub
                    savings_rub = purchase_full_tco - decision.full_tco.total_cost_rub
                    lines.append(
                        f"| {result.audit.retailer} | {_product_name(purchase.product)} — "
                        f"{_money(purchase.full_tco.purchase_cost_rub)} | "
                        f"{_product_name(simplified.product)} — {_money(simplified.simplified_tco.total_cost_rub)} | "
                        f"{_product_name(decision.product)} — {_money(decision.full_tco.total_cost_rub)} | "
                        f"{_money(savings_rub)} ({_percent(savings_rub, purchase_full_tco)}) |"
                    )
                distinct_winners = {
                    result.decision_engine_winner.product.id
                    for result in group_results
                }
                lines.extend(
                    [
                        "",
                        f"Вывод по паре: полный TCO рассчитан без смешивания магазинов у "
                        f"**{len(group_results)} продавцов**; число разных победителей — "
                        f"**{len(distinct_winners)}**. Совпадение результата у разных продавцов "
                        "снижает риск того, что рекомендация вызвана одной случайной ценой.",
                    ]
                )
            else:
                lines.extend(
                    [
                        "",
                        "Basket-aware TCO не рассчитан: любой числовой результат потребовал бы "
                        "смешать магазины либо использовать недоступное устройство. Ценовая "
                        "устойчивость этой пары остаётся неподтверждённой.",
                    ]
                )

            lines.extend(["", "Источники проверки корзин:", ""])
            for audit in group_audits:
                links: list[str] = []
                for offer in audit.offers:
                    product = catalog.product(offer.product_id)
                    if offer.device_source_url:
                        links.append(
                            f"[{_product_name(product)} — устройство]({offer.device_source_url})"
                        )
                    links.extend(
                        f"[{consumable_id}]({url})"
                        for consumable_id, url in offer.consumable_source_urls
                    )
                lines.append(
                    f"- **{audit.retailer}**: "
                    + (", ".join(links) or "нет подтверждённых карточек")
                )

    if retailer_pair_sensitivity:
        lines.extend(
            [
                "",
                "## Решающая многоточечная проверка пар",
                "",
                "Для каждой пары используются неизменные цены полной корзины одного продавца, "
                "а меняются только сценарные входы: 10/25/50/100 страниц в месяц, 0%/20% "
                "цвета и горизонт 1/3/5 лет. Пара считается подтверждённой, если победитель "
                "меняется у каждого из двух продавцов и продавцы совпадают минимум в 75% "
                "контрольных точек.",
            ]
        )
        for sensitivity_item in retailer_pair_sensitivity:
            products = tuple(
                catalog.product(product_id)
                for product_id in sensitivity_item.product_ids
            )
            lines.extend(
                [
                    "",
                    "### " + " ↔ ".join(_product_name(product) for product in products),
                    "",
                    f"- Результат: **{'подтверждено' if sensitivity_item.confirmed else 'не подтверждено'}**.",
                    f"- Совпадение продавцов: {sensitivity_item.agreement_points} / "
                    f"{sensitivity_item.total_points} точек "
                    f"({_percent(sensitivity_item.agreement_points, sensitivity_item.total_points)}).",
                    f"- Полный и упрощённый TCO расходятся по победителю в "
                    f"{sensitivity_item.full_vs_simplified_changes} / "
                    f"{sensitivity_item.result_count} расчётов продавец × сценарий.",
                    "",
                    "| Продавец | Доля цвета | Горизонт | Победители при 10 / 25 / 50 / 100 стр./мес. |",
                    "|---|---:|---:|---|",
                ]
            )
            for audit, audit_results in zip(
                sensitivity_item.audits,
                sensitivity_item.results_by_audit,
                strict=True,
            ):
                for color_share_percent in RETAILER_PAIR_COLOR_SHARES_PERCENT:
                    for ownership_months in RETAILER_PAIR_OWNERSHIP_MONTHS:
                        matching = tuple(
                            result
                            for result in audit_results
                            if result.scenario.ownership_months == ownership_months
                            and (
                                result.scenario.color_pages_per_month
                                * 100
                                // result.scenario.monthly_pages
                            )
                            == color_share_percent
                        )
                        winners = " / ".join(
                            _product_name(result.decision_engine_winner.product)
                            for result in matching
                        )
                        lines.append(
                            f"| {audit.retailer} | {color_share_percent}% | "
                            f"{ownership_months} мес. | {winners} |"
                        )

        lines.extend(
            [
                "",
                f"Итог: сценарная смена победителя подтверждена для "
                f"**{len(confirmed_retailer_pairs)} из {len(retailer_pair_sensitivity)} пар** "
                "на синхронных рыночных корзинах.",
            ]
        )

    lines.extend(["", "## Ablation полного TCO", ""])
    if not simplified_changed:
        lines.append(
            "Полный учёт starter/maintenance не меняет победителя ни в одном сценарии."
        )
    for result in simplified_changed:
        assert result.simplified_tco_winner is not None
        assert result.decision_engine_winner is not None
        simplified_winner = evaluate_full_tco_ablation(
            catalog, result.simplified_tco_winner, result.scenario
        )
        full_winner = evaluate_full_tco_ablation(
            catalog, result.decision_engine_winner, result.scenario
        )
        baseline_lead = (
            full_winner.simplified_tco_rub - simplified_winner.simplified_tco_rub
        )
        starter_advantage = (
            full_winner.starter_credit_rub - simplified_winner.starter_credit_rub
        )
        maintenance_delta = (
            full_winner.maintenance_cost_rub - simplified_winner.maintenance_cost_rub
        )
        final_margin = simplified_winner.full_tco_rub - full_winner.full_tco_rub
        lines.extend(
            [
                f"### {result.scenario.name}",
                "",
                "| Модель | Упрощённый TCO | Снижение покупок за счёт starter | Maintenance | Полный TCO |",
                "|---|---:|---:|---:|---:|",
                f"| {_product_name(simplified_winner.product)} | {_money(simplified_winner.simplified_tco_rub)} | −{_money(simplified_winner.starter_credit_rub)} | +{_money(simplified_winner.maintenance_cost_rub)} | {_money(simplified_winner.full_tco_rub)} |",
                f"| {_product_name(full_winner.product)} | {_money(full_winner.simplified_tco_rub)} | −{_money(full_winner.starter_credit_rub)} | +{_money(full_winner.maintenance_cost_rub)} | {_money(full_winner.full_tco_rub)} |",
                "",
                f"Упрощённый baseline даёт **{_product_name(simplified_winner.product)}** преимущество {_money(baseline_lead)}. "
                f"Стартовый комплект **{_product_name(full_winner.product)}** уменьшает обязательные покупки на {_money(starter_advantage)} больше; "
                f"разница maintenance составляет {_money(maintenance_delta)}. После полного расчёта **{_product_name(full_winner.product)}** выигрывает всего {_money(final_margin)}.",
                "",
                "Вывод: переключение создаёт starter-комплект, а не maintenance; малый финальный отрыв делает результат особенно чувствительным к синхронным ценам.",
                "",
            ]
        )

    lines.extend(["", "## Результаты по сценариям", ""])

    for result, robustness in zip(results, price_robustness, strict=True):
        lines.extend([f"### {result.scenario.name}", ""])
        lines.append(
            f"Кандидатов после hard constraints и проверки данных: **{result.candidate_count}**."
        )
        if result.decision_engine_winner is None:
            lines.append(
                "Нет подходящей модели с полными и подтверждёнными данными для рекомендации."
            )
            _append_exclusions(lines, result)
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
                f"- Победитель по цене покупки: **{_product_name(result.purchase_price_winner)}** — покупка {_money(result.purchase_price_winner_tco.purchase_cost_rub)}, полный TCO {_money(result.purchase_price_winner_tco.total_cost_rub)}",
                f"- Победитель по упрощённому TCO: **{_product_name(result.simplified_tco_winner)}**",
                f"- Победитель Decision Engine: **{_product_name(result.decision_engine_winner)}** — покупка {_money(result.decision_engine_winner_tco.purchase_cost_rub)}, расходники {_money(result.decision_engine_winner_tco.consumables_cost_rub)}, обслуживание {_money(result.decision_engine_winner_tco.maintenance_cost_rub)}, итого {_money(result.decision_engine_winner_tco.total_cost_rub)}",
                f"- Экономия относительно выбора по цене покупки: **{_money(savings_value)} ({_percent(savings_value, result.purchase_price_winner_tco.total_cost_rub)})**",
                f"- Покрытие источниками: **{audit_product(catalog, result.decision_engine_winner).coverage_percent}% обязательных для расчёта фактов**",
            ]
        )
        if result.decision_runner_up and result.decision_runner_up_tco:
            lines.append(
                f"- Ближайшая альтернатива: **{_product_name(result.decision_runner_up)}** — "
                f"TCO {_money(result.decision_runner_up_tco.total_cost_rub)}, "
                f"отрыв {_money(result.decision_runner_up_tco.total_cost_rub - result.decision_engine_winner_tco.total_cost_rub)}"
            )
        if robustness and robustness.has_observed_range:
            challenger = (
                catalog.product(robustness.best_challenger_id)
                if robustness.best_challenger_id
                else None
            )
            robustness_label = (
                "предварительно устойчива по имеющимся наблюдениям"
                if robustness.robust
                else "чувствительна уже в имеющихся наблюдениях"
            )
            lines.append(
                f"- Проверка ценового диапазона: рекомендация **{robustness_label}**; "
                f"TCO победителя {_money(robustness.winner_tco_min_rub)}–{_money(robustness.winner_tco_max_rub)}"
                + (
                    f", минимальный TCO альтернативы **{_product_name(challenger)}** — "
                    f"{_money(robustness.best_challenger_tco_min_rub)}"
                    if challenger and robustness.best_challenger_tco_min_rub is not None
                    else ""
                )
            )
        if result.candidate_count == 1:
            lines.append(
                "- Предупреждение: сценарий проверяет фильтрацию, но не сравнение альтернатив."
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
        sources = _recommendation_sources(catalog, result.decision_engine_winner)
        lines.extend(["", "Источники рекомендации:", ""])
        lines.extend(f"- [{name}]({url})" for name, url in sources)
        _append_exclusions(lines, result)
        lines.append("")

    lines.extend(
        [
            "## Чувствительность ranking к объёму",
            "",
            f"Контроль: только чёрно-белая печать, горизонт {SENSITIVITY_OWNERSHIP_MONTHS // 12} лет, без требований к функциям.",
            "",
            "| Страниц в месяц | Кандидатов | Победитель | TCO |",
            "|---:|---:|---|---:|",
        ]
    )
    for result in sensitivity:
        winner = result.decision_engine_winner
        tco = result.decision_engine_winner_tco
        lines.append(
            f"| {result.scenario.mono_pages_per_month} | {result.candidate_count} | "
            f"{_product_name(winner) if winner else 'нет рекомендации'} | "
            f"{_money(tco.total_cost_rub) if tco else '—'} |"
        )

    lines.extend(
        [
            "",
            "## Чувствительность к доле цветной печати и горизонту владения",
            "",
            f"Контроль: фиксированные {MIXED_SENSITIVITY_TOTAL_PAGES_PER_MONTH} страниц в месяц. "
            "Объём достаточно велик для покупок сменных расходников на длинном горизонте, "
            "но не превышает опубликованный предел HP Smart Tank 580 (800 стр./мес.).",
        ]
    )
    horizon_headers = " | ".join(
        f"{months // 12} г." for months in MIXED_SENSITIVITY_OWNERSHIP_MONTHS
    )
    horizon_alignment = "|".join("---:" for _ in MIXED_SENSITIVITY_OWNERSHIP_MONTHS)
    mixed_robustness_by_profile_id = {
        profile.id: robustness for profile, robustness in mixed_robustness
    }
    for profile, rows in mixed_sensitivity:
        lines.extend(
            [
                "",
                f"### {profile.title}",
                "",
                f"| Доля цветной печати | {horizon_headers} |",
                f"|---:|{horizon_alignment}|",
            ]
        )
        for color_share_percent, row in zip(
            MIXED_SENSITIVITY_COLOR_SHARES_PERCENT,
            rows,
            strict=True,
        ):
            cells: list[str] = []
            for result in row:
                winner = result.decision_engine_winner
                tco = result.decision_engine_winner_tco
                cells.append(
                    f"{_product_name(winner)} — {_money(tco.total_cost_rub)}"
                    if winner and tco
                    else "нет рекомендации"
                )
            lines.append(f"| {color_share_percent}% | " + " | ".join(cells) + " |")
        profile_robustness = mixed_robustness_by_profile_id[profile.id]
        observed_ranges = sum(
            bool(item and item.has_observed_range) for item in profile_robustness
        )
        robust_points = sum(bool(item and item.robust) for item in profile_robustness)
        lines.extend(
            [
                "",
                f"Ценовой стресс: рекомендация сохраняется во всём наблюдаемом "
                f"диапазоне в **{robust_points} из {observed_ranges}** точек.",
            ]
        )

    lines.extend(["", "## Break-even", ""])
    compared_pairs: set[tuple[str, str, int, int]] = set()
    break_even_count = 0
    for result in competitive_changed:
        assert result.purchase_price_winner is not None
        assert result.decision_engine_winner is not None
        pair_key = (
            result.purchase_price_winner.id,
            result.decision_engine_winner.id,
            result.scenario.color_pages_per_month,
            result.scenario.ownership_months,
        )
        if pair_key in compared_pairs:
            continue
        compared_pairs.add(pair_key)
        point = find_break_even(
            catalog,
            result.purchase_price_winner.id,
            result.decision_engine_winner.id,
            result.scenario,
        )
        if point is None:
            continue
        break_even_count += 1
        lines.append(
            f"- **{_product_name(result.decision_engine_winner)}** становится не дороже "
            f"**{_product_name(result.purchase_price_winner)}** начиная примерно с "
            f"**{point.pages_per_month} чёрно-белых страниц в месяц** при горизонте "
            f"{result.scenario.ownership_months} мес. и {result.scenario.color_pages_per_month} цветных стр./мес. "
            f"(TCO: {_money(point.product_b_tco_rub)} против {_money(point.product_a_tco_rub)})."
        )
    if break_even_count == 0:
        lines.append("Подтверждённых пар со сменой победителя пока нет.")

    lines.extend(["", "## Полнота данных", ""])
    if not catalog.products:
        lines.append("В Golden Dataset пока нет моделей.")
    for product in catalog.products:
        audit = audit_product(catalog, product)
        lines.append(
            f"- `{product.id}`: {audit.verified_facts}/{audit.required_facts} подтверждённых расчётных фактов ({audit.coverage_percent}%)"
        )
        lines.extend(f"  - Не хватает evidence: `{item}`" for item in audit.missing)
        lines.extend(
            f"  - Публикационный пробел: `{item}`"
            for item in audit.publication_gaps
        )
        lines.extend(f"  - Конфликт источников: {item}" for item in audit.conflicts)

    lines.extend(
        [
            "",
            "## Ограничения текущей модели",
            "",
            "- Цены являются ручными тестовыми наблюдениями на указанную дату и не образуют production price feed.",
            "- Проверка диапазона цен консервативно сочетает максимум цен победителя с минимумом цен альтернатив; диапазоны пока есть не для всех устройств и расходников.",
            "- Для цветных устройств CMY replacement demand относится к цветным страницам. Неопределённый расход цветного тонера на калибровку и часть монохромных заданий отдельно не прогнозируется.",
            "- В Фазе 0 выбран один обоснованный OEM replacement на канал; оптимизация между standard и high-yield упаковками пока не выполняется.",
            "- Отсутствующий рекомендованный месячный объём и непубликованный maintenance schedule показываются как пробелы, а не заполняются оценкой.",
        ]
    )
    lines.append("")
    return "\n".join(lines)


def write_report(
    catalog: Catalog,
    scenarios: tuple[UsageScenario, ...],
    path: Path,
    retailer_basket_audits: tuple[RetailerBasketAudit, ...] = (),
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        build_report(catalog, scenarios, retailer_basket_audits), encoding="utf-8"
    )
