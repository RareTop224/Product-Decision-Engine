from __future__ import annotations

from dataclasses import dataclass

from product_decision_engine.domain.catalog import Catalog, MissingCriticalData
from product_decision_engine.domain.models import (
    MaintenanceDataStatus,
    Product,
    ProductConsumableRole,
    VerificationStatus,
)


@dataclass(frozen=True, slots=True)
class EvidenceAudit:
    product_id: str
    required_facts: int
    verified_facts: int
    missing: tuple[str, ...]
    publication_gaps: tuple[str, ...]
    conflicts: tuple[str, ...]

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
        ("product", product.id, "maintenance_data_status"),
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
    publication_gaps: list[str] = []
    if product.recommended_monthly_volume is None:
        publication_gaps.append("recommended_monthly_volume")
    if product.maintenance_data_status == MaintenanceDataStatus.NOT_PUBLISHED:
        publication_gaps.append("maintenance_schedule")

    scoped_entities = {("product", product.id)}
    for link in catalog.links(product.id):
        scoped_entities.add(("product_consumable", link.id))
        scoped_entities.add(("consumable", link.consumable_id))
    conflicts = tuple(
        sorted(
            f"{item.entity_type}:{item.entity_id}.{item.field_name}: {item.notes or item.source_name}"
            for item in catalog.evidence
            if item.verification_status == VerificationStatus.CONFLICT
            and (item.entity_type, item.entity_id) in scoped_entities
        )
    )
    return EvidenceAudit(
        product_id=product.id,
        required_facts=len(unique_requirements),
        verified_facts=len(unique_requirements) - len(missing),
        missing=missing,
        publication_gaps=tuple(publication_gaps),
        conflicts=conflicts,
    )
