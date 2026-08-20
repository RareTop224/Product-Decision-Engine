from __future__ import annotations

import json
from pathlib import Path

from product_decision_engine.dataio import (
    load_catalog,
    load_retailer_basket_audits,
    load_scenarios,
)
from product_decision_engine.evaluation.phase1_market import (
    build_phase1_market_report,
)


def main() -> None:
    project_root = Path.cwd()
    golden_dir = project_root / "data" / "golden"
    phase1_dir = project_root / "data" / "phase1"
    with (phase1_dir / "cohort.json").open("r", encoding="utf-8") as stream:
        cohort = json.load(stream)
    with (phase1_dir / "source_snapshot_2026-08-20.json").open(
        "r", encoding="utf-8"
    ) as stream:
        snapshot = json.load(stream)
    report = build_phase1_market_report(
        load_catalog(golden_dir),
        load_scenarios(golden_dir / "scenarios.json"),
        cohort,
        snapshot,
        load_retailer_basket_audits(golden_dir / "retailer_basket_audits.json"),
        load_retailer_basket_audits(
            phase1_dir / "price_ru_basket_audits.json"
        ),
    )
    output = project_root / "reports" / "generated" / "phase1-market-report.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report, encoding="utf-8")
    print(f"Отчёт записан: {output}")
