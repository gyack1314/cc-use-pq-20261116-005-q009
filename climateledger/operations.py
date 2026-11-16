from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any

from .registry import ContractError, Registry


@dataclass(frozen=True)
class QueueItem:
    batch_id: int
    source: str
    external_id: str
    revision: int
    status: str
    finding_codes: tuple[str, ...]
    received_at: str

    def document(self) -> dict[str, Any]:
        return asdict(self)


class ReviewQueue:
    def __init__(self, store):
        self.store = store

    def pending(self, source: str | None = None) -> list[dict[str, Any]]:
        items = []
        for batch in self.store.list_batches("review"):
            if source is not None and batch["source"] != source:
                continue
            latest = self.store.latest_batch(batch["source"], batch["external_id"])
            if latest["id"] != batch["id"]:
                continue
            items.append(QueueItem(batch["id"], batch["source"], batch["external_id"],
                                   batch["revision"], batch["status"],
                                   tuple(sorted({finding["code"] for finding in batch["findings"]})),
                                   batch["received_at"]).document())
        return items

    def rejected(self, source: str | None = None) -> list[dict[str, Any]]:
        results = []
        for batch in self.store.list_batches("rejected"):
            if source is not None and batch["source"] != source:
                continue
            results.append({"batch_id": batch["id"], "source": batch["source"],
                            "external_id": batch["external_id"], "revision": batch["revision"],
                            "blocking": [finding for finding in batch["findings"]
                                         if finding["severity"] == "block"]})
        return results

    def state_history(self, source: str, external_id: str) -> list[dict[str, Any]]:
        rows = [batch for batch in self.store.list_batches()
                if batch["source"] == source and batch["external_id"] == external_id]
        if not rows:
            raise ContractError("unknown_batch", "no batch history for source and external id")
        return [{"batch_id": batch["id"], "revision": batch["revision"],
                 "status": batch["status"], "payload_hash": batch["payload_hash"],
                 "published_version": batch["published_version"],
                 "received_at": batch["received_at"], "approved_at": batch["approved_at"]}
                for batch in rows]

    def work_summary(self) -> dict[str, Any]:
        pending = self.pending()
        rejected = self.rejected()
        return {"pending_count": len(pending), "rejected_count": len(rejected),
                "pending_sources": sorted({row["source"] for row in pending}),
                "rejected_sources": sorted({row["source"] for row in rejected})}


class ReceiptCatalog:
    def __init__(self, store, registry: Registry):
        self.store = store
        self.registry = registry

    def source_summary(self, source_key: str) -> dict[str, Any]:
        source = self.registry.source(source_key)
        batches = [row for row in self.store.list_batches() if row["source"] == source_key]
        latest = self.store.latest_release()
        entries = self.store.release_entries(latest["version"]) if latest else []
        current = [row for row in entries if row["source"] == source_key]
        return {"source": source.key, "title": source.title, "license": source.license,
                "active": source.active, "allowed_indicators": list(source.allowed_indicators),
                "batch_count": len(batches), "current_measurements": len(current),
                "published_batch_ids": sorted({row["batch_id"] for row in current}),
                "latest_release": latest["version"] if latest else None}

    def indicator_summary(self, indicator_key: str) -> dict[str, Any]:
        indicator = self.registry.indicator(indicator_key)
        latest = self.store.latest_release()
        entries = self.store.release_entries(latest["version"]) if latest else []
        current = [row for row in entries if row["indicator"] == indicator_key]
        return {"indicator": indicator.key, "title": indicator.title,
                "canonical_unit": indicator.canonical_unit, "cadence": indicator.cadence,
                "source_count": len({row["source"] for row in current}),
                "measurement_count": len(current),
                "first_period": min((row["period"] for row in current), default=None),
                "last_period": max((row["period"] for row in current), default=None),
                "latest_release": latest["version"] if latest else None}

    def batch_receipt(self, batch_id: int) -> dict[str, Any]:
        batch = self.store.batch(batch_id)
        return {"batch_id": batch_id, "source": batch["source"],
                "external_id": batch["external_id"], "revision": batch["revision"],
                "payload_hash": batch["payload_hash"], "received_at": batch["received_at"],
                "status": batch["status"], "row_count": batch["row_count"],
                "accepted_rows": len(self.store.measurements(batch_id)),
                "published_version": batch["published_version"]}

    def release_receipt(self, version: int) -> dict[str, Any]:
        release = self.store.release(version)
        batch = release["manifest"]["batch"]
        return {"version": version, "created_at": release["created_at"],
                "digest": release["digest"], "parent_digest": release["manifest"]["parent_digest"],
                "entry_count": release["manifest"]["entry_count"], "source_batch": batch}
