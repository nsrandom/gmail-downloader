"""Turning a captured string into a typed value."""

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

TYPES = ("string", "int", "float", "money", "date", "datetime", "bool", "raw")

_WS_RE = re.compile(r"\s+")
_NUMERIC_RE = re.compile(r"[^\d.\-]")

_TRUE = {"true", "yes", "y", "1", "on"}
_FALSE = {"false", "no", "n", "0", "off"}


class CoercionError(ValueError):
    """A captured value did not fit the declared type."""


def coerce(value, type_name="string", fmt=None):
    if value is None:
        return None
    if type_name not in TYPES:
        raise CoercionError(f"unknown type '{type_name}' (expected one of {', '.join(TYPES)})")

    if type_name == "raw":
        return value
    if isinstance(value, (list, tuple)):
        return [coerce(v, type_name, fmt) for v in value]

    text = _WS_RE.sub(" ", str(value)).strip()

    if type_name == "string":
        return text
    if not text:
        raise CoercionError(f"cannot read an empty value as {type_name}")

    if type_name in ("int", "float", "money"):
        # Strips currency symbols, thousands separators, and stray nbsp.
        cleaned = _NUMERIC_RE.sub("", text.replace("\xa0", ""))
        if not cleaned or cleaned in ("-", ".", "-."):
            raise CoercionError(f"no number in {value!r}")
        try:
            if type_name == "int":
                return int(float(cleaned))
            if type_name == "float":
                return float(cleaned)
            return Decimal(cleaned)
        except (ValueError, InvalidOperation) as e:
            raise CoercionError(f"{value!r} is not a valid {type_name}: {e}") from e

    if type_name in ("date", "datetime"):
        parsed = None
        if fmt:
            try:
                parsed = datetime.strptime(text, fmt)
            except ValueError as e:
                raise CoercionError(f"{value!r} does not match format {fmt!r}: {e}") from e
        else:
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError as e:
                raise CoercionError(
                    f"{value!r} is not ISO-8601; give the step a `format:` (strptime)"
                ) from e
        return parsed.date() if type_name == "date" else parsed

    if type_name == "bool":
        lowered = text.lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
        raise CoercionError(f"{value!r} is not a boolean")

    raise CoercionError(f"unhandled type '{type_name}'")


def json_safe(value, money_as="number"):
    """Convert a record into something a JSON encoder accepts.

    Decimals are left alone -- simplejson writes them as exact numbers, which
    the stdlib encoder cannot do -- unless the target asked for strings.
    """
    if isinstance(value, Decimal):
        return str(value) if money_as == "string" else value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: json_safe(v, money_as) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v, money_as) for v in value]
    return value
