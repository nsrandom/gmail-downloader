"""Pull a value out of a message with a regular expression."""

import re

from extractors import ExtractionError, extractor

_FLAGS = {
    "i": re.I, "ignorecase": re.I,
    "m": re.M, "multiline": re.M,
    "s": re.S, "dotall": re.S,
    "x": re.X, "verbose": re.X,
}


def _compile(config):
    pattern = config.get("pattern")
    if not pattern:
        raise ExtractionError("a `regex` step needs a `pattern`")
    flags = 0
    for name in str(config.get("flags", "")).replace(",", " ").split():
        if name.lower() not in _FLAGS:
            raise ExtractionError(f"unknown regex flag '{name}'")
        flags |= _FLAGS[name.lower()]
    try:
        return re.compile(pattern, flags)
    except re.error as e:
        raise ExtractionError(f"bad pattern {pattern!r}: {e}") from e


@extractor("regex")
def extract(email, config, record):
    """Return the matched group, every match with `all: true`, or a dict of
    named groups when the pattern has them and the step has no `name`."""
    compiled = _compile(config)
    text = email.source(config["source"])

    if config.get("all"):
        return compiled.findall(text)

    match = compiled.search(text)
    if not match:
        return None

    if compiled.groupindex and config.get("group") is None:
        named = match.groupdict()
        # A single named group behaves like a plain capture, so `name:` still
        # works; several are only meaningful as a merged dict.
        return named if len(named) > 1 else next(iter(named.values()))

    group = config.get("group", 1 if compiled.groups else 0)
    try:
        return match.group(group)
    except (IndexError, re.error) as e:
        raise ExtractionError(
            f"pattern has {compiled.groups} capture groups; asked for group {group}"
        ) from e
