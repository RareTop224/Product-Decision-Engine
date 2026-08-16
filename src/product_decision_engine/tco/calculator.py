from __future__ import annotations

from dataclasses import dataclass

from product_decision_engine.domain.catalog import Catalog, MissingCriticalData
from product_decision_engine.domain.models import (
    PageScope,
    ProductConsumable,
    ProductConsumableRole,
    UsageScenario,
)


def _ceil_div(numerator: int, denominator: int) -> int:
    if numerator <= 0:
        return 0
    return (numerator + denominator - 1) // denominator


def _demand_for_scope(scope: PageScope, scenario: UsageScenario) -> int:
    if scope == PageScope.MONO_PAGES:
        return scenario.mono_pages_total
    if scope == PageScope.COLOR_PAGES:
        return scenario.color_pages_total
    if scope == PageScope.ALL_PAGES:
        return scenario.all_pages_total
    raise ValueError(f"Unsupported page scope: {scope}")


@dataclass(frozen=True, slots=True)
class ComponentCost:
    channel: str
    consumable_id: str
    role: ProductConsumableRole
    page_scope: PageScope
    demand_pages: int
    starter_capacity_pages: int
    unit_capacity_pages: int
    units_purchased: int
    unit_price_rub: int
    cost_rub: int


@dataclass(frozen=True, slots=True)
class TcoBreakdown:
    product_id: str
    scenario_id: str
    purchase_cost_rub: int
    consumables_cost_rub: int
    maintenance_cost_rub: int
    total_cost_rub: int
    components: tuple[ComponentCost, ...]


def _validate_evidence(
    catalog: Catalog,
    product_id: str,
    links: tuple[ProductConsumable, ...],
) -> None:
    for field_name in ("product_type", "color_mode", "wifi", "auto_duplex"):
        catalog.require_verified_evidence("product", product_id, field_name)

    product = catalog.product(product_id)
    if product.recommended_monthly_volume is not None:
        catalog.require_verified_evidence(
            "product", product_id, "recommended_monthly_volume"
        )

    product_price = catalog.latest_price("product", product_id)
    catalog.require_verified_evidence("price_observation", product_price.id, "price_rub")

    for link in links:
        catalog.require_verified_evidence("product_consumable", link.id, "configuration")
        consumable = catalog.consumable(link.consumable_id)
        catalog.require_verified_evidence("consumable", consumable.id, "yield_value")
        if link.role != ProductConsumableRole.STARTER:
            price = catalog.latest_price("consumable", consumable.id)
            catalog.require_verified_evidence("price_observation", price.id, "price_rub")


def calculate_tco(
    catalog: Catalog,
    product_id: str,
    scenario: UsageScenario,
    *,
    require_verified_evidence: bool = True,
) -> TcoBreakdown:
    product = catalog.product(product_id)
    issues = catalog.data_issues(product, scenario)
    if issues:
        raise MissingCriticalData("; ".join(issues))

    all_links = catalog.links(product.id)
    if require_verified_evidence:
        _validate_evidence(catalog, product.id, all_links)

    product_price = catalog.latest_price("product", product.id).price_rub
    starter_links = catalog.links(product.id, ProductConsumableRole.STARTER)
    replacement_links = catalog.links(product.id, ProductConsumableRole.REPLACEMENT)
    maintenance_links = catalog.links(product.id, ProductConsumableRole.MAINTENANCE)

    components: list[ComponentCost] = []

    for replacement in replacement_links:
        consumable = catalog.consumable(replacement.consumable_id)
        demand = _demand_for_scope(replacement.page_scope, scenario)
        starters = [link for link in starter_links if link.channel == replacement.channel]
        starter_capacity = sum(
            catalog.consumable(link.consumable_id).yield_value * link.quantity_in_box
            for link in starters
        )
        unit_capacity = consumable.yield_value * replacement.quantity_in_box
        units = _ceil_div(demand - starter_capacity, unit_capacity)
        unit_price = catalog.latest_price("consumable", consumable.id).price_rub
        components.append(
            ComponentCost(
                channel=replacement.channel,
                consumable_id=consumable.id,
                role=replacement.role,
                page_scope=replacement.page_scope,
                demand_pages=demand,
                starter_capacity_pages=starter_capacity,
                unit_capacity_pages=unit_capacity,
                units_purchased=units,
                unit_price_rub=unit_price,
                cost_rub=units * unit_price,
            )
        )

    for maintenance in maintenance_links:
        consumable = catalog.consumable(maintenance.consumable_id)
        demand = _demand_for_scope(maintenance.page_scope, scenario)
        installed_capacity = consumable.yield_value
        package_capacity = consumable.yield_value * maintenance.quantity_in_box
        units = _ceil_div(demand - installed_capacity, package_capacity)
        unit_price = catalog.latest_price("consumable", consumable.id).price_rub
        components.append(
            ComponentCost(
                channel=maintenance.channel,
                consumable_id=consumable.id,
                role=maintenance.role,
                page_scope=maintenance.page_scope,
                demand_pages=demand,
                starter_capacity_pages=installed_capacity,
                unit_capacity_pages=package_capacity,
                units_purchased=units,
                unit_price_rub=unit_price,
                cost_rub=units * unit_price,
            )
        )

    consumables_cost = sum(
        component.cost_rub
        for component in components
        if component.role == ProductConsumableRole.REPLACEMENT
    )
    maintenance_cost = sum(
        component.cost_rub
        for component in components
        if component.role == ProductConsumableRole.MAINTENANCE
    )
    return TcoBreakdown(
        product_id=product.id,
        scenario_id=scenario.id,
        purchase_cost_rub=product_price,
        consumables_cost_rub=consumables_cost,
        maintenance_cost_rub=maintenance_cost,
        total_cost_rub=product_price + consumables_cost + maintenance_cost,
        components=tuple(components),
    )


def calculate_simplified_tco(
    catalog: Catalog,
    product_id: str,
    scenario: UsageScenario,
    *,
    require_verified_evidence: bool = True,
) -> TcoBreakdown:
    """Baseline B: device plus replacement consumables, ignoring starter/maintenance."""
    product = catalog.product(product_id)
    issues = catalog.data_issues(product, scenario)
    if issues:
        raise MissingCriticalData("; ".join(issues))

    replacement_links = catalog.links(product.id, ProductConsumableRole.REPLACEMENT)
    if require_verified_evidence:
        _validate_evidence(catalog, product.id, catalog.links(product.id))

    purchase_cost = catalog.latest_price("product", product.id).price_rub
    components: list[ComponentCost] = []
    for replacement in replacement_links:
        consumable = catalog.consumable(replacement.consumable_id)
        demand = _demand_for_scope(replacement.page_scope, scenario)
        capacity = consumable.yield_value * replacement.quantity_in_box
        units = _ceil_div(demand, capacity)
        unit_price = catalog.latest_price("consumable", consumable.id).price_rub
        components.append(
            ComponentCost(
                channel=replacement.channel,
                consumable_id=consumable.id,
                role=replacement.role,
                page_scope=replacement.page_scope,
                demand_pages=demand,
                starter_capacity_pages=0,
                unit_capacity_pages=capacity,
                units_purchased=units,
                unit_price_rub=unit_price,
                cost_rub=units * unit_price,
            )
        )

    consumables_cost = sum(component.cost_rub for component in components)
    return TcoBreakdown(
        product_id=product.id,
        scenario_id=scenario.id,
        purchase_cost_rub=purchase_cost,
        consumables_cost_rub=consumables_cost,
        maintenance_cost_rub=0,
        total_cost_rub=purchase_cost + consumables_cost,
        components=tuple(components),
    )
