"""Extract the home value estimate from a Redfin home report email.

Redfin has redesigned this mail at least four times since 2017, and the wording
around the number changes every time:

    2017-2020   "<Month> <Year> Home Report / <address> / $1,250,000 /
                 Redfin Estimate"          -- the label follows the value
    2021, 2025+ "Your Home Estimate / <address> / $1,610,500"
    2022-2024   "<MONTH YEAR> / Your Home Report / <ADDRESS> /
                 QUALIFIES FOR REDFIN PREMIER / $1,875,400"

Rather than one reader per layout -- which would need a fifth the next time
they redesign -- this leans on the one thing all four have in common: the
address appears, and the estimate is the first plausible money amount after it.
Everything else about the page moves around.

Two guards keep that from turning into "grab any number": the search stops
after `window` characters, and the value has to fall inside `min_value` ..
`max_value`. Without them the $974 median-price-per-square-foot further down
the page would be a candidate.

The address is taken from the subject line ("<address> Home Report -- 27 nearby
homes listed"), so this module has nothing property-specific baked into it and
works for a second address without editing.
"""

import logging
import re
from datetime import date

from core.coerce import CoercionError, coerce
from extractors import ExtractionError

# The estimate is always written with thousands separators; requiring one is
# what keeps "3 Beds 2 Baths" and similar out of the running.
MONEY_RE = re.compile(r"\$\s*(\d{1,3}(?:,\d{3})+)(?:\.\d{2})?")

MONTH_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+(\d{4})\b",
    re.I,
)

MONTHS = ["january", "february", "march", "april", "may", "june", "july",
          "august", "september", "october", "november", "december"]

SUBJECT_ADDRESS_RE = re.compile(r"\s*(.+?)\s+home report", re.I)

# How far past the address to keep looking, and what counts as a believable
# estimate. All three are overridable from the step's config.
DEFAULT_WINDOW = 400
DEFAULT_MIN = 100_000
DEFAULT_MAX = 100_000_000


def _address(email, config):
    if config.get("address"):
        return config["address"]
    match = SUBJECT_ADDRESS_RE.match(email.subject or "")
    if not match:
        raise ExtractionError(
            f"cannot tell which property this is: subject {email.subject!r} is not "
            f"'<address> Home Report ...'. Set `address:` on the step to say explicitly."
        )
    return match.group(1)


def _report_month(text, before, email):
    """The month the report covers.

    Stated near the top in every layout so far. Falling back to the month the
    mail arrived keeps the series unbroken if a future one drops it -- these
    are sent within the month they cover.
    """
    match = MONTH_RE.search(text[:before + 200])
    if match:
        return date(int(match.group(2)), MONTHS.index(match.group(1).lower()) + 1, 1)
    logging.debug("  Redfin report does not state its month; using the email's")
    return date(email.date.year, email.date.month, 1)


def extract(email, config, record):
    """Return {address, report_month, estimate} for a Redfin home report."""
    address = _address(email, config)
    text = email.html_text or email.text
    window = int(config.get("window", DEFAULT_WINDOW))
    low = int(config.get("min_value", DEFAULT_MIN))
    high = int(config.get("max_value", DEFAULT_MAX))

    start = text.lower().find(address.lower())
    if start < 0:
        raise ExtractionError(
            f"the address {address!r} does not appear in the body, so there is no "
            f"way to tell which of this email's many prices is the estimate"
        )

    rejected = []
    for match in MONEY_RE.finditer(text, start, start + window):
        try:
            value = coerce(match.group(1), "money")
        except CoercionError:
            continue
        if low <= value <= high:
            return {
                "address": address,
                "report_month": _report_month(text, start, email),
                "estimate": value,
            }
        rejected.append(match.group(0))

    detail = f" (ignored {', '.join(rejected)} as out of range)" if rejected else ""
    raise ExtractionError(
        f"no home estimate within {window} characters of {address!r}{detail}. "
        f"If Redfin has redesigned again, check the layout with --explain and "
        f"widen `window:` or adjust extractors/redfin_home_report.py"
    )
