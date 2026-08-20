from __future__ import annotations

import json
import unittest
from pathlib import Path

from product_decision_engine.dataio import (
    load_catalog,
    load_retailer_basket_audits,
    load_scenarios,
)
from product_decision_engine.evaluation.phase1_market import (
    build_phase1_market_report,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Phase1MarketReportTests(unittest.TestCase):
    def test_first_russian_market_snapshot_is_reproducible(self) -> None:
        golden_dir = PROJECT_ROOT / "data" / "golden"
        phase1_dir = PROJECT_ROOT / "data" / "phase1"
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
            load_retailer_basket_audits(
                golden_dir / "retailer_basket_audits.json"
            ),
            load_retailer_basket_audits(
                phase1_dir / "price_ru_basket_audits.json"
            ),
        )

        self.assertIn("рынок: **Россия / Москва / RUB**", report)
        self.assertIn("**7 / 12 (58,3%)**", report)
        self.assertIn("**3 / 12 (25,0%)**", report)
        self.assertIn("**6 / 12 (50,0%)**", report)
        self.assertIn("TCO Canon PIXMA TS3640 | 45 250 ₽ | 55 620 ₽", report)
        self.assertIn("TCO HP DeskJet 2875 | 33 460 ₽ | 30 650 ₽", report)
        self.assertIn("Публичная автоматическая выгрузка без разрешения", report)
        self.assertIn("lifecycle_unknown", report)


if __name__ == "__main__":
    unittest.main()
