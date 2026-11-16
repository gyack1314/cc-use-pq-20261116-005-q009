from __future__ import annotations

from climateledger.registry import ContractError
from support import LedgerCase


class OperationsTests(LedgerCase):
    def test_review_queue_only_latest_revision(self):
        first, _ = self.ingest("gap", self.rows(428) + self.rows(430, start=3))
        second, _ = self.ingest("gap", self.rows(429) + self.rows(431, start=3))
        pending = self.service.pending_review()
        self.assertEqual([item["batch_id"] for item in pending], [second["id"]])
        self.assertNotEqual(first["id"], second["id"])

    def test_review_queue_source_filter(self):
        self.ingest("gap_a", self.rows(428) + self.rows(430, start=3))
        self.ingest("gap_b", self.rows(427) + self.rows(429, start=3), source="lab_b")
        self.assertEqual(len(self.service.pending_review()), 2)
        self.assertEqual(self.service.pending_review("lab_a")[0]["source"], "lab_a")

    def test_rejected_queue_has_blocking_findings(self):
        batch, _ = self.ingest("bad", self.rows(1001))
        rejected = self.service.rejected_batches()
        self.assertEqual(rejected[0]["batch_id"], batch["id"])
        self.assertTrue(all(item["severity"] == "block" for item in rejected[0]["blocking"]))

    def test_batch_history_retains_revisions(self):
        first, _ = self.ingest("same_id", self.rows(428))
        second, _ = self.ingest("same_id", self.rows(429))
        history = self.service.batch_history("lab_a", "same_id")
        self.assertEqual([row["revision"] for row in history], [1, 2])
        self.assertEqual([row["batch_id"] for row in history], [first["id"], second["id"]])

    def test_batch_history_unknown(self):
        with self.assertRaisesRegex(ContractError, "unknown_batch"):
            self.service.batch_history("lab_a", "missing")

    def test_source_receipt_counts_current_rows(self):
        self.release()
        receipt = self.service.receipts.source_summary("lab_a")
        self.assertEqual(receipt["batch_count"], 1)
        self.assertEqual(receipt["current_measurements"], 2)
        self.assertEqual(receipt["latest_release"], 1)

    def test_indicator_receipt_uses_published_snapshot(self):
        self.release()
        receipt = self.service.receipts.indicator_summary("co2_ppm")
        self.assertEqual(receipt["measurement_count"], 2)
        self.assertEqual(receipt["first_period"], "2026-01")
        self.assertEqual(receipt["last_period"], "2026-02")

    def test_batch_receipt_preserves_hash(self):
        batch, _ = self.ingest("receipt", self.rows(428))
        receipt = self.service.receipts.batch_receipt(batch["id"])
        self.assertEqual(receipt["payload_hash"], batch["payload_hash"])
        self.assertEqual(receipt["accepted_rows"], 1)

    def test_release_receipt_has_parent_digest(self):
        self.release()
        later, _ = self.ingest("later", self.rows(430, start=3))
        self.service.publish(later["id"], 1)
        receipt = self.service.receipts.release_receipt(2)
        self.assertEqual(receipt["parent_digest"], self.service.export(1)["digest"])

    def test_status_counts_buckets(self):
        self.ingest("clean", self.rows(428))
        self.ingest("gap", self.rows(428) + self.rows(430, start=3))
        self.ingest("bad", self.rows(1001))
        status = self.service.status()
        self.assertEqual(status["batches"], 3)
        self.assertEqual(status["by_status"]["accepted"], 1)
        self.assertEqual(status["by_status"]["review"], 1)
        self.assertEqual(status["by_status"]["rejected"], 1)

    def test_audit_event_order(self):
        batch, release, _ = self.release()
        events = self.service.events(f"batch:{batch['id']}")
        self.assertEqual([event["kind"] for event in events], ["ingest", "publish"])
        self.assertEqual(events[-1]["detail"]["version"], release["version"])

    def test_audit_all_verifies_release(self):
        self.release()
        report = self.service.audit_all()
        self.assertEqual(report["batch_count"], 1)
        self.assertEqual(report["release_count"], 1)
        self.assertEqual(report["violations"], [])

    def test_audit_detects_modified_measurement(self):
        batch, _, _ = self.release()
        self.service.store.connection.execute("UPDATE measurements SET value='999.00' WHERE batch_id=? AND line=1", (batch["id"],))
        report = self.service.audit.release_trace(1)
        self.assertFalse(report["verified"])
        self.assertEqual(report["broken_entries"][0]["reason"], "measurement_mismatch")

    def test_batch_coverage_reports_gap(self):
        batch, _ = self.ingest("gap", self.rows(428) + self.rows(430, start=3))
        report = self.service.batch_coverage_report(batch["id"])
        self.assertEqual(report["series"][0]["missing"], ["2026-02"])
        self.assertEqual(report["declared_rows"], 2)

    def test_release_coverage_reports_gap(self):
        batch, _ = self.ingest("gap", self.rows(428) + self.rows(430, start=3))
        self.service.approve(batch["id"], 1)
        self.service.publish(batch["id"], 1)
        report = self.service.coverage(1)
        self.assertEqual(report["missing"], 1)
        self.assertEqual(report["series"][0]["missing"], ["2026-02"])

    def test_discrepancy_report_between_sources(self):
        self.release()
        other, _ = self.ingest("other", self.rows(427), source="lab_b")
        self.service.publish(other["id"], 1)
        report = self.service.discrepancies(2, "co2_ppm")
        self.assertEqual(report["compared_periods"], 1)
        self.assertEqual(report["disagreements"][0]["spread"], "1.00")

    def test_replay_matches_original_manifest(self):
        batch, manifest = self.ingest("replay", self.rows(428, 429))
        result = self.service.replay(manifest, batch["id"])
        self.assertTrue(result["reproducible"])
        self.assertEqual(result["stored_rows"], 2)

    def test_replay_rejects_wrong_identity(self):
        batch, _ = self.ingest("original", self.rows(428))
        other = self.manifest("different", self.rows(428))
        with self.assertRaisesRegex(ContractError, "replay_identity"):
            self.service.replay(other, batch["id"])
