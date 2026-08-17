"""The extract stage: an Email in, one JSON-shaped dict -- the record -- out.

The record is the contract between the two halves of the runner. Everything
before it is about reading Gmail; everything after it only sees a dict. So a
Python extractor is interchangeable with a declarative one as long as it
returns the same shape.
"""

import importlib
import logging
from pathlib import Path

from core.coerce import CoercionError, coerce
from core.templating import render_value

_REGISTRY = {}

# A regex `pattern` is never rendered as a template, so `\d{2}` needs no
# escaping. Everything else in a step's config is.
NEVER_RENDERED = ("pattern",)

# Keys that route the step rather than configure the extractor.
ROUTING_KEYS = ("name", "using", "source", "type", "format", "required", "default",
                "fallbacks")


class ExtractionError(Exception):
    """A step could not produce its value."""


class MissingField(ExtractionError):
    """A required field was not found."""


def extractor(name):
    def register(fn):
        _REGISTRY[name] = fn
        return fn
    return register


def get_extractor(name):
    if name in _REGISTRY:
        return _REGISTRY[name]
    raise ExtractionError(
        f"unknown extractor '{name}' (known: {', '.join(sorted(_REGISTRY))}, "
        f"or `using: python` with a module in extractors/)"
    )


def load_python_step(module_name, function_name):
    """Resolve `using: python` to a callable in extractors/<module>.py."""
    try:
        module = importlib.import_module(f"extractors.{module_name}")
    except ImportError as e:
        available = sorted(
            p.stem for p in Path(__file__).parent.glob("*.py") if not p.stem.startswith("_")
        )
        raise ExtractionError(
            f"no module extractors/{module_name}.py ({e}). Available: {', '.join(available)}"
        ) from e
    fn = getattr(module, function_name, None)
    if fn is None:
        raise ExtractionError(
            f"extractors/{module_name}.py has no function '{function_name}'"
        )
    return fn


def run_computed(record, computed_config, email, label=""):
    """Derived fields, added after the steps have run.

    Deliberately four operations and no more. The common "three days before
    the due date" case should not need a Python file, and everything past that
    should not turn this table into a small programming language -- that is
    what `using: python` is for.
    """
    from datetime import date, datetime, timedelta

    for name, spec in (computed_config or {}).items():
        if not isinstance(spec, dict) or "using" not in spec:
            raise ExtractionError(f"{label}computed '{name}' needs a `using:`")
        using = spec["using"]

        if using == "const":
            record[name] = spec.get("value")

        elif using == "date_shift":
            source = spec.get("from")
            if source not in record:
                raise ExtractionError(
                    f"{label}computed '{name}' shifts from '{source}', which was not extracted"
                )
            base = record[source]
            if not isinstance(base, (datetime, date)):
                raise ExtractionError(
                    f"{label}computed '{name}': '{source}' is a {type(base).__name__}, "
                    f"not a date -- give that step `type: date`"
                )
            record[name] = base + timedelta(days=int(spec.get("days", 0)))

        elif using == "format":
            template = spec.get("template")
            if template is None:
                raise ExtractionError(f"{label}computed '{name}' needs a `template:`")
            record[name] = render_value(template, {**record, "email": email})

        elif using == "python":
            fn = load_python_step(spec.get("module"), spec.get("function", "compute"))
            record[name] = fn(email, spec, record)

        else:
            raise ExtractionError(
                f"{label}computed '{name}': unknown `using: {using}` "
                f"(expected const, date_shift, format, or python)"
            )

    return record


def run_extract(email, extract_config, computed_config=None, on_missing_field="fail", label=""):
    """Build the record for one email.

    Steps run in order and see the record built so far -- their own config is
    rendered against it first, so a later step can key off an earlier value.
    """
    record = {}
    if not extract_config:
        return run_computed(record, computed_config, email, label)

    default_source = extract_config.get("source", "html")

    for step in extract_config.get("steps", []):
        rendered = render_value(step, {"record": record, "email": email, **record},
                                skip_keys=NEVER_RENDERED)
        name = rendered.get("name")
        required = rendered.get("required", True)
        candidates = _candidates(rendered)
        what = f"{label}field '{name}'" if name else f"{label}step '{rendered['using']}'"

        value = None
        matched = None
        last_error = None

        for index, candidate in enumerate(candidates):
            raw = _run_candidate(candidate, email, record, default_source, what)
            if _is_empty(raw):
                continue

            if name is None:
                if not isinstance(raw, dict):
                    raise ExtractionError(
                        f"{what} has no `name:`, so it must return a dict to merge; "
                        f"got {type(raw).__name__}"
                    )
                value, matched = raw, index
                break

            try:
                value = coerce(raw, rendered.get("type", "string"), rendered.get("format"))
            except CoercionError as e:
                # A match that will not coerce is no better than no match, so
                # let a later candidate have a go before giving up.
                last_error = e
                continue
            matched = index
            break

        if matched:
            # Worth surfacing: the primary no longer matches, which usually
            # means the sender has redesigned and the config needs revisiting.
            logging.info(f"  {what} came from fallback #{matched}")

        if matched is None:
            problem = f"{what}: {last_error}" if last_error else f"{what} not found"
            if len(candidates) > 1 and not last_error:
                problem += f" (tried {len(candidates)} candidates)"
            if not required:
                # An unnamed step merges a dict; there is no key to put a
                # default under, so it has nothing to fall back to.
                if "default" in rendered and name:
                    record[name] = rendered["default"]
                elif last_error:
                    logging.warning(f"  {problem}")
                continue
            if on_missing_field == "fail":
                raise MissingField(problem)
            logging.warning(f"  {problem}; continuing ({on_missing_field})")
            if on_missing_field == "skip":
                raise MissingField(problem)
            continue

        if name is None:
            overlap = set(value) & set(record)
            if overlap:
                logging.warning(f"  {what} overwrites {sorted(overlap)}")
            record.update(value)
        else:
            record[name] = value

    return run_computed(record, computed_config, email, label)


def _is_empty(value):
    return value is None or (isinstance(value, (str, list, dict)) and len(value) == 0)


def _infer_using(fallback, primary_using):
    """A fallback that only swaps the selector need not restate `using`."""
    if "using" in fallback:
        return fallback["using"]
    if "selector" in fallback:
        return "css"
    if "pattern" in fallback:
        return "regex"
    return primary_using


def _candidates(rendered):
    """The primary extractor, then each fallback, in order.

    Senders redesign their mail, and the old messages do not change to match.
    A fallback inherits the field's identity -- its name, type, format, and
    whether it is required -- and brings only its own way of finding the value,
    so one field can span three layouts without three near-identical steps.
    """
    primary = {k: v for k, v in rendered.items() if k != "fallbacks"}
    candidates = [primary]

    for fallback in rendered.get("fallbacks") or []:
        inherited = {
            k: rendered[k]
            for k in ("name", "type", "format", "required", "default", "source")
            if k in rendered
        }
        candidates.append(
            {**inherited, **fallback, "using": _infer_using(fallback, rendered["using"])}
        )
    return candidates


def _run_candidate(candidate, email, record, default_source, what):
    using = candidate["using"]
    config = {k: v for k, v in candidate.items() if k not in ROUTING_KEYS}
    config["source"] = candidate.get("source", default_source)

    try:
        if using == "python":
            fn = load_python_step(config.get("module"), config.get("function", "extract"))
            return fn(email, config, record)
        return get_extractor(using)(email, config, record)
    except ExtractionError:
        # A bad selector or pattern is a config mistake, not a miss -- it would
        # fail on every message, so falling through to the next candidate would
        # just hide it.
        raise
    except Exception as e:
        raise ExtractionError(f"{what} raised {type(e).__name__}: {e}") from e


# Registering the builtins. Imported for the side effect, at the bottom so the
# decorator above is defined by the time they run.
from extractors import attachment_pdf, css, regex  # noqa: E402,F401
