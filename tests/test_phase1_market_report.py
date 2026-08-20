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
    analyze_currency_reconciliation,
    analyze_provider_source_audit,
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
        with (phase1_dir / "provider_source_audit_2026-08-21.json").open(
            "r", encoding="utf-8"
        ) as stream:
            provider_audit = json.load(stream)

        report = build_phase1_market_report(
            load_catalog(golden_dir),
            load_scenarios(golden_dir / "scenarios.json"),
            cohort,
            snapshot,
            provider_audit,
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
        self.assertIn("**8 / 12 (66,7%)**", report)
        self.assertIn("**38 / 38** обязательных позиций", report)
        self.assertIn("KNS — основной кандидат", report)
        self.assertIn("не считается вторым M3-срезом", report)
        self.assertIn("Epson EcoTank L4260", report)
        self.assertIn("**4 / 6** доступных устройств", report)
        self.assertIn("исходящий affiliate XML", report)
        self.assertIn("Неподтверждённых обязательных полей: **9**", report)

    def test_no_checked_provider_is_production_ready(self) -> None:
        phase1_dir = PROJECT_ROOT / "data" / "phase1"
        with (phase1_dir / "cohort.json").open("r", encoding="utf-8") as stream:
            cohort = json.load(stream)
        with (phase1_dir / "provider_source_audit_2026-08-21.json").open(
            "r", encoding="utf-8"
        ) as stream:
            provider_audit = json.load(stream)

        summaries = analyze_provider_source_audit(
            provider_audit,
            tuple(item["product_id"] for item in cohort),
        )

        self.assertEqual(4, len(summaries))
        self.assertFalse(any(item.production_ready for item in summaries))
        kns = next(item for item in summaries if item.provider_key == "kns")
        self.assertEqual(3, kns.passed_gates)
        self.assertEqual(7, kns.required_gates)

    def test_kns_cbr_conversion_is_not_universally_reproducible(self) -> None:
        phase1_dir = PROJECT_ROOT / "data" / "phase1"
        with (phase1_dir / "cohort.json").open("r", encoding="utf-8") as stream:
            cohort = json.load(stream)
        with (phase1_dir / "provider_source_audit_2026-08-21.json").open(
            "r", encoding="utf-8"
        ) as stream:
            provider_audit = json.load(stream)
        kns = next(
            item for item in provider_audit["providers"]
            if item["provider_key"] == "kns"
        )

        summary = analyze_currency_reconciliation(
            kns["currency_reconciliation"],
            tuple(item["product_id"] for item in cohort),
        )

        self.assertEqual(6, summary.sample_count)
        self.assertEqual(4, summary.within_tolerance_count)
        self.assertFalse(summary.universally_reproducible)
        self.assertGreater(summary.maximum_error_percent, 12)
        self.assertLess(summary.maximum_error_percent, 13)


if __name__ == "__main__":
    unittest.main()
