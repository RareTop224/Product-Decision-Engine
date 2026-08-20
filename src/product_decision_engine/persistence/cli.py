from __future__ import annotations

import os
from pathlib import Path

from .postgres import (
    apply_migrations,
    connect_postgres,
    database_entity_counts,
    import_golden_dataset,
    verify_golden_parity,
)


def main() -> int:
    database_url = os.environ.get("PDE_DATABASE_URL")
    if not database_url:
        raise SystemExit("Укажите переменную PDE_DATABASE_URL.")
    project_root = Path.cwd()
    connection = connect_postgres(database_url)
    try:
        apply_migrations(connection, project_root / "db" / "migrations")
        import_golden_dataset(connection, project_root / "data" / "golden")
        first_counts = database_entity_counts(connection)
        import_golden_dataset(connection, project_root / "data" / "golden")
        second_counts = database_entity_counts(connection)
        parity = verify_golden_parity(connection, project_root / "data" / "golden")
    finally:
        connection.close()
    if first_counts != second_counts or not parity.ok:
        print("Результат M1: FAIL")
        return 1
    print("Результат M1: PASS — CONTINUE PHASE 1")
    return 0
