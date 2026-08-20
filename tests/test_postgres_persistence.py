from __future__ import annotations

import os
import unittest
from pathlib import Path

from product_decision_engine.persistence import (
    apply_migrations,
    connect_postgres,
    database_entity_counts,
    import_golden_dataset,
    verify_golden_parity,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class PostgresSchemaTests(unittest.TestCase):
    def test_initial_migration_contains_phase1_data_boundaries(self) -> None:
        migration = (
            PROJECT_ROOT / "db" / "migrations" / "001_initial.sql"
        ).read_text(encoding="utf-8")

        for table in (
            "data_providers",
            "import_runs",
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
        ):
            with self.subTest(table=table):
                self.assertIn(f"pde.{table}", migration)
        self.assertIn("latest_product_availability", migration)
        self.assertIn("one_primary_price_per_entity_idx", migration)
        self.assertIn("validate_price_observation_trigger", migration)

        identity_migration = (
            PROJECT_ROOT / "db" / "migrations" / "002_product_identity.sql"
        ).read_text(encoding="utf-8")
        self.assertIn("unique_product_mpn_idx", identity_migration)
        self.assertIn("WHERE mpn IS NOT NULL", identity_migration)

        freshness_migration = (
            PROJECT_ROOT / "db" / "migrations" / "003_market_freshness.sql"
        ).read_text(encoding="utf-8")
        self.assertIn("pde.product_lifecycle_observations", freshness_migration)
        self.assertIn("pde.latest_product_lifecycle", freshness_migration)


@unittest.skipUnless(
    os.environ.get("PDE_TEST_DATABASE_URL"),
    "PDE_TEST_DATABASE_URL is required for PostgreSQL integration test",
)
class PostgresIntegrationTests(unittest.TestCase):
    def test_golden_import_is_idempotent_and_report_has_exact_parity(self) -> None:
        database_url = os.environ["PDE_TEST_DATABASE_URL"]
        connection = connect_postgres(database_url)
        try:
            apply_migrations(connection, PROJECT_ROOT / "db" / "migrations")
            first = import_golden_dataset(
                connection, PROJECT_ROOT / "data" / "golden"
            )
            first_counts = database_entity_counts(connection)
            second = import_golden_dataset(
                connection, PROJECT_ROOT / "data" / "golden"
            )
            second_counts = database_entity_counts(connection)
            parity = verify_golden_parity(
                connection, PROJECT_ROOT / "data" / "golden"
            )
            invalid_price_rejected = False
            try:
                connection.execute(
                    """
                    INSERT INTO pde.evidence(
                        id, entity_type, entity_id, field_name, source_type,
                        source_name, source_url, observed_at,
                        verification_status, dataset_position
                    ) VALUES (
                        'invalid-price-evidence', 'price_observation',
                        'invalid-price', 'price_rub', 'fixture', 'Fixture',
                        'fixture://invalid', CURRENT_DATE, 'verified', 999999
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO pde.price_observations(
                        id, entity_type, entity_id, price_rub,
                        source_evidence_id, observed_at, is_primary,
                        dataset_position
                    ) VALUES (
                        'invalid-price', 'product', 'unknown-product', 1,
                        'invalid-price-evidence', CURRENT_DATE, FALSE, 999999
                    )
                    """
                )
            except Exception:
                invalid_price_rejected = True
                connection.rollback()
            else:
                connection.rollback()
        finally:
            connection.close()

        self.assertNotEqual(first.run_id, second.run_id)
        self.assertEqual(first.counts, second.counts)
        self.assertEqual(first_counts, second_counts)
        self.assertTrue(parity.catalog_equal)
        self.assertTrue(parity.scenarios_equal)
        self.assertTrue(parity.retailer_basket_audits_equal)
        self.assertTrue(parity.report_equal)
        self.assertTrue(invalid_price_rejected)


if __name__ == "__main__":
    unittest.main()
