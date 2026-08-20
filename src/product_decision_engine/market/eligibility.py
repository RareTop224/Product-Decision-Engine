from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from product_decision_engine.domain.catalog import Catalog, MissingCriticalData
from product_decision_engine.domain.models import (
    AvailabilityObservation,
    OfferAvailability,
    Product,
    ProductConsumableRole,
    ProductLifecycleObservation,
    ProductLifecycleStatus,
    RetailerBasketAudit,
    VerificationStatus,
)
from product_decision_engine.evidence import audit_product


@dataclass(frozen=True, slots=True)
class FreshnessPolicy:
    price_max_age_days: int = 30
    availability_max_age_days: int = 7

    def __post_init__(self) -> None:
        if self.price_max_age_days < 0:
            raise ValueError("price_max_age_days must not be negative")
        if self.availability_max_age_days < 0:
            raise ValueError("availability_max_age_days must not be negative")


@dataclass(frozen=True, slots=True)
class MarketEligibilityAudit:
    product_id: str
    eligible: bool
    blocking_reasons: tuple[str, ...]
    warnings: tuple[str, ...]


def _parse_date(value: str, field_name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field_name} must be an ISO date: {value!r}") from error


def _required_price_entities(
    catalog: Catalog,
    product: Product,
) -> tuple[tuple[str, str], ...]:
    consumable_ids = {
        link.consumable_id
        for link in catalog.links(product.id)
        if link.role
        in {
            ProductConsumableRole.REPLACEMENT,
            ProductConsumableRole.MAINTENANCE,
        }
    }
    return (
        ("product", product.id),
        *(('consumable', consumable_id) for consumable_id in sorted(consumable_ids)),
    )


def audit_market_eligibility(
    catalog: Catalog,
    product: Product,
    availability_observations: tuple[AvailabilityObservation, ...],
    lifecycle_observations: tuple[ProductLifecycleObservation, ...] = (),
    *,
    as_of: date,
    policy: FreshnessPolicy = FreshnessPolicy(),
) -> MarketEligibilityAudit:
    """Decide whether a product may enter a current recommendation set.

    Technical completeness remains the responsibility of the normal ranking
    eligibility check. This audit adds the time-dependent market gate: every
    mandatory price must be verified and fresh, the device must have a fresh
    verified in-stock observation, and a discontinued signal must block it.
    """

    blocking: list[str] = []
    warnings: list[str] = []

    if product.status != "active":
        blocking.append(f"inactive_product_status:{product.status}")

    evidence_audit = audit_product(catalog, product)
    if evidence_audit.missing:
        blocking.append(f"incomplete_critical_facts:{len(evidence_audit.missing)}")
    if evidence_audit.conflicts:
        warnings.append(f"conflicting_facts:{len(evidence_audit.conflicts)}")

    for entity_type, entity_id in _required_price_entities(catalog, product):
        entity_key = f"{entity_type}:{entity_id}"
        try:
            observation = catalog.latest_price(entity_type, entity_id)
        except MissingCriticalData:
            blocking.append(f"missing_primary_price:{entity_key}")
            continue
        if not catalog.has_verified_evidence(
            "price_observation", observation.id, "price_rub"
        ):
            blocking.append(f"unverified_price:{entity_key}")
        observed_at = _parse_date(observation.observed_at, "price observed_at")
        age_days = (as_of - observed_at).days
        if age_days < 0:
            blocking.append(f"future_price:{entity_key}")
        elif age_days > policy.price_max_age_days:
            blocking.append(f"stale_price:{entity_key}:{age_days}d")

    verified_availability = tuple(
        item
        for item in availability_observations
        if item.product_id == product.id
        and item.verification_status == VerificationStatus.VERIFIED
    )
    if not verified_availability:
        blocking.append("missing_verified_availability")
    else:
        future_availability = tuple(
            item
            for item in verified_availability
            if _parse_date(item.observed_at, "availability observed_at") > as_of
        )
        if future_availability:
            blocking.append("future_availability_observation")
        fresh_availability = tuple(
            item
            for item in verified_availability
            if 0
            <= (as_of - _parse_date(item.observed_at, "availability observed_at")).days
            <= policy.availability_max_age_days
        )
        if not fresh_availability:
            latest = max(
                verified_availability,
                key=lambda item: (item.observed_at, item.id),
            )
            latest_age = (
                as_of - _parse_date(latest.observed_at, "availability observed_at")
            ).days
            if latest_age >= 0:
                blocking.append(f"stale_availability:{latest_age}d")
        elif not any(
            item.availability == OfferAvailability.IN_STOCK
            for item in fresh_availability
        ):
            states = ",".join(
                sorted({item.availability.value for item in fresh_availability})
            )
            blocking.append(f"not_in_stock:{states}")

    verified_lifecycle = tuple(
        item
        for item in lifecycle_observations
        if item.product_id == product.id
        and item.verification_status == VerificationStatus.VERIFIED
    )
    if not verified_lifecycle:
        warnings.append("lifecycle_unknown")
    else:
        non_future = tuple(
            item
            for item in verified_lifecycle
            if _parse_date(item.observed_at, "lifecycle observed_at") <= as_of
        )
        if len(non_future) != len(verified_lifecycle):
            blocking.append("future_lifecycle_observation")
        if non_future:
            latest_lifecycle = max(
                non_future,
                key=lambda item: (item.observed_at, item.id),
            )
            if latest_lifecycle.lifecycle_status == ProductLifecycleStatus.DISCONTINUED:
                blocking.append("product_discontinued")
            elif latest_lifecycle.lifecycle_status == ProductLifecycleStatus.UNKNOWN:
                warnings.append("lifecycle_unknown")

    unique_blocking = tuple(dict.fromkeys(blocking))
    unique_warnings = tuple(dict.fromkeys(warnings))
    return MarketEligibilityAudit(
        product_id=product.id,
        eligible=not unique_blocking,
        blocking_reasons=unique_blocking,
        warnings=unique_warnings,
    )


def availability_from_retailer_audits(
    audits: tuple[RetailerBasketAudit, ...],
) -> tuple[AvailabilityObservation, ...]:
    observations: list[AvailabilityObservation] = []
    for audit in audits:
        provider_key = "retailer:" + "-".join(audit.retailer.lower().split())
        for offer in audit.offers:
            observations.append(
                AvailabilityObservation(
                    id=f"{audit.id}:{offer.product_id}",
                    product_id=offer.product_id,
                    source_provider_key=provider_key,
                    availability=offer.device_availability,
                    observed_at=audit.observed_at,
                    verification_status=audit.verification_status,
                    source_url=offer.device_source_url,
                )
            )
    return tuple(observations)
