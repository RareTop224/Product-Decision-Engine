from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence

from product_decision_engine.dataio import (
    load_catalog,
    load_retailer_basket_audits,
    load_scenarios,
)
from product_decision_engine.domain.catalog import Catalog
from product_decision_engine.domain.models import (
    ColorMode,
    Consumable,
    ConsumableKind,
    Evidence,
    MaintenanceDataStatus,
    OfferAvailability,
    PageScope,
    PriceObservation,
    Product,
    ProductConsumable,
    ProductConsumableRole,
    ProductType,
    RetailerBasketAudit,
    RetailerProductCoverage,
    UsageScenario,
    VerificationStatus,
)


class CursorLike(Protocol):
    description: Sequence[Any] | None

    def execute(self, query: str, params: Sequence[Any] | None = None) -> Any: ...

    def executemany(self, query: str, params_seq: Iterable[Sequence[Any]]) -> Any: ...

    def fetchone(self) -> Sequence[Any] | None: ...

    def fetchall(self) -> list[Sequence[Any]]: ...

    def __enter__(self) -> CursorLike: ...

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None: ...


class ConnectionLike(Protocol):
    def execute(self, query: str, params: Sequence[Any] | None = None) -> CursorLike: ...

    def cursor(self) -> CursorLike: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ImportSummary:
    run_id: str
    counts: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class ParityResult:
    catalog_equal: bool
    scenarios_equal: bool
    retailer_basket_audits_equal: bool
    report_equal: bool

    @property
    def ok(self) -> bool:
        return (
            self.catalog_equal
            and self.scenarios_equal
            and self.retailer_basket_audits_equal
            and self.report_equal
        )


def connect_postgres(database_url: str) -> ConnectionLike:
    try:
        import psycopg
    except ImportError as error:
        raise RuntimeError(
            "PostgreSQL support requires the project dependency psycopg. "
            "Install the project with `python -m pip install -e .`."
        ) from error
    return psycopg.connect(database_url)


def apply_migrations(connection: ConnectionLike, migrations_dir: Path) -> tuple[str, ...]:
    connection.execute("CREATE SCHEMA IF NOT EXISTS pde")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS pde.schema_migrations (
            version TEXT PRIMARY KEY,
            checksum_sha256 TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    applied: list[str] = []
    try:
        for path in sorted(migrations_dir.glob("*.sql")):
            version = path.stem
            sql = path.read_text(encoding="utf-8")
            checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            existing = connection.execute(
                "SELECT checksum_sha256 FROM pde.schema_migrations WHERE version = %s",
                (version,),
            ).fetchone()
            if existing is not None:
                if existing[0] != checksum:
                    raise RuntimeError(
                        f"Applied migration {version} has a different checksum"
                    )
                continue
            connection.execute(sql)
            connection.execute(
                """
                INSERT INTO pde.schema_migrations(version, checksum_sha256)
                VALUES (%s, %s)
                """,
                (version, checksum),
            )
            applied.append(version)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return tuple(applied)


def _executemany(
    connection: ConnectionLike,
    query: str,
    rows: Iterable[Sequence[Any]],
) -> None:
    with connection.cursor() as cursor:
        cursor.executemany(query, rows)


def import_golden_dataset(connection: ConnectionLike, data_dir: Path) -> ImportSummary:
    catalog = load_catalog(data_dir)
    scenarios = load_scenarios(data_dir / "scenarios.json")
    audits = load_retailer_basket_audits(data_dir / "retailer_basket_audits.json")
    run_id = str(uuid.uuid4())
    counts = {
        "products": len(catalog.products),
        "consumables": len(catalog.consumables),
        "product_consumables": len(catalog.product_consumables),
        "evidence": len(catalog.evidence),
        "price_observations": len(catalog.prices),
        "usage_scenarios": len(scenarios),
        "retailer_basket_audits": len(audits),
        "retailer_product_offers": sum(len(audit.offers) for audit in audits),
        "retailer_offer_consumables": sum(
            len(offer.required_consumable_ids)
            for audit in audits
            for offer in audit.offers
        ),
        "availability_observations": sum(len(audit.offers) for audit in audits),
    }

    try:
        connection.execute(
            """
            INSERT INTO pde.data_providers(
                provider_key, provider_type, display_name, terms_review_status
            ) VALUES ('golden_json', 'golden_dataset', 'Phase 0 Golden Dataset', 'allowed_for_pilot')
            ON CONFLICT (provider_key) DO UPDATE SET
                display_name = EXCLUDED.display_name,
                terms_review_status = EXCLUDED.terms_review_status,
                updated_at = CURRENT_TIMESTAMP
            """
        )
        connection.execute(
            """
            INSERT INTO pde.import_runs(id, provider_key, status, source_revision)
            VALUES (%s, 'golden_json', 'running', 'phase0-final')
            """,
            (run_id,),
        )

        _executemany(
            connection,
            """
            INSERT INTO pde.products(
                id, manufacturer, model, mpn, product_type, print_technology,
                color_mode, wifi, auto_duplex, recommended_monthly_volume,
                expected_consumable_channels, maintenance_data_status, status,
                dataset_position, last_import_run_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                manufacturer = EXCLUDED.manufacturer,
                model = EXCLUDED.model,
                mpn = EXCLUDED.mpn,
                product_type = EXCLUDED.product_type,
                print_technology = EXCLUDED.print_technology,
                color_mode = EXCLUDED.color_mode,
                wifi = EXCLUDED.wifi,
                auto_duplex = EXCLUDED.auto_duplex,
                recommended_monthly_volume = EXCLUDED.recommended_monthly_volume,
                expected_consumable_channels = EXCLUDED.expected_consumable_channels,
                maintenance_data_status = EXCLUDED.maintenance_data_status,
                status = EXCLUDED.status,
                dataset_position = EXCLUDED.dataset_position,
                last_import_run_id = EXCLUDED.last_import_run_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                (
                    item.id,
                    item.manufacturer,
                    item.model,
                    item.mpn,
                    item.product_type.value,
                    item.print_technology,
                    item.color_mode.value,
                    item.wifi,
                    item.auto_duplex,
                    item.recommended_monthly_volume,
                    list(item.expected_consumable_channels),
                    item.maintenance_data_status.value,
                    item.status,
                    position,
                    run_id,
                )
                for position, item in enumerate(catalog.products)
            ),
        )
        _executemany(
            connection,
            """
            INSERT INTO pde.consumables(
                id, manufacturer, part_number, kind, color, yield_value,
                yield_unit, yield_standard, is_oem, dataset_position,
                last_import_run_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                manufacturer = EXCLUDED.manufacturer,
                part_number = EXCLUDED.part_number,
                kind = EXCLUDED.kind,
                color = EXCLUDED.color,
                yield_value = EXCLUDED.yield_value,
                yield_unit = EXCLUDED.yield_unit,
                yield_standard = EXCLUDED.yield_standard,
                is_oem = EXCLUDED.is_oem,
                dataset_position = EXCLUDED.dataset_position,
                last_import_run_id = EXCLUDED.last_import_run_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                (
                    item.id,
                    item.manufacturer,
                    item.part_number,
                    item.kind.value,
                    item.color,
                    item.yield_value,
                    item.yield_unit,
                    item.yield_standard,
                    item.is_oem,
                    position,
                    run_id,
                )
                for position, item in enumerate(catalog.consumables)
            ),
        )
        _executemany(
            connection,
            """
            INSERT INTO pde.product_consumables(
                id, product_id, consumable_id, role, channel, page_scope,
                quantity_in_box, mono_page_weight, color_page_weight,
                installed_yield_value, notes, dataset_position, last_import_run_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                product_id = EXCLUDED.product_id,
                consumable_id = EXCLUDED.consumable_id,
                role = EXCLUDED.role,
                channel = EXCLUDED.channel,
                page_scope = EXCLUDED.page_scope,
                quantity_in_box = EXCLUDED.quantity_in_box,
                mono_page_weight = EXCLUDED.mono_page_weight,
                color_page_weight = EXCLUDED.color_page_weight,
                installed_yield_value = EXCLUDED.installed_yield_value,
                notes = EXCLUDED.notes,
                dataset_position = EXCLUDED.dataset_position,
                last_import_run_id = EXCLUDED.last_import_run_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                (
                    item.id,
                    item.product_id,
                    item.consumable_id,
                    item.role.value,
                    item.channel,
                    item.page_scope.value,
                    item.quantity_in_box,
                    item.mono_page_weight,
                    item.color_page_weight,
                    item.installed_yield_value,
                    item.notes,
                    position,
                    run_id,
                )
                for position, item in enumerate(catalog.product_consumables)
            ),
        )
        _executemany(
            connection,
            """
            INSERT INTO pde.evidence(
                id, entity_type, entity_id, field_name, source_type, source_name,
                source_url, observed_at, verification_status, notes,
                dataset_position, last_import_run_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                entity_type = EXCLUDED.entity_type,
                entity_id = EXCLUDED.entity_id,
                field_name = EXCLUDED.field_name,
                source_type = EXCLUDED.source_type,
                source_name = EXCLUDED.source_name,
                source_url = EXCLUDED.source_url,
                observed_at = EXCLUDED.observed_at,
                verification_status = EXCLUDED.verification_status,
                notes = EXCLUDED.notes,
                dataset_position = EXCLUDED.dataset_position,
                last_import_run_id = EXCLUDED.last_import_run_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                (
                    item.id,
                    item.entity_type,
                    item.entity_id,
                    item.field_name,
                    item.source_type,
                    item.source_name,
                    item.source_url,
                    item.observed_at,
                    item.verification_status.value,
                    item.notes,
                    position,
                    run_id,
                )
                for position, item in enumerate(catalog.evidence)
            ),
        )
        _executemany(
            connection,
            """
            INSERT INTO pde.price_observations(
                id, entity_type, entity_id, price_rub, source_evidence_id,
                observed_at, is_primary, dataset_position, last_import_run_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                entity_type = EXCLUDED.entity_type,
                entity_id = EXCLUDED.entity_id,
                price_rub = EXCLUDED.price_rub,
                source_evidence_id = EXCLUDED.source_evidence_id,
                observed_at = EXCLUDED.observed_at,
                is_primary = EXCLUDED.is_primary,
                dataset_position = EXCLUDED.dataset_position,
                last_import_run_id = EXCLUDED.last_import_run_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                (
                    item.id,
                    item.entity_type,
                    item.entity_id,
                    item.price_rub,
                    item.source_id,
                    item.observed_at,
                    item.is_primary,
                    position,
                    run_id,
                )
                for position, item in enumerate(catalog.prices)
            ),
        )
        _executemany(
            connection,
            """
            INSERT INTO pde.usage_scenarios(
                id, name, mono_pages_per_month, color_pages_per_month,
                ownership_months, max_purchase_price_rub, require_mfp,
                require_wifi, require_auto_duplex, dataset_position,
                last_import_run_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name,
                mono_pages_per_month = EXCLUDED.mono_pages_per_month,
                color_pages_per_month = EXCLUDED.color_pages_per_month,
                ownership_months = EXCLUDED.ownership_months,
                max_purchase_price_rub = EXCLUDED.max_purchase_price_rub,
                require_mfp = EXCLUDED.require_mfp,
                require_wifi = EXCLUDED.require_wifi,
                require_auto_duplex = EXCLUDED.require_auto_duplex,
                dataset_position = EXCLUDED.dataset_position,
                last_import_run_id = EXCLUDED.last_import_run_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                (
                    item.id,
                    item.name,
                    item.mono_pages_per_month,
                    item.color_pages_per_month,
                    item.ownership_months,
                    item.max_purchase_price_rub,
                    item.require_mfp,
                    item.require_wifi,
                    item.require_auto_duplex,
                    position,
                    run_id,
                )
                for position, item in enumerate(scenarios)
            ),
        )

        for audit_position, audit in enumerate(audits):
            retailer_provider_key = "retailer_" + re.sub(
                r"[^a-z0-9]+", "_", audit.retailer.lower()
            ).strip("_")
            connection.execute(
                """
                INSERT INTO pde.data_providers(
                    provider_key, provider_type, display_name, terms_review_status
                ) VALUES (%s, 'retailer', %s, 'not_reviewed')
                ON CONFLICT (provider_key) DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (retailer_provider_key, audit.retailer),
            )
            connection.execute(
                """
                INSERT INTO pde.retailer_basket_audits(
                    id, retailer, observed_at, source_type, verification_status,
                    scenario_id, dataset_position, last_import_run_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    retailer = EXCLUDED.retailer,
                    observed_at = EXCLUDED.observed_at,
                    source_type = EXCLUDED.source_type,
                    verification_status = EXCLUDED.verification_status,
                    scenario_id = EXCLUDED.scenario_id,
                    dataset_position = EXCLUDED.dataset_position,
                    last_import_run_id = EXCLUDED.last_import_run_id,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    audit.id,
                    audit.retailer,
                    audit.observed_at,
                    audit.source_type,
                    audit.verification_status.value,
                    audit.scenario_id,
                    audit_position,
                    run_id,
                ),
            )
            connection.execute(
                "DELETE FROM pde.retailer_product_offers WHERE audit_id = %s",
                (audit.id,),
            )
            for offer_position, offer in enumerate(audit.offers):
                connection.execute(
                    """
                    INSERT INTO pde.retailer_product_offers(
                        audit_id, product_id, device_availability,
                        device_price_rub, device_source_url, notes, dataset_position
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        audit.id,
                        offer.product_id,
                        offer.device_availability.value,
                        offer.device_price_rub,
                        offer.device_source_url,
                        offer.notes,
                        offer_position,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO pde.availability_observations(
                        id, product_id, source_provider_key, availability,
                        observed_at, source_url, verification_status,
                        retailer_basket_audit_id, dataset_position,
                        last_import_run_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        product_id = EXCLUDED.product_id,
                        source_provider_key = EXCLUDED.source_provider_key,
                        availability = EXCLUDED.availability,
                        observed_at = EXCLUDED.observed_at,
                        source_url = EXCLUDED.source_url,
                        verification_status = EXCLUDED.verification_status,
                        retailer_basket_audit_id = EXCLUDED.retailer_basket_audit_id,
                        dataset_position = EXCLUDED.dataset_position,
                        last_import_run_id = EXCLUDED.last_import_run_id,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        f"{audit.id}:{offer.product_id}",
                        offer.product_id,
                        retailer_provider_key,
                        offer.device_availability.value,
                        audit.observed_at,
                        offer.device_source_url,
                        audit.verification_status.value,
                        audit.id,
                        offer_position,
                        run_id,
                    ),
                )
                source_urls = dict(offer.consumable_source_urls)
                prices = dict(offer.consumable_prices_rub)
                _executemany(
                    connection,
                    """
                    INSERT INTO pde.retailer_offer_consumables(
                        audit_id, product_id, consumable_id, is_required,
                        is_covered, source_url, price_rub, dataset_position
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        (
                            audit.id,
                            offer.product_id,
                            consumable_id,
                            True,
                            consumable_id in offer.covered_consumable_ids,
                            source_urls.get(consumable_id),
                            prices.get(consumable_id),
                            consumable_position,
                        )
                        for consumable_position, consumable_id in enumerate(
                            offer.required_consumable_ids
                        )
                    ),
                )

        connection.execute(
            """
            UPDATE pde.import_runs
            SET status = 'completed', completed_at = CURRENT_TIMESTAMP,
                counts = %s::jsonb
            WHERE id = %s
            """,
            (json.dumps(counts, sort_keys=True), run_id),
        )
        connection.commit()
    except Exception as error:
        connection.rollback()
        try:
            connection.execute(
                """
                INSERT INTO pde.data_providers(
                    provider_key, provider_type, display_name, terms_review_status
                ) VALUES (
                    'golden_json', 'golden_dataset', 'Phase 0 Golden Dataset',
                    'allowed_for_pilot'
                )
                ON CONFLICT (provider_key) DO NOTHING
                """
            )
            connection.execute(
                """
                INSERT INTO pde.import_runs(
                    id, provider_key, status, completed_at, source_revision,
                    error_message
                ) VALUES (%s, 'golden_json', 'failed', CURRENT_TIMESTAMP,
                          'phase0-final', %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (run_id, str(error)[:2000]),
            )
            connection.commit()
        except Exception:
            connection.rollback()
        raise

    return ImportSummary(run_id=run_id, counts=counts)


def _rows(cursor: CursorLike) -> list[dict[str, Any]]:
    if cursor.description is None:
        return []
    columns = [getattr(item, "name", None) or item[0] for item in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _date_text(value: date | str) -> str:
    return value.isoformat() if isinstance(value, date) else value


def load_catalog_from_postgres(connection: ConnectionLike) -> Catalog:
    product_rows = _rows(
        connection.execute(
            """
            SELECT id, manufacturer, model, mpn, product_type, print_technology,
                   color_mode, wifi, auto_duplex, recommended_monthly_volume,
                   expected_consumable_channels, maintenance_data_status, status
            FROM pde.products ORDER BY dataset_position, id
            """
        )
    )
    consumable_rows = _rows(
        connection.execute(
            """
            SELECT id, manufacturer, part_number, kind, color, yield_value,
                   yield_unit, yield_standard, is_oem
            FROM pde.consumables ORDER BY dataset_position, id
            """
        )
    )
    link_rows = _rows(
        connection.execute(
            """
            SELECT id, product_id, consumable_id, role, channel, page_scope,
                   quantity_in_box, mono_page_weight, color_page_weight,
                   installed_yield_value, notes
            FROM pde.product_consumables ORDER BY dataset_position, id
            """
        )
    )
    price_rows = _rows(
        connection.execute(
            """
            SELECT id, entity_type, entity_id, price_rub,
                   source_evidence_id, observed_at, is_primary
            FROM pde.price_observations ORDER BY dataset_position, id
            """
        )
    )
    evidence_rows = _rows(
        connection.execute(
            """
            SELECT id, entity_type, entity_id, field_name, source_type,
                   source_name, source_url, observed_at, verification_status, notes
            FROM pde.evidence ORDER BY dataset_position, id
            """
        )
    )

    return Catalog(
        products=tuple(
            Product(
                id=row["id"],
                manufacturer=row["manufacturer"],
                model=row["model"],
                mpn=row["mpn"],
                product_type=ProductType(row["product_type"]),
                print_technology=row["print_technology"],
                color_mode=ColorMode(row["color_mode"]),
                wifi=row["wifi"],
                auto_duplex=row["auto_duplex"],
                recommended_monthly_volume=row["recommended_monthly_volume"],
                expected_consumable_channels=tuple(row["expected_consumable_channels"]),
                maintenance_data_status=MaintenanceDataStatus(
                    row["maintenance_data_status"]
                ),
                status=row["status"],
            )
            for row in product_rows
        ),
        consumables=tuple(
            Consumable(
                id=row["id"],
                manufacturer=row["manufacturer"],
                part_number=row["part_number"],
                kind=ConsumableKind(row["kind"]),
                color=row["color"],
                yield_value=row["yield_value"],
                yield_unit=row["yield_unit"],
                yield_standard=row["yield_standard"],
                is_oem=row["is_oem"],
            )
            for row in consumable_rows
        ),
        product_consumables=tuple(
            ProductConsumable(
                id=row["id"],
                product_id=row["product_id"],
                consumable_id=row["consumable_id"],
                role=ProductConsumableRole(row["role"]),
                channel=row["channel"],
                page_scope=PageScope(row["page_scope"]),
                quantity_in_box=row["quantity_in_box"],
                mono_page_weight=row["mono_page_weight"],
                color_page_weight=row["color_page_weight"],
                installed_yield_value=row["installed_yield_value"],
                notes=row["notes"],
            )
            for row in link_rows
        ),
        prices=tuple(
            PriceObservation(
                id=row["id"],
                entity_type=row["entity_type"],
                entity_id=row["entity_id"],
                price_rub=row["price_rub"],
                source_id=row["source_evidence_id"],
                observed_at=_date_text(row["observed_at"]),
                is_primary=row["is_primary"],
            )
            for row in price_rows
        ),
        evidence=tuple(
            Evidence(
                id=row["id"],
                entity_type=row["entity_type"],
                entity_id=row["entity_id"],
                field_name=row["field_name"],
                source_type=row["source_type"],
                source_name=row["source_name"],
                source_url=row["source_url"],
                observed_at=_date_text(row["observed_at"]),
                verification_status=VerificationStatus(row["verification_status"]),
                notes=row["notes"],
            )
            for row in evidence_rows
        ),
    )


def load_scenarios_from_postgres(connection: ConnectionLike) -> tuple[UsageScenario, ...]:
    rows = _rows(
        connection.execute(
            """
            SELECT id, name, mono_pages_per_month, color_pages_per_month,
                   ownership_months, max_purchase_price_rub, require_mfp,
                   require_wifi, require_auto_duplex
            FROM pde.usage_scenarios ORDER BY dataset_position, id
            """
        )
    )
    return tuple(UsageScenario(**row) for row in rows)


def load_retailer_basket_audits_from_postgres(
    connection: ConnectionLike,
) -> tuple[RetailerBasketAudit, ...]:
    audits = _rows(
        connection.execute(
            """
            SELECT id, retailer, observed_at, source_type, verification_status,
                   scenario_id
            FROM pde.retailer_basket_audits ORDER BY dataset_position, id
            """
        )
    )
    result: list[RetailerBasketAudit] = []
    for audit in audits:
        offers = _rows(
            connection.execute(
                """
                SELECT product_id, device_availability, device_price_rub,
                       device_source_url, notes
                FROM pde.retailer_product_offers
                WHERE audit_id = %s
                ORDER BY dataset_position, product_id
                """,
                (audit["id"],),
            )
        )
        product_offers: list[RetailerProductCoverage] = []
        for offer in offers:
            consumables = _rows(
                connection.execute(
                    """
                    SELECT consumable_id, is_required, is_covered, source_url,
                           price_rub
                    FROM pde.retailer_offer_consumables
                    WHERE audit_id = %s AND product_id = %s
                    ORDER BY dataset_position, consumable_id
                    """,
                    (audit["id"], offer["product_id"]),
                )
            )
            product_offers.append(
                RetailerProductCoverage(
                    product_id=offer["product_id"],
                    device_availability=OfferAvailability(
                        offer["device_availability"]
                    ),
                    device_price_rub=offer["device_price_rub"],
                    device_source_url=offer["device_source_url"],
                    required_consumable_ids=tuple(
                        item["consumable_id"]
                        for item in consumables
                        if item["is_required"]
                    ),
                    covered_consumable_ids=tuple(
                        item["consumable_id"]
                        for item in consumables
                        if item["is_covered"]
                    ),
                    consumable_source_urls=tuple(
                        (item["consumable_id"], item["source_url"])
                        for item in consumables
                        if item["source_url"] is not None
                    ),
                    consumable_prices_rub=tuple(
                        (item["consumable_id"], item["price_rub"])
                        for item in consumables
                        if item["price_rub"] is not None
                    ),
                    notes=offer["notes"],
                )
            )
        result.append(
            RetailerBasketAudit(
                id=audit["id"],
                retailer=audit["retailer"],
                observed_at=_date_text(audit["observed_at"]),
                source_type=audit["source_type"],
                verification_status=VerificationStatus(
                    audit["verification_status"]
                ),
                scenario_id=audit["scenario_id"],
                offers=tuple(product_offers),
            )
        )
    return tuple(result)


def database_entity_counts(connection: ConnectionLike) -> dict[str, int]:
    tables = (
        "products",
        "consumables",
        "product_consumables",
        "evidence",
        "price_observations",
        "usage_scenarios",
        "retailer_basket_audits",
        "retailer_product_offers",
        "retailer_offer_consumables",
        "availability_observations",
    )
    return {
        table: int(connection.execute(f"SELECT COUNT(*) FROM pde.{table}").fetchone()[0])
        for table in tables
    }


def database_quality_baseline(connection: ConnectionLike) -> dict[str, int | str]:
    from product_decision_engine.evidence import audit_product

    catalog = load_catalog_from_postgres(connection)
    audits = load_retailer_basket_audits_from_postgres(connection)

    def scalar(query: str) -> Any:
        row = connection.execute(query).fetchone()
        if row is None:
            raise RuntimeError("Expected a scalar query result")
        return row[0]

    observed_at = connection.execute(
        "SELECT MIN(observed_at), MAX(observed_at) FROM pde.price_observations"
    ).fetchone()
    assert observed_at is not None
    database_today = scalar("SELECT CURRENT_DATE")
    stale_primary_prices = _rows(
        connection.execute(
            """
            SELECT entity_type, entity_id, observed_at
            FROM pde.price_observations
            WHERE is_primary AND observed_at < CURRENT_DATE - 30
            ORDER BY entity_type, entity_id
            """
        )
    )
    price_entities = int(
        scalar(
            """
            SELECT COUNT(*) FROM (
                SELECT entity_type, entity_id
                FROM pde.price_observations
                GROUP BY entity_type, entity_id
            ) AS entities
            """
        )
    )
    repeated_price_entities = int(
        scalar(
            """
            SELECT COUNT(*) FROM (
                SELECT entity_type, entity_id
                FROM pde.price_observations
                GROUP BY entity_type, entity_id
                HAVING COUNT(*) >= 2
            ) AS entities
            """
        )
    )
    product_audits = tuple(audit_product(catalog, product) for product in catalog.products)
    return {
        "products": len(catalog.products),
        "active_status_products": sum(item.status == "active" for item in catalog.products),
        "products_with_complete_critical_facts": sum(
            not item.missing for item in product_audits
        ),
        "products_with_conflicts": sum(bool(item.conflicts) for item in product_audits),
        "products_missing_recommended_volume": sum(
            item.recommended_monthly_volume is None for item in catalog.products
        ),
        "price_entities": price_entities,
        "repeated_price_entities": repeated_price_entities,
        "primary_price_entities": int(
            scalar("SELECT COUNT(*) FROM pde.price_observations WHERE is_primary")
        ),
        "fresh_primary_price_entities_30d": int(
            scalar(
                """
                SELECT COUNT(*) FROM pde.price_observations
                WHERE is_primary AND observed_at >= CURRENT_DATE - 30
                """
            )
        ),
        "stale_primary_price_entities_30d": len(stale_primary_prices),
        "stale_primary_price_entity_ids_30d": ", ".join(
            item["entity_id"] for item in stale_primary_prices
        ),
        "database_as_of": _date_text(database_today),
        "price_observed_at_min": _date_text(observed_at[0]),
        "price_observed_at_max": _date_text(observed_at[1]),
        "products_with_availability_observation": int(
            scalar(
                "SELECT COUNT(DISTINCT product_id) FROM pde.availability_observations"
            )
        ),
        "products_with_in_stock_observation": int(
            scalar(
                """
                SELECT COUNT(DISTINCT product_id)
                FROM pde.availability_observations
                WHERE availability = 'in_stock'
                  AND verification_status = 'verified'
                """
            )
        ),
        "retailer_basket_audits": len(audits),
        "complete_retailer_basket_audits": sum(item.complete for item in audits),
        "retailer_providers": int(
            scalar(
                "SELECT COUNT(*) FROM pde.data_providers WHERE provider_type = 'retailer'"
            )
        ),
    }


def verify_golden_parity(connection: ConnectionLike, data_dir: Path) -> ParityResult:
    from product_decision_engine.evaluation.report import build_report

    json_catalog = load_catalog(data_dir)
    json_scenarios = load_scenarios(data_dir / "scenarios.json")
    json_audits = load_retailer_basket_audits(
        data_dir / "retailer_basket_audits.json"
    )
    db_catalog = load_catalog_from_postgres(connection)
    db_scenarios = load_scenarios_from_postgres(connection)
    db_audits = load_retailer_basket_audits_from_postgres(connection)
    return ParityResult(
        catalog_equal=json_catalog == db_catalog,
        scenarios_equal=json_scenarios == db_scenarios,
        retailer_basket_audits_equal=json_audits == db_audits,
        report_equal=build_report(json_catalog, json_scenarios, json_audits)
        == build_report(db_catalog, db_scenarios, db_audits),
    )
