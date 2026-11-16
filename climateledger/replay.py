from __future__ import annotations

from pathlib import Path
from typing import Any

from .intake import load_batch
from .normalize import Normalizer
from .quality import QualityGate
from .registry import ContractError, Registry


class ReplayInspector:
    def __init__(self, store, registry: Registry):
        self.store = store
        self.registry = registry
        self.normalizer = Normalizer(registry)
        self.quality = QualityGate(registry, store)

    def compare_manifest(self, manifest_path: str | Path, batch_id: int) -> dict[str, Any]:
        envelope = load_batch(manifest_path)
        batch = self.store.batch(batch_id)
        identity = envelope.source == batch["source"] and envelope.batch_id == batch["external_id"]
        bytes_match = envelope.payload_hash == batch["payload_hash"]
        if not identity:
            raise ContractError("replay_identity", "manifest source or external id differs from batch")
        source = self.registry.source(envelope.source)
        measurements, rejected = self.normalizer.batch(source, envelope.rows)
        expected = self.store.measurements(batch_id)
        comparison = []
        for item in measurements:
            old = next((row for row in expected if row["line"] == item.line), None)
            comparison.append({"line": item.line, "matches": old is not None and
                               old["indicator"] == item.indicator and old["period"] == item.period and
                               old["value"] == item.value and old["unit"] == item.unit,
                               "stored_value": old["value"] if old else None,
                               "replayed_value": item.value})
        return {"batch_id": batch_id, "identity_match": identity, "payload_hash_match": bytes_match,
                "stored_rows": len(expected), "replayed_rows": len(measurements),
                "rejected_rows": rejected, "row_comparison": comparison,
                "reproducible": bytes_match and len(expected) == len(measurements) and
                                all(row["matches"] for row in comparison)}

    def manifest_receipt(self, manifest_path: str | Path) -> dict[str, Any]:
        envelope = load_batch(manifest_path)
        existing = self.store.latest_batch(envelope.source, envelope.batch_id)
        return {"source": envelope.source, "external_id": envelope.batch_id,
                "payload_hash": envelope.payload_hash, "declared_count": envelope.declared_count,
                "latest_batch": existing["id"] if existing else None,
                "same_payload": existing["payload_hash"] == envelope.payload_hash if existing else None,
                "next_revision": (existing["revision"] + 1 if existing else 1)}
