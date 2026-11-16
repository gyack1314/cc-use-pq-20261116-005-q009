from __future__ import annotations

from decimal import Decimal

from climateledger.normalize import normalize_period, period_index, period_from_index
from climateledger.registry import ContractError
from support import LedgerCase


class NormalizationTests(LedgerCase):
    def test_monthly_period(self):
        self.assertEqual(normalize_period("2026-01", "month"), "2026-01")
        self.assertEqual(period_from_index(period_index("2026-12", "month") + 1, "month"), "2027-01")

    def test_yearly_period(self):
        self.assertEqual(normalize_period("2026", "year"), "2026")
        self.assertEqual(period_from_index(period_index("2026", "year") + 1, "year"), "2027")

    def test_invalid_period_format(self):
        with self.assertRaisesRegex(ContractError, "period_format"):
            normalize_period("2026-1", "month")

    def test_invalid_period_year(self):
        with self.assertRaisesRegex(ContractError, "period_range"):
            normalize_period("1800-01", "month")

    def test_conversion_to_canonical_unit(self):
        batch, _ = self.ingest("convert", self.rows(428000, unit="ppb"))
        self.assertEqual(batch["status"], "accepted")
        row = self.service.batch(batch["id"])["measurements"][0]
        self.assertEqual(row["value"], "428.00")
        self.assertEqual(row["raw_value"], "428000")
        self.assertEqual(row["factor"], "0.001")

    def test_decimal_round_half_even(self):
        batch, _ = self.ingest("rounding", self.rows("428.005"))
        self.assertEqual(self.service.batch(batch["id"])["measurements"][0]["value"], "428.00")

    def test_unknown_unit_blocks_row(self):
        batch, _ = self.ingest("unit_unknown", self.rows(428, unit="degrees"))
        self.assertEqual(batch["status"], "rejected")
        self.assertEqual(batch["findings"][0]["code"], "empty_batch")
        self.assertIn("unknown_unit", {item["code"] for item in batch["findings"]})

    def test_invalid_number_blocks_row(self):
        batch, _ = self.ingest("bad_number", self.rows("not-a-number"))
        self.assertEqual(batch["status"], "rejected")
        self.assertIn("invalid_decimal", {item["code"] for item in batch["findings"]})

    def test_nonfinite_number_blocks_row(self):
        batch, _ = self.ingest("nan", self.rows("NaN"))
        self.assertEqual(batch["status"], "rejected")
        self.assertIn("invalid_decimal", {item["code"] for item in batch["findings"]})


class QualityTests(LedgerCase):
    def test_clean_batch_is_accepted(self):
        batch, _ = self.ingest("clean", self.rows(428, 429, 430))
        self.assertEqual(batch["status"], "accepted")
        self.assertEqual(batch["findings"], [])
        self.assertEqual(len(self.service.batch(batch["id"])["measurements"]), 3)

    def test_gap_requires_review(self):
        rows = self.rows(428) + self.rows(430, start=3)
        batch, _ = self.ingest("gap", rows)
        self.assertEqual(batch["status"], "review")
        self.assertEqual(batch["findings"][0]["code"], "missing_period")
        self.assertEqual(batch["findings"][0]["period"], "2026-02")

    def test_duplicate_period_blocks(self):
        batch, _ = self.ingest("duplicate", self.rows(428, 428, start=1)[:1] * 2)
        self.assertEqual(batch["status"], "rejected")
        self.assertIn("duplicate_period", {item["code"] for item in batch["findings"]})

    def test_conflicting_period_blocks(self):
        rows = self.rows(428) + self.rows(429)
        batch, _ = self.ingest("conflict", rows)
        self.assertEqual(batch["status"], "rejected")
        self.assertIn("conflicting_period", {item["code"] for item in batch["findings"]})

    def test_above_maximum_blocks(self):
        batch, _ = self.ingest("high", self.rows(1001))
        self.assertEqual(batch["status"], "rejected")
        self.assertIn("above_maximum", {item["code"] for item in batch["findings"]})

    def test_below_minimum_blocks(self):
        batch, _ = self.ingest("low", self.rows(-1))
        self.assertEqual(batch["status"], "rejected")
        self.assertIn("below_minimum", {item["code"] for item in batch["findings"]})

    def test_negative_value_blocks(self):
        batch, _ = self.ingest("negative", self.rows(-1))
        self.assertIn("negative_value", {item["code"] for item in batch["findings"]})

    def test_partial_rejection_blocks_whole_batch(self):
        batch, _ = self.ingest("partial", self.rows(428, "bad"))
        self.assertEqual(batch["status"], "rejected")
        self.assertEqual(len(self.service.batch(batch["id"])["measurements"]), 1)
        with self.assertRaisesRegex(ContractError, "quality_gate"):
            self.service.publish(batch["id"], batch["revision"])

    def test_yearly_indicator_uses_yearly_gap(self):
        batch, _ = self.ingest("year_gap", self.rows(100, indicator="fire_area", unit="ha", start=1) +
                               self.rows(110, indicator="fire_area", unit="ha", start=3))
        self.assertEqual(batch["status"], "review")
        self.assertIn("2027", [item["period"] for item in batch["findings"]])

    def test_source_scope_blocks_fire_area_from_lab_b(self):
        batch, _ = self.ingest("scope", self.rows(100, indicator="fire_area"), source="lab_b")
        self.assertEqual(batch["status"], "rejected")
        self.assertIn("scope_violation", {item["code"] for item in batch["findings"]})

    def test_revised_published_value_requires_review(self):
        self.release()
        batch, _ = self.ingest("revision", self.rows(430))
        self.assertEqual(batch["status"], "review")
        self.assertIn("revised_value", {item["code"] for item in batch["findings"]})

    def test_same_published_value_is_accepted(self):
        self.release()
        batch, _ = self.ingest("same_value", self.rows(428))
        self.assertEqual(batch["status"], "accepted")

    def test_review_findings_are_not_silently_corrected(self):
        rows = self.rows(428) + self.rows(430, start=3)
        batch, _ = self.ingest("review_gap", rows)
        self.assertEqual([item["period"] for item in self.service.batch(batch["id"])["measurements"]],
                         ["2026-01", "2026-03"])
        self.assertEqual(batch["status"], "review")
