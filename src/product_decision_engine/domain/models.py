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
    notes: str | None = None

    def __post_init__(self) -> None:
        if self.quantity_in_box <= 0:
            raise ValueError("quantity_in_box must be positive")


@dataclass(frozen=True, slots=True)
class PriceObservation:
    id: str
    entity_type: str
    entity_id: str
    price_rub: int
    source_id: str
    observed_at: str

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

