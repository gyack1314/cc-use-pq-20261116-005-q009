from __future__ import annotations

from dataclasses import dataclass, asdict
from decimal import Decimal, InvalidOperation
from typing import Any
import json
import re


class ContractError(ValueError):
    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{1,39}$")


def identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ContractError("invalid_identifier", f"{label} must be a lower-case identifier")
    return value


def precise(value: Any, label: str) -> Decimal:
    if isinstance(value, bool) or value is None or isinstance(value, (dict, list)):
        raise ContractError("invalid_decimal", f"{label} must be a finite decimal")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ContractError("invalid_decimal", f"{label} must be a finite decimal") from None
    if not number.is_finite():
        raise ContractError("invalid_decimal", f"{label} must be a finite decimal")
    return number


@dataclass(frozen=True)
class Indicator:
    key: str
    title: str
    canonical_unit: str
    cadence: str
    minimum: str | None
    maximum: str | None
    precision: int
    allow_negative: bool

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Indicator":
        if not isinstance(value, dict):
            raise ContractError("indicator_shape", "indicator must be an object")
        key = identifier(value.get("key"), "indicator key")
        title = value.get("title")
        if not isinstance(title, str) or not title.strip() or len(title) > 120:
            raise ContractError("indicator_title", "title must contain 1-120 characters")
        unit = value.get("canonical_unit")
        if not isinstance(unit, str) or not unit.strip() or len(unit) > 32:
            raise ContractError("indicator_unit", "canonical unit must contain 1-32 characters")
        cadence = value.get("cadence")
        if cadence not in {"month", "year"}:
            raise ContractError("indicator_cadence", "cadence must be month or year")
        precision = value.get("precision", 3)
        if type(precision) is not int or not 0 <= precision <= 9:
            raise ContractError("indicator_precision", "precision must be an integer from 0 to 9")
        bounds = []
        for label in ("minimum", "maximum"):
            raw = value.get(label)
            bounds.append(str(precise(raw, label)) if raw is not None else None)
        low, high = bounds
        if low is not None and high is not None and Decimal(low) > Decimal(high):
            raise ContractError("indicator_bounds", "minimum exceeds maximum")
        negative = value.get("allow_negative", False)
        if type(negative) is not bool:
            raise ContractError("indicator_negative", "allow_negative must be boolean")
        return cls(key, title.strip(), unit.strip(), cadence, low, high, precision, negative)

    def encode(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class Source:
    key: str
    title: str
    license: str
    allowed_indicators: tuple[str, ...]
    active: bool

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Source":
        if not isinstance(value, dict):
            raise ContractError("source_shape", "source must be an object")
        key = identifier(value.get("key"), "source key")
        title = value.get("title")
        license_text = value.get("license")
        if not isinstance(title, str) or not title.strip() or len(title) > 120:
            raise ContractError("source_title", "source title must contain 1-120 characters")
        if not isinstance(license_text, str) or not license_text.strip() or len(license_text) > 400:
            raise ContractError("source_license", "license text must contain 1-400 characters")
        allowed = value.get("allowed_indicators")
        if not isinstance(allowed, list) or not allowed:
            raise ContractError("source_scope", "allowed_indicators must be nonempty")
        keys = tuple(identifier(item, "allowed indicator") for item in allowed)
        if len(set(keys)) != len(keys):
            raise ContractError("source_scope", "duplicate allowed indicator")
        active = value.get("active", True)
        if type(active) is not bool:
            raise ContractError("source_active", "active must be boolean")
        return cls(key, title.strip(), license_text.strip(), keys, active)

    def encode(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class UnitRule:
    indicator: str
    source_unit: str
    factor: str
    offset: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "UnitRule":
        if not isinstance(value, dict):
            raise ContractError("unit_rule_shape", "unit rule must be an object")
        indicator = identifier(value.get("indicator"), "indicator")
        source_unit = value.get("source_unit")
        if not isinstance(source_unit, str) or not source_unit.strip() or len(source_unit) > 32:
            raise ContractError("source_unit", "source unit must contain 1-32 characters")
        factor = precise(value.get("factor"), "factor")
        offset = precise(value.get("offset", 0), "offset")
        if factor == 0:
            raise ContractError("zero_factor", "conversion factor cannot be zero")
        return cls(indicator, source_unit.strip(), str(factor), str(offset))

    def convert(self, raw: Any) -> Decimal:
        return precise(raw, "value") * Decimal(self.factor) + Decimal(self.offset)


class Registry:
    def __init__(self, store):
        self.store = store

    def register_indicator(self, value: dict[str, Any]) -> Indicator:
        indicator = Indicator.from_dict(value)
        self.store.register_contract("indicators", indicator.key, indicator.encode())
        return indicator

    def register_source(self, value: dict[str, Any]) -> Source:
        source = Source.from_dict(value)
        for key in source.allowed_indicators:
            self.indicator(key)
        self.store.register_contract("sources", source.key, source.encode())
        return source

    def register_unit(self, value: dict[str, Any]) -> UnitRule:
        rule = UnitRule.from_dict(value)
        indicator = self.indicator(rule.indicator)
        if rule.source_unit == indicator.canonical_unit and (rule.factor != "1" or rule.offset != "0"):
            raise ContractError("canonical_conversion", "canonical unit cannot have nonidentity conversion")
        self.store.register_unit(rule)
        return rule

    def indicator(self, key: str) -> Indicator:
        identifier(key, "indicator")
        value = self.store.get_contract("indicators", key)
        if value is None:
            raise ContractError("unknown_indicator", f"indicator {key} is not registered")
        return Indicator.from_dict(json.loads(value))

    def source(self, key: str) -> Source:
        identifier(key, "source")
        value = self.store.get_contract("sources", key)
        if value is None:
            raise ContractError("unknown_source", f"source {key} is not registered")
        return Source.from_dict(json.loads(value))

    def conversion(self, indicator: Indicator, source_unit: str) -> UnitRule:
        if source_unit == indicator.canonical_unit:
            return UnitRule(indicator.key, source_unit, "1", "0")
        rule = self.store.get_unit(indicator.key, source_unit)
        if rule is None:
            raise ContractError("unknown_unit", f"no conversion for {indicator.key}: {source_unit}")
        return rule

    def require_scope(self, source: Source, indicator: Indicator) -> None:
        if not source.active:
            raise ContractError("inactive_source", f"source {source.key} is inactive")
        if indicator.key not in source.allowed_indicators:
            raise ContractError("scope_violation", f"source {source.key} cannot submit {indicator.key}")

    def list_indicators(self) -> list[Indicator]:
        return [Indicator.from_dict(json.loads(value)) for value in self.store.list_contracts("indicators")]

    def list_sources(self) -> list[Source]:
        return [Source.from_dict(json.loads(value)) for value in self.store.list_contracts("sources")]
