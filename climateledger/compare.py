from __future__ import annotations

from dataclasses import dataclass, asdict
from decimal import Decimal
from typing import Any

from .registry import ContractError, Registry
from .normalize import period_index


@dataclass(frozen=True)
class Delta:
    source: str
    indicator: str
    period: str
    old_value: str | None
    new_value: str | None
    change: str
    old_batch: int | None
    new_batch: int | None

    def document(self) -> dict[str, Any]:
        return asdict(self)


def entry_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return row["source"], row["indicator"], row["period"]


def compare_entries(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> list[Delta]:
    left = {entry_key(row): row for row in before}
    right = {entry_key(row): row for row in after}
    changes = []
    for key in sorted(set(left) | set(right)):
        old = left.get(key)
        new = right.get(key)
        if old is None:
            kind = "added"
        elif new is None:
            kind = "removed"
        elif old["value"] != new["value"] or old["unit"] != new["unit"]:
            kind = "revised"
        elif old["batch_id"] != new["batch_id"]:
            kind = "reaffirmed"
        else:
            continue
        changes.append(Delta(*key, old["value"] if old else None,
                             new["value"] if new else None, kind,
                             old["batch_id"] if old else None,
                             new["batch_id"] if new else None))
    return changes


def summarize_changes(changes: list[Delta]) -> dict[str, Any]:
    by_kind = {name: 0 for name in ("added", "removed", "revised", "reaffirmed")}
    by_indicator: dict[str, int] = {}
    for change in changes:
        by_kind[change.change] += 1
        by_indicator[change.indicator] = by_indicator.get(change.indicator, 0) + 1
    return {"count": len(changes), "by_kind": by_kind, "by_indicator": by_indicator}


class Comparator:
    def __init__(self, store, registry: Registry):
        self.store = store
        self.registry = registry

    def releases(self, old_version: int, new_version: int) -> dict[str, Any]:
        if old_version >= new_version:
            raise ContractError("version_order", "old version must precede new version")
        self.store.release(old_version)
        self.store.release(new_version)
        changes = compare_entries(self.store.release_entries(old_version),
                                  self.store.release_entries(new_version))
        return {"from": old_version, "to": new_version,
                "summary": summarize_changes(changes),
                "changes": [change.document() for change in changes]}

    def batch_impact(self, batch_id: int) -> dict[str, Any]:
        batch = self.store.batch(batch_id)
        prior = self.store.latest_release()
        existing = self.store.release_entries(prior["version"]) if prior else []
        keys = {entry_key(item): item for item in existing}
        touched = []
        for item in self.store.measurements(batch_id):
            key = batch["source"], item["indicator"], item["period"]
            previous = keys.get(key)
            touched.append({"indicator": item["indicator"], "period": item["period"],
                            "new_value": item["value"],
                            "old_value": previous["value"] if previous else None,
                            "effect": "added" if previous is None else (
                                "revised" if previous["value"] != item["value"] else "reaffirmed")})
        return {"batch": batch_id, "against_version": prior["version"] if prior else None,
                "touched": touched}


class SeriesWindow:
    def __init__(self, store, registry: Registry):
        self.store = store
        self.registry = registry

    def slice(self, indicator_key: str, version: int, *, source: str | None = None,
              start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
        indicator = self.registry.indicator(indicator_key)
        self.store.release(version)
        rows = [row for row in self.store.release_entries(version)
                if row["indicator"] == indicator_key and (source is None or row["source"] == source)]
        if start is not None:
            start_index = period_index(start, indicator.cadence)
            rows = [row for row in rows if period_index(row["period"], indicator.cadence) >= start_index]
        if end is not None:
            end_index = period_index(end, indicator.cadence)
            rows = [row for row in rows if period_index(row["period"], indicator.cadence) <= end_index]
        return rows

    def statistics(self, indicator_key: str, version: int, *, source: str | None = None) -> dict[str, Any]:
        rows = self.slice(indicator_key, version, source=source)
        values = [Decimal(row["value"]) for row in rows]
        if not values:
            return {"count": 0, "minimum": None, "maximum": None, "mean": None,
                    "first_period": None, "last_period": None}
        return {"count": len(values), "minimum": str(min(values)), "maximum": str(max(values)),
                "mean": str(sum(values) / len(values)),
                "first_period": min(row["period"] for row in rows),
                "last_period": max(row["period"] for row in rows)}
