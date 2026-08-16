from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from product_decision_engine.domain.catalog import Catalog
from product_decision_engine.domain.models import (
    ColorMode,
    Consumable,
    ConsumableKind,
    Evidence,
    PageScope,
    PriceObservation,
    Product,
    ProductConsumable,
    ProductConsumableRole,
    ProductType,
    UsageScenario,
    VerificationStatus,
)


def _read_array(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, list):
        raise ValueError(f"{path} must contain a JSON array")
    return value


def _ensure_unique_ids(items: tuple[Any, ...], entity_name: str) -> None:
    ids = [item.id for item in items]
    duplicates = sorted({item_id for item_id in ids if ids.count(item_id) > 1})
    if duplicates:
        raise ValueError(f"Duplicate {entity_name} ids: {', '.join(duplicates)}")


def load_catalog(directory: Path) -> Catalog:
    products = tuple(
        Product(
            **{
                **item,
                "product_type": ProductType(item["product_type"]),
                "color_mode": ColorMode(item["color_mode"]),
                "expected_consumable_channels": tuple(item["expected_consumable_channels"]),
            }
        )
        for item in _read_array(directory / "products.json")
    )
    consumables = tuple(
        Consumable(
            **{
                **item,
                "kind": ConsumableKind(item["kind"]),
            }
        )
        for item in _read_array(directory / "consumables.json")
    )
    product_consumables = tuple(
        ProductConsumable(
            **{
                **item,
                "role": ProductConsumableRole(item["role"]),
                "page_scope": PageScope(item["page_scope"]),
            }
        )
        for item in _read_array(directory / "product_consumables.json")
    )
    prices = tuple(PriceObservation(**item) for item in _read_array(directory / "prices.json"))
    evidence = tuple(
        Evidence(
            **{
                **item,
                "verification_status": VerificationStatus(item["verification_status"]),
            }
        )
        for item in _read_array(directory / "evidence.json")
    )

    for items, name in (
        (products, "product"),
        (consumables, "consumable"),
        (product_consumables, "product_consumable"),
        (prices, "price_observation"),
        (evidence, "evidence"),
    ):
        _ensure_unique_ids(items, name)

    product_ids = {item.id for item in products}
    consumable_ids = {item.id for item in consumables}
    evidence_ids = {item.id for item in evidence}
    for link in product_consumables:
        if link.product_id not in product_ids:
            raise ValueError(f"Unknown product in link {link.id}: {link.product_id}")
        if link.consumable_id not in consumable_ids:
            raise ValueError(f"Unknown consumable in link {link.id}: {link.consumable_id}")
    for price in prices:
        if price.source_id not in evidence_ids:
            raise ValueError(f"Unknown source evidence in price {price.id}: {price.source_id}")

    return Catalog(
        products=products,
        consumables=consumables,
        product_consumables=product_consumables,
        prices=prices,
        evidence=evidence,
    )


def load_scenarios(path: Path) -> tuple[UsageScenario, ...]:
    scenarios = tuple(UsageScenario(**item) for item in _read_array(path))
    _ensure_unique_ids(scenarios, "scenario")
    return scenarios

