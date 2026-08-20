from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from product_decision_engine.persistence import (  # noqa: E402
    apply_migrations,
    connect_postgres,
    database_entity_counts,
    database_quality_baseline,
    import_golden_dataset,
    verify_golden_parity,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import Phase 0 Golden Dataset into PostgreSQL and verify parity."
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("PDE_DATABASE_URL"),
        help="PostgreSQL URL; defaults to PDE_DATABASE_URL.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=PROJECT_ROOT / "data" / "golden",
    )
    parser.add_argument(
        "--migrations-dir",
        type=Path,
        default=PROJECT_ROOT / "db" / "migrations",
    )
    parser.add_argument(
        "--report-path",
        type=Path,
        default=PROJECT_ROOT / "reports" / "generated" / "phase1-bootstrap-report.md",
    )
    return parser


def build_report(
    *,
    applied: tuple[str, ...],
    schema_versions: tuple[str, ...],
    first_counts: dict[str, int],
    second_counts: dict[str, int],
    parity: object,
    quality: dict[str, int | str],
) -> str:
    counts_stable = first_counts == second_counts
    parity_ok = bool(getattr(parity, "ok"))
    m1_passed = counts_stable and parity_ok
    lines = [
        "# Отчёт PostgreSQL bootstrap Фазы 1",
        "",
        "> Отчёт сформирован кодом после двух импортов Golden Dataset.",
        "",
        "## Результат M1",
        "",
        f"- Версии схемы: `{', '.join(schema_versions)}`",
        f"- Новые миграции в этом запуске: `{', '.join(applied) if applied else 'нет'}`",
        f"- Количество строк после повторного импорта стабильно: **{counts_stable}**",
        f"- Паритет каталога: **{getattr(parity, 'catalog_equal')}**",
        f"- Паритет сценариев: **{getattr(parity, 'scenarios_equal')}**",
        "- Паритет retailer basket audits: "
        f"**{getattr(parity, 'retailer_basket_audits_equal')}**",
        f"- Побайтовый паритет отчёта Фазы 0: **{getattr(parity, 'report_equal')}**",
        f"- Gate M1: **{'PASS' if m1_passed else 'FAIL'}**",
        "",
        "## Перенесённые сущности",
        "",
    ]
    for name, count in first_counts.items():
        lines.append(f"- `{name}`: {count}")
    lines.extend(
        [
            "",
            "## Исходная готовность данных к актуализации",
            "",
            f"- Полные критические facts: {quality['products_with_complete_critical_facts']} / {quality['products']} моделей",
            f"- Модели с конфликтами evidence: {quality['products_with_conflicts']} / {quality['products']}",
            f"- Не опубликован recommended volume: {quality['products_missing_recommended_volume']} / {quality['products']}",
            f"- Повторные цены: {quality['repeated_price_entities']} / {quality['price_entities']} оцениваемых сущностей",
            f"- Диапазон дат цен: {quality['price_observed_at_min']} — {quality['price_observed_at_max']}",
            f"- Свежие primary prices (не старше 30 дней на {quality['database_as_of']}): {quality['fresh_primary_price_entities_30d']} / {quality['primary_price_entities']}",
            f"- Просроченные primary prices: {quality['stale_primary_price_entities_30d']} — {quality['stale_primary_price_entity_ids_30d'] or 'нет'}",
            f"- Есть хотя бы одно наблюдение наличия: {quality['products_with_availability_observation']} / {quality['products']} моделей",
            f"- Есть verified `in_stock`: {quality['products_with_in_stock_observation']} / {quality['products']} моделей",
            f"- Полные retailer baskets: {quality['complete_retailer_basket_audits']} / {quality['retailer_basket_audits']}",
            f"- Представлено retailer-провайдеров: {quality['retailer_providers']}",
            "",
            "## Вывод",
            "",
        ]
    )
    if not m1_passed:
        lines.append(
            "**`STOP / FIX M1`**. До устранения потери данных или вычислительного дрейфа продолжать автоматизацию нельзя."
        )
    else:
        lines.extend(
            [
                "**`CONTINUE PHASE 1 / M2`**. PostgreSQL-перенос не изменил ни одну расчётную сущность или рекомендацию и не создаёт дубликаты при повторном запуске.",
                "",
                "Это не `GO` к web MVP. Текущее поле `status=active` есть у исходного каталога, но фактическое наличие наблюдалось только для части моделей. Полных одновременных корзин пока 4 из 7, а повторными ценами покрыта меньшая часть оцениваемых сущностей. Следующая решающая проверка — freshness/lifecycle и три повторных обновления когорты с измерением ручного труда.",
            ]
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    args = build_parser().parse_args()
    if not args.database_url:
        raise SystemExit("Укажите --database-url или переменную PDE_DATABASE_URL.")

    connection = connect_postgres(args.database_url)
    try:
        applied = apply_migrations(connection, args.migrations_dir)
        first = import_golden_dataset(connection, args.data_dir)
        counts_after_first = database_entity_counts(connection)
        second = import_golden_dataset(connection, args.data_dir)
        counts_after_second = database_entity_counts(connection)
        parity = verify_golden_parity(connection, args.data_dir)
        quality = database_quality_baseline(connection)
    finally:
        connection.close()

    report = build_report(
        applied=applied,
        schema_versions=tuple(
            path.stem for path in sorted(args.migrations_dir.glob("*.sql"))
        ),
        first_counts=counts_after_first,
        second_counts=counts_after_second,
        parity=parity,
        quality=quality,
    )
    args.report_path.parent.mkdir(parents=True, exist_ok=True)
    args.report_path.write_text(report, encoding="utf-8")

    print(f"Применены миграции: {', '.join(applied) if applied else 'нет новых'}")
    print(f"Первый import run: {first.run_id}")
    print(f"Повторный import run: {second.run_id}")
    print(f"Количество строк стабильно: {counts_after_first == counts_after_second}")
    print(f"Паритет каталога: {parity.catalog_equal}")
    print(f"Паритет сценариев: {parity.scenarios_equal}")
    print(f"Паритет retailer audits: {parity.retailer_basket_audits_equal}")
    print(f"Побайтовый паритет отчёта Фазы 0: {parity.report_equal}")
    print(f"Отчёт: {args.report_path}")
    if counts_after_first != counts_after_second or not parity.ok:
        print("Результат M1: FAIL")
        return 1
    print("Результат M1: PASS — CONTINUE PHASE 1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
