from __future__ import annotations

from dataclasses import replace

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
    VerificationStatus,
)


def make_mono_catalog(
    *,
    prefix: str = "p1",
    purchase_price: int = 10_000,
    starter_yield: int = 500,
    replacement_yield: int = 1_000,
    replacement_price: int = 1_000,
    drum_yield: int | None = None,
    drum_price: int = 2_000,
    wifi: bool = True,
    auto_duplex: bool = True,
    product_type: ProductType = ProductType.PRINTER,
    recommended_monthly_volume: int | None = 5_000,
    verified: bool = True,
) -> Catalog:
    product = Product(
        id=prefix,
        manufacturer="Fixture",
        model=prefix,
        product_type=product_type,
        print_technology="laser",
        color_mode=ColorMode.MONO,
        wifi=wifi,
        auto_duplex=auto_duplex,
        recommended_monthly_volume=recommended_monthly_volume,
        expected_consumable_channels=("black",),
    )
    starter = Consumable(
        id=f"{prefix}-starter",
        manufacturer="Fixture",
        part_number=f"{prefix}-starter",
        kind=ConsumableKind.TONER,
        color="black",
        yield_value=starter_yield,
        yield_unit="pages",
        is_oem=True,
    )
    replacement = Consumable(
        id=f"{prefix}-replacement",
        manufacturer="Fixture",
        part_number=f"{prefix}-replacement",
        kind=ConsumableKind.TONER,
        color="black",
        yield_value=replacement_yield,
        yield_unit="pages",
        is_oem=True,
    )
    links = [
        ProductConsumable(
            id=f"{prefix}-starter-link",
            product_id=prefix,
            consumable_id=starter.id,
            role=ProductConsumableRole.STARTER,
            channel="black",
            page_scope=PageScope.ALL_PAGES,
        ),
        ProductConsumable(
            id=f"{prefix}-replacement-link",
            product_id=prefix,
            consumable_id=replacement.id,
            role=ProductConsumableRole.REPLACEMENT,
            channel="black",
            page_scope=PageScope.ALL_PAGES,
        ),
    ]
    consumables = [starter, replacement]
    prices = [
        PriceObservation(
            id=f"{prefix}-product-price",
            entity_type="product",
            entity_id=prefix,
            price_rub=purchase_price,
            source_id=f"{prefix}-product-price-evidence",
            observed_at="2026-08-16",
        ),
        PriceObservation(
            id=f"{prefix}-replacement-price",
            entity_type="consumable",
            entity_id=replacement.id,
            price_rub=replacement_price,
            source_id=f"{prefix}-replacement-price-evidence",
            observed_at="2026-08-16",
        ),
    ]

    if drum_yield is not None:
        drum = Consumable(
            id=f"{prefix}-drum",
            manufacturer="Fixture",
            part_number=f"{prefix}-drum",
            kind=ConsumableKind.DRUM,
            color="black",
            yield_value=drum_yield,
            yield_unit="pages",
            is_oem=True,
        )
        consumables.append(drum)
        links.append(
            ProductConsumable(
                id=f"{prefix}-drum-link",
                product_id=prefix,
                consumable_id=drum.id,
                role=ProductConsumableRole.MAINTENANCE,
                channel="drum",
                page_scope=PageScope.ALL_PAGES,
            )
        )
        prices.append(
            PriceObservation(
                id=f"{prefix}-drum-price",
                entity_type="consumable",
                entity_id=drum.id,
                price_rub=drum_price,
                source_id=f"{prefix}-drum-price-evidence",
                observed_at="2026-08-16",
            )
        )

    status = VerificationStatus.VERIFIED if verified else VerificationStatus.CONFLICT
    evidence: list[Evidence] = []

    def add_evidence(
        evidence_id: str,
        entity_type: str,
        entity_id: str,
        field_name: str,
    ) -> None:
        evidence.append(
            Evidence(
                id=evidence_id,
                entity_type=entity_type,
                entity_id=entity_id,
                field_name=field_name,
                source_type="fixture",
                source_name="Automated test fixture",
                source_url="fixture://local",
                observed_at="2026-08-16",
                verification_status=status,
            )
        )

    for field_name in ("product_type", "color_mode", "wifi", "auto_duplex"):
        add_evidence(f"{prefix}-product-{field_name}", "product", prefix, field_name)
    if recommended_monthly_volume is not None:
        add_evidence(
            f"{prefix}-product-volume",
            "product",
            prefix,
            "recommended_monthly_volume",
        )
    for consumable in consumables:
        add_evidence(
            f"{consumable.id}-yield-evidence",
            "consumable",
            consumable.id,
            "yield_value",
        )
    for link in links:
        add_evidence(
            f"{link.id}-configuration-evidence",
            "product_consumable",
            link.id,
            "configuration",
        )
    for price in prices:
        add_evidence(
            price.source_id,
            "price_observation",
            price.id,
            "price_rub",
        )

    return Catalog(
        products=(product,),
        consumables=tuple(consumables),
        product_consumables=tuple(links),
        prices=tuple(prices),
        evidence=tuple(evidence),
    )


def merge_catalogs(*catalogs: Catalog) -> Catalog:
    return Catalog(
        products=tuple(item for catalog in catalogs for item in catalog.products),
        consumables=tuple(item for catalog in catalogs for item in catalog.consumables),
        product_consumables=tuple(
            item for catalog in catalogs for item in catalog.product_consumables
        ),
        prices=tuple(item for catalog in catalogs for item in catalog.prices),
        evidence=tuple(item for catalog in catalogs for item in catalog.evidence),
    )

