"""Extract the amount and due date from a PG&E statement email.

PG&E has sent this mail in three shapes, and old messages in the mailbox never
change to match the newest one, so all three have to keep working:

    prose   2023-01 .. 2023-10   "The amount of $162.62 for account number
                                  ******1234-5 is due on 02/03/2023."
    text    2023-10 .. 2024-10   "Statement balance:" / "Payment due date:"
                                  as text labels above the values
    image   2024-10 .. current   the same two labels, but as <img> tags with
                                  empty alt text -- there is no text to anchor
                                  on at all, only the table structure and the
                                  image filename

The layouts are tried newest first, since almost all mail this runs on is
recent. The first one that yields both values wins.

This started as `fallbacks:` in the config and moved here once it was three
layouts deep: the dates above are the sort of thing that belongs in a comment
next to the pattern, and YAML has nowhere good to put them.
"""

import logging
import re

from core.coerce import CoercionError, coerce
from extractors import ExtractionError

DATE_FORMAT = "%m/%d/%Y"

# "account ending in ******1234-5" in the newer layouts, "for account number
# ******1234-5" in the prose one. Optional -- a statement without it is still
# perfectly usable.
ACCOUNT_RE = r"account (?:ending in|number) \**([\w-]+)"

_AMOUNT = r"([\d,]+\.\d{2})"
_DATE = r"(\d{2}/\d{2}/\d{4})"


def _one(text, pattern):
    match = re.search(pattern, text)
    return match.group(1) if match else None


def _css_one(email, selector):
    from bs4 import BeautifulSoup

    element = BeautifulSoup(email.html or "", "html.parser").select_one(selector)
    return element.get_text(" ", strip=True) if element else None


def _image_labels(email):
    """Current layout. The labels are images, so the anchor is the filename."""
    return {
        "amount": _css_one(email, 'td:has(img[src*="amount-due"]) + td strong'),
        "due_date": _css_one(email, 'td:has(img[src*="due-date"]) + td strong'),
    }


def _plain_text(email):
    """The readable text, whichever part carried it."""
    return email.html_text or email.text


def _text_labels(email):
    """Values on the line after a text label."""
    text = _plain_text(email)
    return {
        "amount": _one(text, rf"Statement balance:\s*\$\s*{_AMOUNT}"),
        "due_date": _one(text, rf"Payment due date:\s*{_DATE}"),
    }


def _prose(email):
    """Both values in one sentence."""
    text = _plain_text(email)
    return {
        "amount": _one(text, rf"The amount of \${_AMOUNT}"),
        "due_date": _one(text, rf"is due on {_DATE}"),
    }


LAYOUTS = (
    ("image labels (Oct 2024 onwards)", _image_labels),
    ("text labels (Oct 2023 - Oct 2024)", _text_labels),
    ("prose sentence (to Oct 2023)", _prose),
)


def extract(email, config, record):
    """Return {amount, due_date, bill_date, account_no?} for a PG&E statement."""
    for index, (layout, read) in enumerate(LAYOUTS):
        found = read(email)
        if not found["amount"] or not found["due_date"]:
            continue

        try:
            values = {
                "amount": coerce(found["amount"], "money"),
                "due_date": coerce(found["due_date"], "date", DATE_FORMAT),
                # No layout states the statement date, but the mail goes out on
                # it. `email.date` is already in the configured timezone, so
                # taking the date off it does not slip a day.
                "bill_date": email.date.date(),
            }
        except CoercionError as e:
            # Matching the shape but not the content means this is the wrong
            # layout, not a broken message -- let the next one try.
            logging.debug(f"  PG&E {layout} matched but did not parse ({e})")
            continue

        if index:
            # Worth surfacing: the newest layout no longer matches, which for
            # recent mail would mean PG&E has changed something again.
            logging.info(f"  PG&E statement read as {layout}")

        account = _one(_plain_text(email), ACCOUNT_RE)
        if account:
            values["account_no"] = account
        return values

    raise ExtractionError(
        "no PG&E layout matched this message (tried: "
        + "; ".join(name for name, _ in LAYOUTS)
        + "). If they have redesigned again, save it with --dump-body and add "
        "a layout to extractors/pge_bill.py"
    )
