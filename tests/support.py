from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from climateledger.intake import write_manifest
from climateledger.service import LedgerService


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "ledger.sqlite3"
        self.service = LedgerService(self.db)
        self.addCleanup(self.service.close)
        self.service.register_indicator({"key": "co2_ppm", "title": "Synthetic carbon dioxide",
                                         "canonical_unit": "ppm", "cadence": "month", "minimum": "0",
                                         "maximum": "1000", "precision": 2})
        self.service.register_indicator({"key": "fire_area", "title": "Synthetic fire area",
                                         "canonical_unit": "ha", "cadence": "year", "minimum": "0",
                                         "maximum": "100000000", "precision": 0})
        self.service.register_source({"key": "lab_a", "title": "Synthetic lab A",
                                      "license": "Local synthetic fixture", "allowed_indicators": ["co2_ppm", "fire_area"]})
        self.service.register_source({"key": "lab_b", "title": "Synthetic lab B",
                                      "license": "Local synthetic fixture", "allowed_indicators": ["co2_ppm"]})
        self.service.register_unit({"indicator": "co2_ppm", "source_unit": "ppb", "factor": "0.001"})

    def rows(self, *values, indicator="co2_ppm", unit="ppm", start=1):
        return [{"indicator": indicator, "period": f"2026-{n:02d}" if indicator == "co2_ppm" else str(2025 + n),
                 "value": str(value), "unit": unit} for n, value in enumerate(values, start)]

    def manifest(self, batch_id, rows, source="lab_a", format_name="json"):
        folder = self.root / source / batch_id
        return write_manifest(folder, source=source, batch_id=batch_id, rows=rows,
                              format_name=format_name, metadata={"synthetic": True})

    def ingest(self, batch_id, rows, source="lab_a", format_name="json"):
        manifest = self.manifest(batch_id, rows, source, format_name)
        return self.service.ingest(manifest)["batch"], manifest

    def release(self, batch_id="first", rows=None, source="lab_a"):
        batch, manifest = self.ingest(batch_id, rows or self.rows(428, 429), source)
        self.assertEqual(batch["status"], "accepted")
        published = self.service.publish(batch["id"], batch["revision"])
        return batch, published, manifest
