from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import json

from climateledger.intake import load_batch, write_manifest
from climateledger.registry import ContractError, Indicator, Source, UnitRule
from support import LedgerCase


class RegistryTests(LedgerCase):
    def test_indicator_registration_is_idempotent(self):
        value = {"key": "co2_ppm", "title": "Synthetic carbon dioxide", "canonical_unit": "ppm",
                 "cadence": "month", "minimum": "0", "maximum": "1000", "precision": 2}
        self.service.register_indicator(value)
        self.assertEqual(len(self.service.registry.list_indicators()), 2)
        self.assertEqual(self.service.registry.indicator("co2_ppm").maximum, "1000")

    def test_indicator_contract_is_immutable(self):
        with self.assertRaisesRegex(ContractError, "contract_immutable"):
            self.service.register_indicator({"key": "co2_ppm", "title": "Changed", "canonical_unit": "ppm",
                                             "cadence": "month", "precision": 2})
        self.assertEqual(self.service.registry.indicator("co2_ppm").title, "Synthetic carbon dioxide")

    def test_source_contract_is_immutable(self):
        with self.assertRaisesRegex(ContractError, "contract_immutable"):
            self.service.register_source({"key": "lab_a", "title": "Changed",
                                          "license": "X", "allowed_indicators": ["co2_ppm"]})
        self.assertEqual(self.service.registry.source("lab_a").title, "Synthetic lab A")

    def test_unknown_source_indicator_is_rejected(self):
        with self.assertRaisesRegex(ContractError, "unknown_indicator"):
            self.service.register_source({"key": "lab_c", "title": "C",
                                          "license": "L", "allowed_indicators": ["missing"]})
        self.assertEqual(len(self.service.registry.list_sources()), 2)

    def test_unit_rule_is_immutable(self):
        self.service.register_unit({"indicator": "co2_ppm", "source_unit": "ppb", "factor": "0.001"})
        with self.assertRaisesRegex(ContractError, "rule_immutable"):
            self.service.register_unit({"indicator": "co2_ppm", "source_unit": "ppb", "factor": "0.002"})
        self.assertEqual(self.service.registry.conversion(self.service.registry.indicator("co2_ppm"), "ppb").factor, "0.001")

    def test_canonical_conversion_must_be_identity(self):
        with self.assertRaisesRegex(ContractError, "canonical_conversion"):
            self.service.register_unit({"indicator": "co2_ppm", "source_unit": "ppm", "factor": "10"})

    def test_invalid_indicator_bounds(self):
        with self.assertRaisesRegex(ContractError, "indicator_bounds"):
            Indicator.from_dict({"key": "invalid", "title": "Invalid", "canonical_unit": "u",
                                 "cadence": "year", "minimum": "2", "maximum": "1"})

    def test_invalid_indicator_key(self):
        with self.assertRaisesRegex(ContractError, "invalid_identifier"):
            Indicator.from_dict({"key": "Bad Key", "title": "Invalid", "canonical_unit": "u",
                                 "cadence": "year"})

    def test_invalid_source_scope_duplicate(self):
        with self.assertRaisesRegex(ContractError, "source_scope"):
            Source.from_dict({"key": "other", "title": "Other", "license": "L",
                              "allowed_indicators": ["co2_ppm", "co2_ppm"]})

    def test_zero_conversion_factor(self):
        with self.assertRaisesRegex(ContractError, "zero_factor"):
            UnitRule.from_dict({"indicator": "co2_ppm", "source_unit": "raw", "factor": "0"})


class IntakeTests(LedgerCase):
    def test_json_round_trip(self):
        rows = self.rows(428, 429)
        path = self.manifest("sample", rows)
        batch = load_batch(path)
        self.assertEqual(batch.source, "lab_a")
        self.assertEqual(batch.declared_count, 2)
        self.assertEqual(batch.rows[1]["line"], 2)
        self.assertEqual(batch.rows[1]["value"], "429")

    def test_csv_round_trip(self):
        path = self.manifest("sample", self.rows(428, 429), format_name="csv")
        batch = load_batch(path)
        self.assertEqual(batch.format, "csv")
        self.assertEqual([row["period"] for row in batch.rows], ["2026-01", "2026-02"])

    def test_payload_hash_mismatch(self):
        path = self.manifest("sample", self.rows(428))
        payload = path.with_name("sample.json")
        payload.write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "payload_hash"):
            load_batch(path)

    def test_declared_row_count_mismatch(self):
        path = self.manifest("sample", self.rows(428))
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["row_count"] = 2
        path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "row_count"):
            load_batch(path)

    def test_payload_path_cannot_escape(self):
        path = self.manifest("sample", self.rows(428))
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["payload"] = "../elsewhere.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "payload_path"):
            load_batch(path)

    def test_column_map_must_cover_fields(self):
        path = self.manifest("sample", self.rows(428))
        manifest = json.loads(path.read_text(encoding="utf-8"))
        del manifest["columns"]["unit"]
        path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "column_mapping"):
            load_batch(path)

    def test_json_rows_must_be_objects(self):
        path = self.manifest("sample", self.rows(428))
        payload = path.with_name("sample.json")
        data = b'[1]'
        payload.write_bytes(data)
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["sha256"] = sha256(data).hexdigest()
        path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "row_shape"):
            load_batch(path)

    def test_invalid_csv_header(self):
        path = self.manifest("sample", self.rows(428), format_name="csv")
        payload = path.with_name("sample.csv")
        data = b"indicator,period,value,value\nco2_ppm,2026-01,428,ppm\n"
        payload.write_bytes(data)
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["sha256"] = sha256(data).hexdigest()
        path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "csv_header"):
            load_batch(path)

    def test_empty_rows_rejected_before_write(self):
        with self.assertRaisesRegex(ContractError, "empty_rows"):
            write_manifest(self.root, source="lab_a", batch_id="empty", rows=[])

    def test_metadata_stays_with_envelope(self):
        path = self.manifest("sample", self.rows(428))
        self.assertEqual(load_batch(path).metadata, {"synthetic": True})
