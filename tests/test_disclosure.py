from __future__ import annotations

from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
from hashlib import sha256
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from climateledger.cli import main
from climateledger.disclosure import REDACTIONS, CredentialViews, manifest_digest
from climateledger.intake import canonical_bytes, write_manifest
from climateledger.service import LedgerService
from support import LedgerCase


SECRET_CONTACT = "dr.chen@example.org"
SECRET_NOTE = "hand-checked against station logbook"


class DisclosureCase(LedgerCase):
    def release_with_secrets(self):
        rows = self.rows(428) + self.rows(430, start=3)
        folder = self.root / "lab_a" / "secret_batch"
        manifest = write_manifest(folder, source="lab_a", batch_id="secret_batch", rows=rows,
                                  metadata={"internal_contact": SECRET_CONTACT,
                                            "processing_note": SECRET_NOTE})
        batch = self.service.ingest(manifest)["batch"]
        self.assertEqual(batch["status"], "review")
        self.service.approve(batch["id"], 1)
        release = self.service.publish(batch["id"], 1)
        return batch, release


def walk_keys(value):
    keys = set()
    if isinstance(value, dict):
        for key, item in value.items():
            keys.add(key)
            keys |= walk_keys(item)
    elif isinstance(value, list):
        for item in value:
            keys |= walk_keys(item)
    return keys


class PublicDeliveryTests(DisclosureCase):
    def test_public_view_hides_metadata_and_clear_findings(self):
        self.release_with_secrets()
        document = self.service.delivery_view(1)
        serialized = json.dumps(document, ensure_ascii=False)
        self.assertNotIn(SECRET_CONTACT, serialized)
        self.assertNotIn(SECRET_NOTE, serialized)
        self.assertNotIn("gap within submitted series", serialized)
        keys = walk_keys(document)
        for forbidden in ("metadata", "findings", "measurements", "events",
                          "raw_value", "raw_unit", "received_at", "approved_at"):
            self.assertNotIn(forbidden, keys)
        sealed = document["manifest"]["quality_findings"]
        self.assertEqual(set(sealed), {"sealed_sha256"})
        self.assertEqual(len(sealed["sealed_sha256"]), 64)
        self.assertEqual(document["redactions"], REDACTIONS)

    def test_public_view_carries_source_batch_and_file_proofs(self):
        self.release_with_secrets()
        document = self.service.delivery_view(1)
        proofs = document["manifest"]["source_files"]
        self.assertEqual(len(proofs), 1)
        proof = proofs[0]
        self.assertEqual(proof["source"], "lab_a")
        self.assertEqual(proof["external_id"], "secret_batch")
        batch = self.service.batches()[0]
        self.assertEqual(proof["payload_hash"], batch["payload_hash"])
        referenced = {entry["batch_id"] for entry in document["entries"]}
        self.assertEqual(referenced, {proof["batch_id"]})

    def test_partner_verifies_delivery(self):
        self.release_with_secrets()
        document = self.service.delivery_view(1)
        self.assertTrue(self.service.verify_view(document))
        self.assertTrue(CredentialViews.verify_public(document))

    def test_partner_detects_value_swap(self):
        self.release_with_secrets()
        document = self.service.delivery_view(1)
        tampered = deepcopy(document)
        tampered["entries"][0]["value"] = "999.00"
        self.assertFalse(self.service.verify_view(tampered))

    def test_partner_detects_version_swap(self):
        self.release_with_secrets()
        document = self.service.delivery_view(1)
        tampered = deepcopy(document)
        tampered["version"] = 7
        tampered["manifest"]["version"] = 7
        self.assertFalse(self.service.verify_view(tampered))

    def test_partner_detects_missing_file_proof(self):
        self.release_with_secrets()
        document = self.service.delivery_view(1)
        tampered = deepcopy(document)
        tampered["manifest"]["source_files"] = []
        self.assertFalse(self.service.verify_view(tampered))

    def test_partner_rejects_unsealed_findings(self):
        self.release_with_secrets()
        document = self.service.delivery_view(1)
        tampered = deepcopy(document)
        tampered["manifest"]["quality_findings"] = [
            {"severity": "review", "code": "missing_period", "detail": "leaked"}]
        self.assertFalse(self.service.verify_view(tampered))

    def test_partner_detects_view_digest_removal_of_redactions(self):
        self.release_with_secrets()
        document = self.service.delivery_view(1)
        tampered = deepcopy(document)
        tampered["redactions"] = []
        self.assertFalse(self.service.verify_view(tampered))

    def test_verify_view_rejects_unknown_shapes(self):
        self.assertFalse(self.service.verify_view({}))
        self.assertFalse(self.service.verify_view({"view": "mystery"}))
        self.assertFalse(self.service.verify_view("not a document"))
        self.assertFalse(self.service.verify_view(None))


class InternalAuditTests(DisclosureCase):
    def test_internal_view_traces_full_collection_chain(self):
        self.release_with_secrets()
        document = self.service.audit_view(1)
        self.assertEqual(document["view"], "internal_audit")
        chain = document["batches"][0]
        self.assertEqual(chain["metadata"]["internal_contact"], SECRET_CONTACT)
        self.assertEqual(chain["metadata"]["processing_note"], SECRET_NOTE)
        self.assertEqual(chain["findings"][0]["code"], "missing_period")
        self.assertEqual(document["manifest"]["quality_findings"][0]["code"], "missing_period")
        self.assertTrue(chain["measurements"])
        self.assertIn("raw_value", chain["measurements"][0])
        self.assertEqual([event["kind"] for event in chain["events"]],
                         ["ingest", "approve", "publish"])
        self.assertEqual(document["lineage"][0]["version"], 1)

    def test_internal_view_consistency_confirms_public_projection(self):
        _, release = self.release_with_secrets()
        document = self.service.audit_view(1)
        consistency = document["consistency"]
        self.assertTrue(consistency["digest_matches"])
        self.assertTrue(consistency["entries_match"])
        self.assertEqual(consistency["sealed_manifest_digest"], release["digest"])
        public = self.service.delivery_view(1)
        self.assertEqual(consistency["public_view_digest"], public["view_digest"])
        self.assertTrue(self.service.verify_view(document))

    def test_sealed_commitment_matches_internal_findings(self):
        self.release_with_secrets()
        internal = self.service.audit_view(1)
        public = self.service.delivery_view(1)
        expected = sha256(canonical_bytes(internal["manifest"]["quality_findings"])).hexdigest()
        self.assertEqual(public["manifest"]["quality_findings"]["sealed_sha256"], expected)

    def test_internal_view_rejects_sealed_manifest(self):
        self.release_with_secrets()
        document = self.service.audit_view(1)
        tampered = deepcopy(document)
        tampered["manifest"] = self.service.delivery_view(1)["manifest"]
        self.assertFalse(self.service.verify_view(tampered))


class StabilityTests(DisclosureCase):
    def test_masking_preserves_registered_digest(self):
        _, release = self.release_with_secrets()
        stored = self.service.store.release(1)
        public = self.service.delivery_view(1)
        internal = self.service.audit_view(1)
        self.assertEqual(public["digest"], release["digest"])
        self.assertEqual(internal["digest"], release["digest"])
        self.assertEqual(stored["digest"], release["digest"])
        self.assertEqual(manifest_digest(stored["manifest"]), release["digest"])

    def test_duplicate_exports_are_identical(self):
        self.release_with_secrets()
        first = self.service.delivery_view(1)
        second = self.service.delivery_view(1)
        self.assertEqual(first, second)
        self.assertEqual(canonical_bytes(first), canonical_bytes(second))
        self.assertEqual(first["view_digest"], second["view_digest"])
        self.assertEqual(self.service.audit_view(1), self.service.audit_view(1))

    def test_old_snapshot_view_stable_after_new_batch(self):
        self.release_with_secrets()
        public_before = self.service.delivery_view(1)
        internal_before = self.service.audit_view(1)
        later, _ = self.ingest("later", self.rows(431, start=4))
        self.assertEqual(later["status"], "accepted")
        self.service.publish(later["id"], 1)
        self.assertEqual(self.service.status()["latest_release"], 2)
        self.assertEqual(self.service.delivery_view(1), public_before)
        self.assertEqual(self.service.audit_view(1), internal_before)
        self.assertTrue(self.service.verify_view(self.service.delivery_view(1)))

    def test_views_stable_across_reopen(self):
        self.release_with_secrets()
        public_before = self.service.delivery_view(1)
        internal_before = self.service.audit_view(1)
        with LedgerService(self.db) as reopened:
            self.assertEqual(reopened.delivery_view(1), public_before)
            self.assertEqual(reopened.audit_view(1), internal_before)
            self.assertTrue(reopened.verify_view(reopened.delivery_view(1)))

    def test_second_release_links_to_first_digest(self):
        self.release_with_secrets()
        later, _ = self.ingest("later", self.rows(431, start=4))
        self.service.publish(later["id"], 1)
        first = self.service.delivery_view(1)
        second = self.service.delivery_view(2)
        self.assertEqual(second["manifest"]["parent_digest"], first["digest"])
        self.assertTrue(self.service.verify_view(second))
        carried = {entry["batch_id"] for entry in second["entries"]}
        proven = {proof["batch_id"] for proof in second["manifest"]["source_files"]}
        self.assertEqual(carried, proven)
        self.assertEqual(len(proven), 2)


class CredentialCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "cli.sqlite3"

    def call(self, *args):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--db", str(self.db), *args])
        return code, json.loads(out.getvalue()) if out.getvalue() else None, err.getvalue()

    def prepare_release(self):
        indicator = self.root / "indicator.json"
        indicator.write_text(json.dumps({"key": "co2_ppm", "title": "Synthetic carbon dioxide",
                                         "canonical_unit": "ppm", "cadence": "month",
                                         "precision": 2}), encoding="utf-8")
        source = self.root / "source.json"
        source.write_text(json.dumps({"key": "lab_a", "title": "Synthetic lab A",
                                      "license": "Local synthetic fixture",
                                      "allowed_indicators": ["co2_ppm"]}), encoding="utf-8")
        self.assertEqual(self.call("register-indicator", str(indicator))[0], 0)
        self.assertEqual(self.call("register-source", str(source))[0], 0)
        rows = [{"indicator": "co2_ppm", "period": "2026-01", "value": "428", "unit": "ppm"},
                {"indicator": "co2_ppm", "period": "2026-03", "value": "430", "unit": "ppm"}]
        manifest = write_manifest(self.root, source="lab_a", batch_id="cli_secret", rows=rows,
                                  metadata={"internal_contact": SECRET_CONTACT})
        code, ingestion, _ = self.call("ingest", str(manifest))
        self.assertEqual(code, 0)
        batch_id = ingestion["batch"]["id"]
        self.assertEqual(self.call("approve", str(batch_id), "1")[0], 0)
        code, published, _ = self.call("publish", str(batch_id), "1")
        self.assertEqual(code, 0)
        return published

    def test_cli_deliver_and_verify_view_roundtrip(self):
        published = self.prepare_release()
        delivery = self.root / "delivery.json"
        code, result, _ = self.call("deliver", "1", "--output", str(delivery))
        self.assertEqual(code, 0)
        self.assertEqual(result["digest"], published["digest"])
        serialized = delivery.read_text(encoding="utf-8")
        self.assertNotIn(SECRET_CONTACT, serialized)
        code, verification, _ = self.call("verify-view", str(delivery))
        self.assertEqual(code, 0)
        self.assertEqual(verification, {"view": "public_delivery", "verified": True})
        repeat = self.root / "repeat.json"
        self.assertEqual(self.call("deliver", "1", "--output", str(repeat))[0], 0)
        self.assertEqual(repeat.read_text(encoding="utf-8"), serialized)

    def test_cli_verify_view_detects_tampered_delivery(self):
        self.prepare_release()
        delivery = self.root / "delivery.json"
        self.assertEqual(self.call("deliver", "1", "--output", str(delivery))[0], 0)
        document = json.loads(delivery.read_text(encoding="utf-8"))
        document["entries"][0]["value"] = "999.00"
        delivery.write_text(json.dumps(document), encoding="utf-8")
        self.assertEqual(self.call("verify-view", str(delivery))[1]["verified"], False)

    def test_cli_audit_view_keeps_internal_chain(self):
        self.prepare_release()
        code, document, _ = self.call("audit-view", "1")
        self.assertEqual(code, 0)
        self.assertEqual(document["batches"][0]["metadata"]["internal_contact"], SECRET_CONTACT)
        self.assertTrue(document["consistency"]["digest_matches"])
        audit_file = self.root / "audit.json"
        self.assertEqual(self.call("audit-view", "1", "--output", str(audit_file))[0], 0)
        self.assertEqual(self.call("verify-view", str(audit_file))[1],
                         {"view": "internal_audit", "verified": True})
