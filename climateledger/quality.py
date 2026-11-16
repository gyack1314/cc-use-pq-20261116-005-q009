from __future__ import annotations

from dataclasses import dataclass, asdict
from decimal import Decimal
from typing import Any

from .normalize import Measurement, index_by_series, period_index, period_from_index
from .registry import Registry


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    indicator: str | None
    period: str | None
    line: int | None
    detail: str

    def document(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Assessment:
    status: str
    accepted_count: int
    rejected_count: int
    findings: tuple[Finding, ...]

    def document(self) -> dict[str, Any]:
        return {"status": self.status, "accepted_count": self.accepted_count,
                "rejected_count": self.rejected_count,
                "findings": [item.document() for item in self.findings]}


class QualityGate:
    def __init__(self, registry: Registry, store):
        self.registry = registry
        self.store = store

    def assess(self, source_key: str, measurements: list[Measurement],
               rejected: list[dict[str, Any]]) -> Assessment:
        findings: list[Finding] = []
        for item in rejected:
            findings.append(Finding("block", item["code"], None, None, item["line"], item["detail"]))
        series = index_by_series(measurements)
        seen: dict[tuple[str, str], Measurement] = {}
        for item in measurements:
            key = item.indicator, item.period
            previous = seen.get(key)
            if previous:
                code = "duplicate_period" if previous.value == item.value else "conflicting_period"
                findings.append(Finding("block", code, item.indicator, item.period, item.line,
                                        f"period previously supplied on line {previous.line}"))
            else:
                seen[key] = item
            indicator = self.registry.indicator(item.indicator)
            number = Decimal(item.value)
            if not indicator.allow_negative and number < 0:
                findings.append(Finding("block", "negative_value", item.indicator, item.period,
                                        item.line, "negative measurement not allowed"))
            if indicator.minimum is not None and number < Decimal(indicator.minimum):
                findings.append(Finding("block", "below_minimum", item.indicator, item.period,
                                        item.line, f"below registered minimum {indicator.minimum}"))
            if indicator.maximum is not None and number > Decimal(indicator.maximum):
                findings.append(Finding("block", "above_maximum", item.indicator, item.period,
                                        item.line, f"above registered maximum {indicator.maximum}"))
        for key, items in series.items():
            indicator = self.registry.indicator(key)
            indices = sorted({period_index(item.period, indicator.cadence) for item in items})
            if indices:
                missing = sorted(set(range(indices[0], indices[-1] + 1)) - set(indices))
                if len(missing) > 1000:
                    findings.append(Finding("block", "coverage_span", key, None, None,
                                            "more than 1000 missing periods"))
                else:
                    for index in missing:
                        period = period_from_index(index, indicator.cadence)
                        findings.append(Finding("review", "missing_period", key, period, None,
                                                "gap within submitted series"))
            for item in items:
                prior = self.store.current_measurement(source_key, key, item.period)
                if prior is not None and prior["value"] != item.value:
                    findings.append(Finding("review", "revised_value", key, item.period, item.line,
                                            f"published value {prior['value']} differs"))
        if not measurements:
            findings.append(Finding("block", "empty_batch", None, None, None, "no accepted measurements"))
        status = "rejected" if any(item.severity == "block" for item in findings) else (
            "review" if findings else "accepted")
        findings.sort(key=lambda item: (item.severity, item.code, item.indicator or "", item.period or "", item.line or 0))
        return Assessment(status, len(measurements), len(rejected), tuple(findings))

    def reviewable(self, assessment: Assessment) -> list[Finding]:
        return [finding for finding in assessment.findings if finding.severity == "review"]

    def blocking(self, assessment: Assessment) -> list[Finding]:
        return [finding for finding in assessment.findings if finding.severity == "block"]
