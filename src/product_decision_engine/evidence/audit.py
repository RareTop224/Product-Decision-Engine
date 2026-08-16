from __future__ import annotations

from dataclasses import dataclass

from product_decision_engine.domain.catalog import Catalog, MissingCriticalData
from product_decision_engine.domain.models import Product, ProductConsumableRole


@dataclass(frozen=True, slots=True)
class EvidenceAudit:
    product_id: str
    required_facts: int
    verified_facts: int
    missing: tuple[str, ...]

    @property
    def coverage_percent(self) -> int:
        if self.required_facts == 0:
            return 0
        return self.verified_facts * 100 // self.required_facts


def audit_product(catalog: Catalog, product: Product) -> EvidenceAudit:
    requirements: list[tuple[str, str, str]] = [
        ("product", product.id, "product_type"),
        ("product", product.id, "color_mode"),
        ("product", product.id, "wifi"),
        ("product", product.id, "auto_duplex"),
    ]
    if product.recommended_monthly_volume is not None:
        requirements.append(("product", product.id, "recommended_monthly_volume"))

    try:
        product_price = catalog.latest_price("product", product.id)
    except MissingCriticalData:
        requirements.append(("price_observation", "<missing product price>", "price_rub"))
    else:
        requirements.append(("price_observation", product_price.id, "price_rub"))

    for link in catalog.links(product.id):
        requirements.append(("product_consumable", link.id, "configuration"))
        requirements.append(("consumable", link.consumable_id, "yield_value"))
        if link.role != ProductConsumableRole.STARTER:
            try:
                price = catalog.latest_price("consumable", link.consumable_id)
            except MissingCriticalData:
                requirements.append(
                    ("price_observation", f"<missing {link.consumable_id} price>", "price_rub")
                )
            else:
                requirements.append(("price_observation", price.id, "price_rub"))

    unique_requirements = tuple(dict.fromkeys(requirements))
    missing = tuple(
        f"{entity_type}:{entity_id}.{field_name}"
        for entity_type, entity_id, field_name in unique_requirements
        if not catalog.has_verified_evidence(entity_type, entity_id, field_name)
    )
    return EvidenceAudit(
        product_id=product.id,
        required_facts=len(unique_requirements),
        verified_facts=len(unique_requirements) - len(missing),
        missing=missing,
    )

