"""Create a Google Calendar event from a record.

Duplicate protection uses a deterministic *event id* rather than the iCalUID
the design doc first named: the Calendar API only accepts iCalUID through
`events.import`, while `events.insert` takes a caller-supplied `id` and answers
a repeat with 409. That 409 is exactly the outcome we want, so it counts as
success -- the event is already there.

Event ids must be base32hex (0-9, a-v), which is why the idempotency key gets
re-encoded rather than used as the hex digest it starts as.
"""

import base64
import logging
from datetime import date, datetime, timedelta

from googleapiclient.errors import HttpError

from actions import ActionError, action

DEFAULT_DURATION_MINUTES = 30


def _event_id(idempotency_key):
    digest = bytes.fromhex(idempotency_key)
    return base64.b32hexencode(digest).decode("ascii").rstrip("=").lower()


def _as_date(value, what):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).date()
        except ValueError as e:
            raise ActionError(f"{what} {value!r} is not a date: {e}") from e
    raise ActionError(f"{what} must be a date, got {type(value).__name__}")


def _as_datetime(value, what):
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, 9, 0)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError as e:
            raise ActionError(f"{what} {value!r} is not a datetime: {e}") from e
    raise ActionError(f"{what} must be a datetime, got {type(value).__name__}")


def _timing(config):
    timezone = config.get("timezone")

    if config.get("all_day") or config.get("start_date"):
        start = _as_date(config.get("start_date") or config.get("start_datetime"), "start_date")
        end = start + timedelta(days=int(config.get("duration_days", 1)))
        return {"start": {"date": start.isoformat()}, "end": {"date": end.isoformat()}}

    if not config.get("start_datetime"):
        raise ActionError("needs `start_date` (with `all_day`) or `start_datetime`")

    start = _as_datetime(config["start_datetime"], "start_datetime")
    end = start + timedelta(minutes=int(config.get("duration_minutes",
                                                   DEFAULT_DURATION_MINUTES)))
    # Google rejects a timed event whose start carries neither a UTC offset nor
    # a `timeZone`, and the common case here produces exactly that: a `type:
    # date` field becomes a naive 09:00. `defaults.timezone` normally fills
    # this in, so say so rather than letting the API answer with a 400.
    if start.tzinfo is None and not timezone:
        raise ActionError(
            f"start_datetime {start.isoformat()} has no time zone. Set "
            f"`defaults.timezone:` in the config, or `timezone:` on this action."
        )
    block = {
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
    }
    if timezone:
        block["start"]["timeZone"] = timezone
        block["end"]["timeZone"] = timezone
    return block


@action("google_calendar")
def run(record, config, context, dry_run=False):
    services = context.get("services")
    calendar_id = config.get("calendar_id", "primary")

    body = {
        "summary": config.get("summary", "(no summary)"),
        **_timing(config),
    }
    for key in ("description", "location"):
        if config.get(key):
            body[key] = config[key]
    if config.get("reminders"):
        body["reminders"] = {
            "useDefault": False,
            "overrides": [
                {"method": r.get("method", "popup"), "minutes": int(r["minutes"])}
                for r in config["reminders"]
            ],
        }
    if context.get("idempotency_key"):
        body["id"] = _event_id(context["idempotency_key"])

    when = body.get("start", {}).get("date") or body.get("start", {}).get("dateTime")
    if dry_run:
        logging.info(
            f"  [dry-run] would create calendar event on {when} "
            f"in '{calendar_id}': {body['summary']}"
        )
        return {"id": body.get("id"), "dry_run": True}

    if services is None:
        raise ActionError("no Google credentials available for the calendar action")

    try:
        event = services.calendar.events().insert(calendarId=calendar_id, body=body).execute()
    except HttpError as e:
        if e.resp.status == 409:
            logging.info(f"  calendar event for {when} already exists; leaving it alone")
            return {"id": body.get("id"), "duplicate": True}
        raise ActionError(f"calendar insert failed: {e}") from e

    logging.info(f"  created calendar event {event.get('id')} on {when}")
    return {
        "id": event.get("id"),
        "html_link": event.get("htmlLink"),
        "status": event.get("status"),
    }
