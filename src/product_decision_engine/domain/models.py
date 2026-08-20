from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ProductType(StrEnum):
    PRINTER = "printer"
    MFP = "mfp"


class ColorMode(StrEnum):
    MONO = "mono"
    COLOR = "color"


class ConsumableKind(StrEnum):
    TONER = "toner"
    CARTRIDGE = "cartridge"
    INK_BOTTLE = "ink_bottle"
    DRUM = "drum"
    MAINTENANCE_BOX = "maintenance_box"
    OTHER = "other"


class ProductConsumableRole(StrEnum):
    STARTER = "starter"
    REPLACEMENT = "replacement"
    MAINTENANCE = "maintenance"


class PageScope(StrEnum):
    MONO_PAGES = "mono_pages"
    COLOR_PAGES = "color_pages"
    ALL_PAGES = "all_pages"


class VerificationStatus(StrEnum):
    CANDIDATE = "candidate"
    VERIFIED = "verified"
    CONFLICT = "conflict"
    REJECTED = "rejected"


class MaintenanceDataStatus(StrEnum):
    COMPLETE = "complete"
    NOT_PUBLISHED = "not_published"
    INCOMPLETE = "incomplete"


class OfferAvailability(StrEnum):
    IN_STOCK = "in_stock"
    ORDERABLE_UNCONFIRMED = "orderable_unconfirmed"
    EXPECTED = "expected"
    TRANSIT = "transit"
    UNAVAILABLE = "unavailable"
    NOT_LISTED = "not_listed"
    UNVERIFIED = "unverified"


@dataclass(frozen=True, slots=True)
class Product:
    id: str
    manufacturer: str
    model: str
    product_type: ProductType
    print_technology: str
    color_mode: ColorMode
    wifi: bool
    auto_duplex: bool
    recommended_monthly_volume: int | None
    expected_consumable_channels: tuple[str, ...]
    maintenance_data_status: MaintenanceDataStatus
    status: str = "active"
    mpn: str | None = None

    def __post_init__(self) -> None:
        if self.recommended_monthly_volume is not None and self.recommended_monthly_volume <= 0:
            raise ValueError("recommended_monthly_volume must be positive")
        if not self.expected_consumable_channels:
            raise ValueError("expected_consumable_channels must not be empty")
        if len(set(self.expected_consumable_channels)) != len(self.expected_consumable_channels):
            raise ValueError("expected_consumable_channels must be unique")


@dataclass(frozen=True, slots=True)
class Consumable:
    id: str
    manufacturer: str
    part_number: str
    kind: ConsumableKind
    color: str
    yield_value: int
    yield_unit: str
    is_oem: bool
    yield_standard: str | None = None

    def __post_init__(self) -> None:
        if self.yield_value <= 0:
            raise ValueError("yield_value must be positive")


@dataclass(frozen=True, slots=True)
class ProductConsumable:
    id: str
    product_id: str
    consumable_id: str
    role: ProductConsumableRole
    channel: str
    page_scope: PageScope
    quantity_in_box: int = 1
    mono_page_weight: int = 1
    color_page_weight: int = 1
    installed_yield_value: int | None = None
    notes: str | None = None

    def __post_init__(self) -> None:
        if self.quantity_in_box <= 0:
            raise ValueError("quantity_in_box must be positive")
        if self.mono_page_weight < 0 or self.color_page_weight < 0:
            raise ValueError("page weights must not be negative")
        if self.mono_page_weight == 0 and self.color_page_weight == 0:
            raise ValueError("at least one page weight must be positive")
        if self.installed_yield_value is not None and self.installed_yield_value <= 0:
            raise ValueError("installed_yield_value must be positive")


@dataclass(frozen=True, slots=True)
class PriceObservation:
    id: str
    entity_type: str
    entity_id: str
    price_rub: int
    source_id: str
    observed_at: str
    is_primary: bool = True

    def __post_init__(self) -> None:
        if self.price_rub < 0:
            raise ValueError("price_rub must not be negative")


@dataclass(frozen=True, slots=True)
class Evidence:
    id: str
    entity_type: str
    entity_id: str
    field_name: str
    source_type: str
    source_name: str
    source_url: str
    observed_at: str
    verification_status: VerificationStatus
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class RetailerProductCoverage:
    product_id: str
    device_availability: OfferAvailability
    device_price_rub: int | None
    device_source_url: str | None
    required_consumable_ids: tuple[str, ...]
    covered_consumable_ids: tuple[str, ...]
    consumable_source_urls: tuple[tuple[str, str], ...]
    consumable_prices_rub: tuple[tuple[str, int], ...]
    notes: str | None = None

    def __post_init__(self) -> None:
        if self.device_price_rub is not None and self.device_price_rub < 0:
            raise ValueError("device_price_rub must not be negative")
        if self.device_source_url is not None and not self.device_source_url.strip():
            raise ValueError("device_source_url must not be blank")
        if len(set(self.required_consumable_ids)) != len(self.required_consumable_ids):
            raise ValueError("required_consumable_ids must be unique")
        if len(set(self.covered_consumable_ids)) != len(self.covered_consumable_ids):
            raise ValueError("covered_consumable_ids must be unique")
        unknown = set(self.covered_consumable_ids) - set(self.required_consumable_ids)
        if unknown:
            raise ValueError(
                "covered consumables must be required: " + ", ".join(sorted(unknown))
            )
        sourced = {consumable_id for consumable_id, _ in self.consumable_source_urls}
        if len(sourced) != len(self.consumable_source_urls):
            raise ValueError("consumable_source_urls ids must be unique")
        if any(not source_url.strip() for _, source_url in self.consumable_source_urls):
            raise ValueError("consumable source URLs must not be blank")
        if sourced != set(self.covered_consumable_ids):
            raise ValueError("every covered consumable must have exactly one source URL")
        priced = {consumable_id for consumable_id, _ in self.consumable_prices_rub}
        if len(priced) != len(self.consumable_prices_rub):
            raise ValueError("consumable_prices_rub ids must be unique")
        unknown_prices = priced - set(self.covered_consumable_ids)
        if unknown_prices:
            raise ValueError(
                "priced consumables must be covered: "
                + ", ".join(sorted(unknown_prices))
            )
        if any(price_rub < 0 for _, price_rub in self.consumable_prices_rub):
            raise ValueError("consumable prices must not be negative")

    @property
    def consumables_covered(self) -> bool:
        return set(self.covered_consumable_ids) == set(self.required_consumable_ids)

    @property
    def consumables_complete(self) -> bool:
        priced = {consumable_id for consumable_id, _ in self.consumable_prices_rub}
        return self.consumables_covered and priced == set(self.required_consumable_ids)

    @property
    def consumable_price_map(self) -> dict[str, int]:
        return dict(self.consumable_prices_rub)

    @property
    def complete(self) -> bool:
        return (
            self.device_availability == OfferAvailability.IN_STOCK
            and self.device_price_rub is not None
            and self.device_source_url is not None
            and self.consumables_complete
        )


@dataclass(frozen=True, slots=True)
class RetailerBasketAudit:
    id: str
    retailer: str
    observed_at: str
    source_type: str
    verification_status: VerificationStatus
    scenario_id: str
    offers: tuple[RetailerProductCoverage, ...]

    def __post_init__(self) -> None:
        product_ids = [offer.product_id for offer in self.offers]
        if len(product_ids) < 2:
            raise ValueError("retailer basket audit must compare at least two products")
        if len(set(product_ids)) != len(product_ids):
            raise ValueError("retailer basket audit product ids must be unique")

    @property
    def complete(self) -> bool:
        return (
            self.verification_status == VerificationStatus.VERIFIED
            and all(offer.complete for offer in self.offers)
        )


@dataclass(frozen=True, slots=True)
class UsageScenario:
    id: str
    name: str
    mono_pages_per_month: int
    color_pages_per_month: int
    ownership_months: int
    require_mfp: bool = False
    require_wifi: bool = False
    require_auto_duplex: bool = False
    max_purchase_price_rub: int | None = None

    def __post_init__(self) -> None:
        if self.mono_pages_per_month < 0 or self.color_pages_per_month < 0:
            raise ValueError("monthly page counts must not be negative")
        if self.ownership_months <= 0:
            raise ValueError("ownership_months must be positive")
        if self.max_purchase_price_rub is not None and self.max_purchase_price_rub < 0:
            raise ValueError("max_purchase_price_rub must not be negative")

    @property
    def monthly_pages(self) -> int:
        return self.mono_pages_per_month + self.color_pages_per_month

    @property
    def mono_pages_total(self) -> int:
        return self.mono_pages_per_month * self.ownership_months

    @property
    def color_pages_total(self) -> int:
        return self.color_pages_per_month * self.ownership_months

    @property
    def all_pages_total(self) -> int:
        return self.mono_pages_total + self.color_pages_total
