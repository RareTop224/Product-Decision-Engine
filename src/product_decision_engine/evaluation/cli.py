from __future__ import annotations

import argparse
from pathlib import Path

from product_decision_engine.dataio import (
    load_catalog,
    load_retailer_basket_audits,
    load_scenarios,
)
from product_decision_engine.evaluation.report import write_report


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate Phase 0 Golden Dataset")
    parser.add_argument("--data-dir", type=Path, default=Path("data/golden"))
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("reports/generated/phase0-report.md"),
    )
    args = parser.parse_args()

    catalog = load_catalog(args.data_dir)
    scenarios = load_scenarios(args.data_dir / "scenarios.json")
    basket_audits = load_retailer_basket_audits(
        args.data_dir / "retailer_basket_audits.json"
    )
    write_report(catalog, scenarios, args.report, basket_audits)
    print(f"Wrote {args.report} for {len(catalog.products)} products and {len(scenarios)} scenarios")


if __name__ == "__main__":
    main()
