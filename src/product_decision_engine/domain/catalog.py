from __future__ import annotations

from dataclasses import dataclass

from .models import (
    Consumable,
    Evidence,
    MaintenanceDataStatus,
    PriceObservation,
    Product,
    ProductConsumable,
    ProductConsumableRole,
    UsageScenario,
    VerificationStatus,
)


class MissingCriticalData(ValueError):
    """Raised when a reliable TCO cannot be produced from known facts."""


@dataclass(frozen=True, slots=True)
class Catalog:
    products: tuple[Product, ...]
    consumables: tuple[Consumable, ...]
    product_consumables: tuple[ProductConsumable, ...]
    prices: tuple[PriceObservation, ...]
    evidence: tuple[Evidence, ...]

    def product(self, product_id: str) -> Product:
        matches = [item for item in self.products if item.id == product_id]
        if len(matches) != 1:
            raise MissingCriticalData(f"Expected one product {product_id!r}, found {len(matches)}")
        return matches[0]

    def consumable(self, consumable_id: str) -> Consumable:
        matches = [item for item in self.consumables if item.id == consumable_id]
        if len(matches) != 1:
            raise MissingCriticalData(
                f"Expected one consumable {consumable_id!r}, found {len(matches)}"
            )
        return matches[0]

    def links(
        self,
        product_id: str,
        role: ProductConsumableRole | None = None,
    ) -> tuple[ProductConsumable, ...]:
        return tuple(
            link
            for link in self.product_consumables
            if link.product_id == product_id and (role is None or link.role == role)
        )

    def latest_price(self, entity_type: str, entity_id: str) -> PriceObservation:
        observations = [
            item
            for item in self.prices
            if item.entity_type == entity_type and item.entity_id == entity_id
        ]
        if not observations:
            raise MissingCriticalData(f"Missing price for {entity_type} {entity_id}")
        return max(observations, key=lambda item: (item.observed_at, item.id))

    def has_verified_evidence(self, entity_type: str, entity_id: str, field_name: str) -> bool:
        return any(
            item.entity_type == entity_type
            and item.entity_id == entity_id
            and item.field_name == field_name
            and item.verification_status == VerificationStatus.VERIFIED
            for item in self.evidence
        )

    def require_verified_evidence(
        self,
        entity_type: str,
        entity_id: str,
        field_name: str,
    ) -> None:
        if not self.has_verified_evidence(entity_type, entity_id, field_name):
            raise MissingCriticalData(
                f"Missing verified evidence for {entity_type} {entity_id}.{field_name}"
            )

    def data_issues(self, product: Product, scenario: UsageScenario) -> tuple[str, ...]:
        issues: list[str] = []
        if product.maintenance_data_status == MaintenanceDataStatus.INCOMPLETE:
            issues.append("maintenance data is explicitly incomplete")
        replacement_links = self.links(product.id, ProductConsumableRole.REPLACEMENT)
        starter_links = self.links(product.id, ProductConsumableRole.STARTER)

        for channel in product.expected_consumable_channels:
            replacements = [link for link in replacement_links if link.channel == channel]
            starters = [link for link in starter_links if link.channel == channel]
            if len(replacements) != 1:
                issues.append(
                    f"channel {channel!r}: expected one replacement, found {len(replacements)}"
                )
            if len(starters) != 1:
                issues.append(f"channel {channel!r}: expected one starter, found {len(starters)}")

        if scenario.color_pages_total > 0 and not any(
            link.page_scope.value in {"color_pages", "all_pages"}
            for link in replacement_links
        ):
            issues.append("no replacement consumable covers color pages")
        if scenario.mono_pages_total > 0 and not any(
            link.page_scope.value in {"mono_pages", "all_pages"}
            for link in replacement_links
        ):
            issues.append("no replacement consumable covers mono pages")

        return tuple(issues)
