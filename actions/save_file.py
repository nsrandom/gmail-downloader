"""Append the extracted record to a file.

The awkward part of this sink is that the other two get duplicate protection
from the far end -- HTTP sends an `Idempotency-Key`, and Calendar rejects a
repeated event id. A file has nobody to ask. State normally prevents a second
write, but state can be deleted, hand-edited, or pruned, and the cost of being
wrong is a silently doubled row in a ledger you later add up.

So an appended row carries a `_key` of `<message_id>:<action_id>`, and the file
is scanned for that key before anything is written. It is a linear scan, which
is the right trade at the scale this runs at (a few hundred rows a year) and
means the file itself is the source of truth rather than the state directory.
"""

import csv
import io
import logging
import os
from pathlib import Path

import simplejson

from actions import ActionError, action
from core.coerce import json_safe
from core.redact import redact

KEY_FIELD = "_key"

FORMATS = ("jsonl", "csv", "text")
_BY_SUFFIX = {
    ".jsonl": "jsonl", ".ndjson": "jsonl", ".json": "jsonl",
    ".csv": "csv",
    ".txt": "text", ".log": "text", ".md": "text",
}


def _resolve_format(config, path):
    fmt = config.get("format")
    if fmt:
        if fmt not in FORMATS:
            raise ActionError(f"unknown format '{fmt}' (expected {', '.join(FORMATS)})")
        return fmt
    return _BY_SUFFIX.get(path.suffix.lower(), "jsonl")


def _build_row(record, config):
    """What to write: an explicit mapping, a subset, or the whole record."""
    if "values" in config:
        return dict(config["values"])
    if "fields" in config:
        missing = [f for f in config["fields"] if f not in record]
        if missing:
            raise ActionError(
                f"`fields` names {', '.join(missing)}, which the extract step did not produce"
            )
        return {f: record[f] for f in config["fields"]}
    return dict(record)


def _already_written(path, fmt, key):
    """Has this exact message+action already been appended?"""
    if not path.exists():
        return False
    if fmt == "jsonl":
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    if simplejson.loads(line).get(KEY_FIELD) == key:
                        return True
                except ValueError:
                    continue  # a hand-edited line should not break the run
    elif fmt == "csv":
        with path.open(newline="") as f:
            for row in csv.DictReader(f):
                if row.get(KEY_FIELD) == key:
                    return True
    return False


def _csv_line(row, columns):
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writerow(row)
    return buffer.getvalue()


def _csv_columns(path, row, append):
    """Column order, and a loud error rather than silently misaligned data."""
    if append and path.exists() and path.stat().st_size > 0:
        with path.open(newline="") as f:
            existing = next(csv.reader(f), [])
        extra = [k for k in row if k not in existing]
        if extra:
            raise ActionError(
                f"{path.name} already has columns {existing}, but this row adds "
                f"{extra}. Add `fields:` to pin the columns, or move the old file aside."
            )
        return existing, False
    return list(row), True


@action("file")
def run(record, config, context, dry_run=False):
    raw_path = config.get("path")
    if not raw_path:
        raise ActionError("a `file` action needs a `path`")

    path = Path(os.path.expanduser(str(raw_path)))
    fmt = _resolve_format(config, path)
    append = str(config.get("mode", "append")).lower() == "append"
    money_as = config.get("money_format", "number")

    if fmt == "text":
        line = config.get("line")
        if line is None:
            raise ActionError("a `text` file action needs a `line:` template")
        payload = str(line).rstrip("\n") + "\n"
        # Nothing in a free-text line is reliably a key, so state is the only
        # protection here. Said out loud rather than pretended otherwise.
        dedupe = False
        row = None
    else:
        row = json_safe(_build_row(record, config), money_as)
        dedupe = bool(config.get("dedupe", True)) and append
        key = f"{context['email'].id}:{context.get('action_id', 'action')}"
        if dedupe or config.get("include_key", True):
            row = {**row, KEY_FIELD: key}
        payload = None

    if dry_run:
        preview = payload if row is None else simplejson.dumps(
            redact(row), use_decimal=True, default=str
        )
        logging.info(f"  [dry-run] would {'append to' if append else 'write'} {path}: "
                     f"{preview.strip()}")
        return {"path": str(path), "written": False, "dry_run": True}

    if dedupe and _already_written(path, fmt, row[KEY_FIELD]):
        logging.info(f"  {path.name} already has this message; leaving it alone")
        return {"path": str(path), "written": False, "duplicate": True}

    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"

    if fmt == "jsonl":
        payload = simplejson.dumps(row, use_decimal=True, default=str, sort_keys=True) + "\n"
        with path.open(mode) as f:
            f.write(payload)
    elif fmt == "csv":
        columns, write_header = _csv_columns(path, row, append)
        with path.open(mode, newline="") as f:
            if write_header:
                f.write(_csv_line({c: c for c in columns}, columns))
            f.write(_csv_line(row, columns))
    else:
        with path.open(mode) as f:
            f.write(payload)

    logging.info(f"  {'appended to' if append else 'wrote'} {path}")
    return {"path": str(path), "written": True, "format": fmt}
