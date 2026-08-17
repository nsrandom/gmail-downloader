"""Searching Gmail and normalizing a message into an `Email`."""

import base64
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import cached_property

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover -- Python < 3.9
    ZoneInfo = None

SOURCES = ("html", "html_text", "text", "subject", "headers")


class SourceUnavailable(Exception):
    """The message has no part of the requested kind."""


@dataclass
class Attachment:
    filename: str
    mime_type: str
    attachment_id: str
    size: int
    _service: object = field(default=None, repr=False)
    _message_id: str = field(default=None, repr=False)
    _data: bytes = field(default=None, repr=False)

    def read(self):
        """Fetch the attachment bytes, once."""
        if self._data is None:
            payload = (
                self._service.users()
                .messages()
                .attachments()
                .get(userId="me", messageId=self._message_id, id=self.attachment_id)
                .execute()
            )
            self._data = base64.urlsafe_b64decode(payload["data"])
        return self._data


@dataclass
class Email:
    id: str
    thread_id: str
    headers: dict
    date: datetime
    html: str = ""
    text: str = ""
    attachments: list = field(default_factory=list)

    @property
    def subject(self):
        return self.headers.get("subject", "")

    @property
    def sender(self):
        return self.headers.get("from", "")

    @property
    def to(self):
        return self.headers.get("to", "")

    @property
    def link(self):
        return f"https://mail.google.com/mail/u/0/#all/{self.id}"

    @cached_property
    def html_text(self):
        """The visible text of the HTML part.

        Worth having as its own source: senders label values with images and
        nested tables, so a regex over rendered text is often far steadier
        than one over the markup, and far more readable in config.
        """
        if not self.html:
            return ""
        from bs4 import BeautifulSoup
        return BeautifulSoup(self.html, "html.parser").get_text("\n", strip=True)

    def source(self, name):
        if name not in SOURCES:
            raise SourceUnavailable(
                f"unknown source '{name}' (expected one of {', '.join(SOURCES)})"
            )
        if name == "subject":
            return self.subject
        if name == "headers":
            return "\n".join(f"{k}: {v}" for k, v in self.headers.items())
        if name == "html_text":
            return self.html_text
        if name == "text":
            return self.text or self.html_text
        if not self.html:
            # Falling back rather than failing: plenty of senders post a text
            # part only, and a config saying `html` still means "the body".
            logging.debug(f"  {self.id}: no HTML part; reading the text part instead")
            return self.text
        return self.html


def _walk_parts(payload, out):
    mime = payload.get("mimeType", "")
    body = payload.get("body", {})
    if body.get("data"):
        decoded = base64.urlsafe_b64decode(body["data"]).decode("utf-8", "replace")
        if mime == "text/html" and not out["html"]:
            out["html"] = decoded
        elif mime == "text/plain" and not out["text"]:
            out["text"] = decoded
    if body.get("attachmentId"):
        out["attachments"].append(
            {
                "filename": payload.get("filename", ""),
                "mime_type": mime,
                "attachment_id": body["attachmentId"],
                "size": body.get("size", 0),
            }
        )
    for part in payload.get("parts", []):
        _walk_parts(part, out)


def _tz(name):
    if not name:
        return None
    if ZoneInfo is None:
        raise RuntimeError("zoneinfo is unavailable; set no timezone, or use Python 3.9+")
    return ZoneInfo(name)


def build_email(message, service=None, tz_name=None):
    headers = {h["name"].lower(): h["value"] for h in message["payload"].get("headers", [])}
    parts = {"html": "", "text": "", "attachments": []}
    _walk_parts(message["payload"], parts)

    # internalDate is the moment Gmail accepted the message, in UTC ms. It is
    # steadier than the Date: header, which senders get wrong.
    when = datetime.fromtimestamp(int(message["internalDate"]) / 1000, tz=timezone.utc)
    tz = _tz(tz_name)
    when = when.astimezone(tz) if tz else when.astimezone()

    return Email(
        id=message["id"],
        thread_id=message.get("threadId", ""),
        headers=headers,
        date=when,
        html=parts["html"],
        text=parts["text"],
        attachments=[
            Attachment(
                filename=a["filename"],
                mime_type=a["mime_type"],
                attachment_id=a["attachment_id"],
                size=a["size"],
                _service=service,
                _message_id=message["id"],
            )
            for a in parts["attachments"]
        ],
    )


def search(service, query, limit=None):
    ids = []
    request = service.users().messages().list(userId="me", q=query)
    while request is not None:
        response = request.execute()
        ids.extend(m["id"] for m in response.get("messages", []))
        if limit and len(ids) >= limit:
            return ids[:limit]
        request = service.users().messages().list_next(request, response)
    return ids


def fetch(service, message_id, tz_name=None):
    message = (
        service.users().messages().get(userId="me", id=message_id, format="full").execute()
    )
    return build_email(message, service=service, tz_name=tz_name)


def to_fixture(email):
    """The JSON side-car saved beside a fixture's HTML."""
    return {
        "id": email.id,
        "thread_id": email.thread_id,
        "date": email.date.isoformat(),
        "headers": email.headers,
        "attachments": [
            {"filename": a.filename, "mime_type": a.mime_type, "size": a.size}
            for a in email.attachments
        ],
    }


def from_fixture(meta_path, html_path=None, text_path=None):
    meta = json.loads(meta_path.read_text())
    return Email(
        id=meta["id"],
        thread_id=meta.get("thread_id", ""),
        headers=meta.get("headers", {}),
        date=datetime.fromisoformat(meta["date"]),
        html=html_path.read_text() if html_path and html_path.exists() else "",
        text=text_path.read_text() if text_path and text_path.exists() else "",
    )
