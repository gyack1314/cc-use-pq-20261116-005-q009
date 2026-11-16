from __future__ import annotations

from copy import deepcopy

from climateledger.registry import ContractError
from support import LedgerCase


class ReleaseTests(LedgerCase):
    def test_publish_accepted_batch(self):
        batch, release, _ = self.release()
        self.assertEqual(release["version"], 1)
        self.assertEqual(self.service.batch(batch["id"])["status"], "published")
        self.assertEqual(len(self.service.export(1)["entries"]), 2)

    def test_publish_is_idempotent(self):
        batch, release, _ = self.release()
        again = self.service.publish(batch["id"], batch["revision"])
        self.assertEqual(again["digest"], release["digest"])
        self.assertEqual(self.service.status()["latest_release"], 1)

    def test_preview_does_not_write_release(self):
        batch, _ = self.ingest("candidate", self.rows(428))
        preview = self.service.preview(batch["id"], batch["revision"])
        self.assertEqual(preview["version"], 1)
        self.assertEqual(self.service.status()["latest_release"], None)

    def test_rejected_batch_cannot_publish(self):
        batch, _ = self.ingest("bad", self.rows(1001))
        with self.assertRaisesRegex(ContractError, "quality_gate"):
            self.service.publish(batch["id"], 1)
        self.assertEqual(self.service.status()["latest_release"], None)

    def test_review_batch_requires_approval(self):
        rows = self.rows(428) + self.rows(430, start=3)
        batch, _ = self.ingest("gap", rows)
        with self.assertRaisesRegex(ContractError, "quality_gate"):
            self.service.publish(batch["id"], 1)
        approved = self.service.approve(batch["id"], 1)
        self.assertEqual(approved["status"], "approved")
        published = self.service.publish(batch["id"], 1)
        self.assertEqual(published["version"], 1)
        self.assertEqual(self.service.export(1)["manifest"]["quality_findings"][0]["code"], "missing_period")

    def test_approval_is_idempotent(self):
        rows = self.rows(428) + self.rows(430, start=3)
        batch, _ = self.ingest("gap", rows)
        first = self.service.approve(batch["id"], 1)
        second = self.service.approve(batch["id"], 1)
        self.assertEqual(first["approved_at"], second["approved_at"])

    def test_rejected_batch_cannot_be_approved(self):
        batch, _ = self.ingest("bad", self.rows(1001))
        with self.assertRaisesRegex(ContractError, "not_reviewable"):
            self.service.approve(batch["id"], 1)

    def test_wrong_revision_cannot_publish(self):
        batch, _ = self.ingest("candidate", self.rows(428))
        with self.assertRaisesRegex(ContractError, "stale_revision"):
            self.service.publish(batch["id"], 2)

    def test_superseded_batch_cannot_publish(self):
        old, _ = self.ingest("candidate", self.rows(428))
        newer, _ = self.ingest("candidate", self.rows(429))
        self.assertEqual(newer["revision"], 2)
        with self.assertRaisesRegex(ContractError, "stale_revision"):
            self.service.publish(old["id"], 1)

    def test_release_chain_is_verifiable(self):
        first_batch, first, _ = self.release()
        second_batch, _ = self.ingest("later", self.rows(430, start=3))
        second = self.service.publish(second_batch["id"], 1)
        self.assertEqual(second["manifest"]["parent_digest"], first["digest"])
        self.assertEqual([row["version"] for row in self.service.lineage(2)], [2, 1])
        self.assertEqual(len(self.service.export(2)["entries"]), 3)

    def test_export_verify_detects_entry_tampering(self):
        self.release()
        bundle = self.service.export(1)
        modified = deepcopy(bundle)
        modified["entries"][0]["value"] = "999.00"
        self.assertFalse(self.service.verify(modified))
        self.assertTrue(self.service.verify(bundle))

    def test_export_verify_detects_manifest_tampering(self):
        self.release()
        bundle = self.service.export(1)
        bundle["manifest"]["entry_count"] = 3
        self.assertFalse(self.service.verify(bundle))

    def test_releases_keep_distinct_sources(self):
        self.release()
        other, _ = self.ingest("other", self.rows(427), source="lab_b")
        second = self.service.publish(other["id"], 1)
        rows = self.service.export(second["version"])["entries"]
        self.assertEqual({row["source"] for row in rows}, {"lab_a", "lab_b"})
        self.assertEqual(len([row for row in rows if row["period"] == "2026-01"]), 2)

    def test_revision_replaces_current_key_but_not_history(self):
        first_batch, first, _ = self.release()
        new_batch, _ = self.ingest("correction", self.rows(430))
        self.assertEqual(new_batch["status"], "review")
        self.service.approve(new_batch["id"], 1)
        second = self.service.publish(new_batch["id"], 1)
        self.assertEqual(self.service.export(1)["entries"][0]["value"], "428.00")
        self.assertEqual(self.service.export(2)["entries"][0]["value"], "430.00")
        self.assertEqual(second["version"], 2)

    def test_series_respects_release_version(self):
        self.release()
        later, _ = self.ingest("later", self.rows(430, start=3))
        self.service.publish(later["id"], 1)
        self.assertEqual(len(self.service.series("co2_ppm", 1)), 2)
        self.assertEqual(len(self.service.series("co2_ppm", 2)), 3)

    def test_impact_marks_reaffirmed_and_revised(self):
        self.release()
        batch, _ = self.ingest("mixed", self.rows(428, 430))
        impact = self.service.impact(batch["id"])
        self.assertEqual([row["effect"] for row in impact["touched"]], ["reaffirmed", "revised"])

    def test_compare_release_reports_changes(self):
        self.release()
        batch, _ = self.ingest("later", self.rows(430, start=3))
        self.service.publish(batch["id"], 1)
        diff = self.service.compare(1, 2)
        self.assertEqual(diff["summary"]["by_kind"]["added"], 1)
        self.assertEqual(diff["changes"][0]["period"], "2026-03")

    def test_compare_rejects_reverse_order(self):
        self.release()
        with self.assertRaisesRegex(ContractError, "version_order"):
            self.service.compare(1, 1)
