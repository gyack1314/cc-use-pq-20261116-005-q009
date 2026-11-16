from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_EVEN, InvalidOperation
from typing import Any
import re

from .registry import ContractError, Registry, Source, precise


MONTH = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
YEAR = re.compile(r"^(\d{4})$")


@dataclass(frozen=True)
class Measurement:
    source: str
    indicator: str
    period: str
    value: str
    unit: str
    raw_value: str
    raw_unit: str
    line: int
    rule_factor: str
    rule_offset: str

    def document(self) -> dict[str, Any]:
        return asdict(self)


def normalize_period(value: Any, cadence: str) -> str:
    if not isinstance(value, str):
        raise ContractError("period_type", "period must be text")
    match = MONTH.fullmatch(value) if cadence == "month" else YEAR.fullmatch(value)
    if not match:
        raise ContractError("period_format", f"period {value!r} does not match {cadence}")
    year = int(match.group(1))
    if not 1900 <= year <= 2200:
        raise ContractError("period_range", "period year must be 1900-2200")
    if cadence == "month":
        date(year, int(match.group(2)), 1)
    return value


def period_index(period: str, cadence: str) -> int:
    if cadence == "year":
        return int(period)
    year, month = period.split("-")
    return int(year) * 12 + int(month) - 1


def period_from_index(index: int, cadence: str) -> str:
    if cadence == "year":
        return str(index)
    year, zero_month = divmod(index, 12)
    return f"{year:04d}-{zero_month + 1:02d}"


def canonical_decimal(value: Decimal, precision: int) -> str:
    quantum = Decimal(1).scaleb(-precision)
    try:
        result = value.quantize(quantum, rounding=ROUND_HALF_EVEN)
    except InvalidOperation:
        raise ContractError("value_overflow", "value cannot be represented at indicator precision") from None
    return format(result, "f")


class Normalizer:
    def __init__(self, registry: Registry):
        self.registry = registry

    def measurement(self, source: Source, row: dict[str, Any]) -> Measurement:
        key = row.get("indicator")
        indicator = self.registry.indicator(key)
        self.registry.require_scope(source, indicator)
        period = normalize_period(row.get("period"), indicator.cadence)
        raw_unit = row.get("unit")
        if not isinstance(raw_unit, str) or not raw_unit.strip():
            raise ContractError("unit_type", "unit must be nonempty text")
        rule = self.registry.conversion(indicator, raw_unit)
        raw = precise(row.get("value"), "value")
        converted = rule.convert(raw)
        value = canonical_decimal(converted, indicator.precision)
        line = row.get("line")
        if type(line) is not int or line < 1:
            raise ContractError("line_number", "line must be a positive integer")
        return Measurement(source.key, indicator.key, period, value, indicator.canonical_unit,
                           str(raw), raw_unit, line, rule.factor, rule.offset)

    def batch(self, source: Source, rows: tuple[dict[str, Any], ...]) -> tuple[list[Measurement], list[dict[str, Any]]]:
        accepted = []
        rejected = []
        for row in rows:
            try:
                accepted.append(self.measurement(source, row))
            except ContractError as exc:
                rejected.append({"line": row.get("line"), "code": exc.code, "detail": exc.detail})
        return accepted, rejected


def index_by_series(measurements: list[Measurement]) -> dict[str, list[Measurement]]:
    series: dict[str, list[Measurement]] = {}
    for item in measurements:
        series.setdefault(item.indicator, []).append(item)
    for values in series.values():
        values.sort(key=lambda item: item.period)
    return series
