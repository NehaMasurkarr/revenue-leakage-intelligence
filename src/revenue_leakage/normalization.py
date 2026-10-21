"""Explicit, lossless normalization of financial source payloads."""

from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import Date, Integer, String

from revenue_leakage.schema import TABLES


def normalize_record(table_name, payload):
    table = TABLES[table_name]
    if set(payload) != set(table.columns.keys()):
        raise ValueError(f"{table_name}: required columns do not match")
    result = {}
    for column in table.columns:
        value = payload[column.name]
        field = f"{table_name}.{column.name}"
        if value is None or isinstance(value, bool):
            raise ValueError(f"{field}: null or boolean value")
        if isinstance(column.type, (Integer,)):
            try:
                number = Decimal(str(value))
            except InvalidOperation as exc:
                raise ValueError(f"{field}: invalid integer") from exc
            limit = 2**63 if column.name.endswith("_cents") else 2**31
            if not number.is_finite() or number != number.to_integral_value():
                raise ValueError(f"{field}: fractional or nonfinite integer")
            if not -limit <= number < limit:
                raise ValueError(f"{field}: integer out of range")
            result[column.name] = int(number)
        elif isinstance(column.type, Date):
            if not isinstance(value, str):
                raise ValueError(f"{field}: expected ISO calendar date")
            try:
                result[column.name] = date.fromisoformat(value.strip())
            except ValueError as exc:
                raise ValueError(f"{field}: invalid ISO calendar date") from exc
        elif isinstance(column.type, String):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field}: required text")
            normalized = value.strip()
            if column.name == "currency":
                normalized = normalized.upper()
            if column.name in ("status", "kind", "segment"):
                normalized = normalized.lower()
            if len(normalized) > column.type.length:
                raise ValueError(f"{field}: text exceeds column length")
            result[column.name] = normalized
    return result
