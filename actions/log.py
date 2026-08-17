"""Write the record to the log. Useful while a pipeline is being developed."""

import logging

import simplejson

from actions import action
from core.coerce import json_safe
from core.redact import redact


@action("log")
def run(record, config, context, dry_run=False):
    payload = config.get("message") or json_safe(record)
    level = getattr(logging, str(config.get("level", "info")).upper(), logging.INFO)
    if isinstance(payload, str):
        logging.log(level, f"  {payload}")
    else:
        logging.log(level, "  " + simplejson.dumps(redact(payload), use_decimal=True, indent=2))
    return {"logged": True}
