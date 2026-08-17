"""Jinja2 rendering for action and extractor config.

Two things here are less obvious than they look, and both are load-bearing:

`ChainableStrictUndefined` keeps `StrictUndefined`'s habit of raising on a
typo, while still letting `{{ meter.previous.reading | default('?') }}` reach
the filter when a Python extractor returned no `meter` at all. Plain
`StrictUndefined` raises the moment `.previous` is touched, so `default` never
gets a chance; plain `Undefined` renders a typo as an empty string, which is
how a bill quietly gets POSTed with no amount.

`Expr` (the `!expr` YAML tag) is how a value keeps its Python type. Jinja
renders to strings, which is right for a calendar description and wrong for
`{"amount": 1234.50}`. Jinja's own `NativeEnvironment` looks like the answer
and is not: it runs `literal_eval` over the result, so the account number
"0012" silently becomes the integer 12.
"""

import re
from datetime import date, datetime, timedelta
from decimal import Decimal

from jinja2 import Environment, StrictUndefined

# A string with neither of these is returned untouched, so a literal brace in
# some sender's subject line can never be mistaken for a template.
_TEMPLATE_MARKERS = ("{{", "{%")


class ChainableStrictUndefined(StrictUndefined):
    """Strict about being *used*, permissive about being *traversed*."""

    __slots__ = ()

    def _chain(self, key):
        return type(self)(hint=self._undefined_hint, name=f"{self._undefined_name}.{key}")

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return self._chain(name)

    def __getitem__(self, key):
        return self._chain(key)


class Expr:
    """An `!expr` value: a Jinja expression evaluated to a Python object."""

    __slots__ = ("source",)

    def __init__(self, source):
        self.source = source

    def __repr__(self):
        return f"Expr({self.source!r})"


def _filter_date(value, fmt="%Y-%m-%d"):
    if isinstance(value, (datetime, date)):
        return value.strftime(fmt)
    raise TypeError(f"the 'date' filter needs a date or datetime, got {type(value).__name__}")


def _filter_money(value, places=2):
    quantum = Decimal(1).scaleb(-places)
    return str(Decimal(str(value)).quantize(quantum))


def _filter_shift_days(value, days):
    if not isinstance(value, (datetime, date)):
        raise TypeError(f"'shift_days' needs a date, got {type(value).__name__}")
    return value + timedelta(days=days)


def _filter_slug(value):
    return re.sub(r"[^\w.-]+", "_", str(value)).strip("_") or "unknown"


def build_environment():
    env = Environment(undefined=ChainableStrictUndefined, keep_trailing_newline=True)
    env.filters["date"] = _filter_date
    env.filters["money"] = _filter_money
    env.filters["shift_days"] = _filter_shift_days
    env.filters["slug"] = _filter_slug
    return env


ENV = build_environment()


def render_value(value, context, skip_keys=(), _key=None):
    """Render one config value against `context`.

    Walks dicts and lists. Mapping keys are never rendered -- only values --
    and any key named in `skip_keys` is passed through verbatim, which is how
    a regex `pattern` full of `\\d{2}` survives.
    """
    if isinstance(value, Expr):
        return ENV.compile_expression(value.source, undefined_to_none=False)(**context)
    if isinstance(value, str):
        if _key in skip_keys or not any(m in value for m in _TEMPLATE_MARKERS):
            return value
        return ENV.from_string(value).render(**context)
    if isinstance(value, dict):
        return {k: render_value(v, context, skip_keys, _key=k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [render_value(v, context, skip_keys) for v in value]
    return value


