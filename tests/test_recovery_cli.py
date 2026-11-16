from __future__ import annotations

from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import sqlite3
import unittest

from climateledger.cli import main
from climateledger.intake import write_manifest
from climateledger.registry import ContractError
from climateledger.service import LedgerService
from support import LedgerCase


class RecoveryTests(LedgerCase):
    def test_reopen_preserves_contracts_and_release(self):
        batch, release, _ = self.release()
        with LedgerService(self.db) as reopened:
            self.assertEqual(reopened.registry.indicator("co2_ppm").canonical_unit, "ppm")
            self.assertEqual(reopened.store.latest_release()["digest"], release["digest"])
            self.assertTrue(reopened.verify(reopened.export(1)))

    def test_retry_ingest_after_reopen_is_idempotent(self):
        batch, manifest = self.ingest("retry", self.rows(428))
        with LedgerService(self.db) as reopened:
            retried = reopened.ingest(manifest)
            self.assertTrue(retried["idempotent"])
            self.assertEqual(retried["batch"]["id"], batch["id"])
            self.assertEqual(len(reopened.batches()), 1)

    def test_retry_publish_after_reopen_is_idempotent(self):
        batch, release, _ = self.release()
        with LedgerService(self.db) as reopened:
            retried = reopened.publish(batch["id"], batch["revision"])
            self.assertEqual(retried["digest"], release["digest"])
            self.assertEqual(reopened.status()["latest_release"], 1)

    def test_failed_transaction_preserves_previous_state(self):
        batch, release, _ = self.release()
        with self.assertRaises(sqlite3.IntegrityError):
            with self.service.store.transaction():
                self.service.store.connection.execute("DELETE FROM sources WHERE key='lab_a'")
        self.assertEqual(self.service.store.latest_release()["digest"], release["digest"])

    def test_superseded_review_cannot_be_approved(self):
        first, _ = self.ingest("gap", self.rows(428) + self.rows(430, start=3))
        second, _ = self.ingest("gap", self.rows(429) + self.rows(431, start=3))
        with self.assertRaisesRegex(ContractError, "stale_revision"):
            self.service.approve(first["id"], first["revision"])
        self.assertEqual(self.service.approve(second["id"], second["revision"])["status"], "approved")

    def test_repeated_payload_after_new_revision_creates_new_revision(self):
        original, _ = self.ingest("cyclic", self.rows(428))
        changed, _ = self.ingest("cyclic", self.rows(429))
        restored, _ = self.ingest("cyclic", self.rows(428))
        self.assertEqual([original["revision"], changed["revision"], restored["revision"]], [1, 2, 3])
        self.assertEqual(original["payload_hash"], restored["payload_hash"])

    def test_approval_event_survives_restart(self):
        batch, _ = self.ingest("gap", self.rows(428) + self.rows(430, start=3))
        self.service.approve(batch["id"], 1)
        with LedgerService(self.db) as reopened:
            events = reopened.events(f"batch:{batch['id']}")
            self.assertEqual([item["kind"] for item in events], ["ingest", "approve"])

    def test_release_export_is_stable_across_restart(self):
        self.release()
        before = self.service.export(1)
        with LedgerService(self.db) as reopened:
            self.assertEqual(before, reopened.export(1))


class CliTests(unittest.TestCase):
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

    def contract(self, filename, data):
        path = self.root / filename
        path.write_text(json.dumps(data), encoding="utf-8")
        return str(path)

    def test_cli_register_ingest_publish_export_verify(self):
        indicator = self.contract("indicator.json", {"key": "co2_ppm", "title": "Synthetic carbon dioxide",
            "canonical_unit": "ppm", "cadence": "month", "precision": 2})
        source = self.contract("source.json", {"key": "lab_a", "title": "Synthetic lab A",
            "license": "Local synthetic fixture", "allowed_indicators": ["co2_ppm"]})
        self.assertEqual(self.call("register-indicator", indicator)[0], 0)
        self.assertEqual(self.call("register-source", source)[0], 0)
        manifest = write_manifest(self.root, source="lab_a", batch_id="cli_batch",
                                  rows=[{"indicator": "co2_ppm", "period": "2026-01", "value": "428", "unit": "ppm"}])
        code, ingestion, _ = self.call("ingest", str(manifest))
        self.assertEqual(code, 0)
        batch_id = ingestion["batch"]["id"]
        code, published, _ = self.call("publish", str(batch_id), "1")
        self.assertEqual(code, 0)
        self.assertEqual(published["version"], 1)
        bundle_path = self.root / "bundle.json"
        self.assertEqual(self.call("export", "1", "--output", str(bundle_path))[0], 0)
        self.assertTrue(self.call("verify", str(bundle_path))[1]["verified"])
        self.assertEqual(len(self.call("series", "co2_ppm", "--version", "1")[1]), 1)
        self.assertEqual(self.call("audit")[1]["violations"], [])

    def test_cli_invalid_input_returns_error(self):
        path = self.contract("bad.json", {"key": "Bad Key"})
        code, _, error = self.call("register-indicator", path)
        self.assertEqual(code, 2)
        self.assertIn("invalid_identifier", error)

    def test_cli_demo_is_repeatable(self):
        workspace = self.root / "demo"
        self.assertEqual(self.call("demo", "--workspace", str(workspace))[1]["verified"], True)
        repeat = self.call("demo", "--workspace", str(workspace))[1]
        self.assertTrue(repeat["idempotent"])
        self.assertEqual(repeat["release_version"], 1)
