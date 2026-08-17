"""POST a record to an API.

The generic sink. It talks to APIs you own or subscribe to; this project
defines none of them and assumes nothing about their shape. Every action names
its own destination -- a `target`, a different `path` on the same target, or an
absolute `url` -- so one pipeline can fan out to unrelated services.
"""

import logging
import time

import requests
import simplejson

from actions import ActionError, action
from core.coerce import json_safe
from core.redact import redact

RETRY_STATUSES = (429, 500, 502, 503, 504)
DEFAULT_RETRY = {"attempts": 3, "backoff_seconds": 2}


def _url(config):
    if config.get("url"):
        return config["url"]
    base = (config.get("base_url") or "").rstrip("/")
    path = config.get("path") or ""
    if not base:
        raise ActionError("needs either an absolute `url` or a target with a `base_url`")
    return f"{base}/{path.lstrip('/')}" if path else base


def _acceptable(status, expect):
    if expect is None:
        return 200 <= status < 300
    wanted = expect if isinstance(expect, (list, tuple)) else [expect]
    for w in wanted:
        if str(w).lower() == "2xx":
            if 200 <= status < 300:
                return True
            continue
        try:
            if status == int(w):
                return True
        except (TypeError, ValueError) as e:
            raise ActionError(f"expect_status {w!r} is not a status code or '2xx'") from e
    return False


@action("http")
def run(record, config, context, dry_run=False):
    url = _url(config)
    method = str(config.get("method", "POST")).upper()
    timeout = config.get("timeout", 20)
    money_as = config.get("money_format", "number")
    headers = dict(config.get("headers") or {})

    body = None
    kwargs = {"timeout": timeout, "params": config.get("query")}
    if "json" in config:
        body = json_safe(config["json"], money_as)
        # simplejson, not the stdlib: it writes a Decimal as an exact number,
        # so 153.13 never becomes 153.13000000000001 via a binary float.
        kwargs["data"] = simplejson.dumps(body, use_decimal=True)
        headers.setdefault("Content-Type", "application/json")
    elif "form" in config:
        kwargs["data"] = json_safe(config["form"], "string")
    elif "body" in config:
        kwargs["data"] = config["body"]

    if context.get("idempotency_key"):
        headers.setdefault("Idempotency-Key", context["idempotency_key"])
    kwargs["headers"] = headers

    if dry_run:
        logging.info(
            f"  [dry-run] would {method} {url}"
            + (f"\n            body: {simplejson.dumps(redact(body), use_decimal=True)}"
               if body is not None else "")
        )
        return {"status": None, "response": None, "dry_run": True}

    retry = {**DEFAULT_RETRY, **(config.get("retry") or {})}
    attempts = max(1, int(retry["attempts"]))
    last_error = None

    for attempt in range(1, attempts + 1):
        try:
            response = requests.request(method, url, **kwargs)
        except requests.RequestException as e:
            last_error = f"{type(e).__name__}: {e}"
            if attempt == attempts:
                break
            time.sleep(retry["backoff_seconds"] * attempt)
            continue

        if _acceptable(response.status_code, config.get("expect_status")):
            try:
                parsed = response.json()
            except ValueError:
                parsed = response.text
            logging.info(f"  {method} {url} -> {response.status_code}")
            return {
                "status": response.status_code,
                "response": parsed,
                "headers": dict(response.headers),
            }

        last_error = f"HTTP {response.status_code}: {response.text[:300]}"
        # 4xx is the server saying the request itself is wrong; sending it
        # again unchanged just repeats the mistake.
        if response.status_code not in RETRY_STATUSES or attempt == attempts:
            break
        time.sleep(retry["backoff_seconds"] * attempt)

    raise ActionError(f"{method} {url} failed after {attempt} attempt(s): {last_error}")
