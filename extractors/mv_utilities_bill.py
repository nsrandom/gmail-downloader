"""Extract the amount and due date from a City of Mountain View utility bill.

The city bills water, sewer, and refuse every two months, and Paymentus sends
the notice. Over eleven years of these the wording has moved twice and the
currency symbol once, but the sentence structure never has:

    2015-02 .. 2017-04   "Total of your current bill: $179.05"
    2017-06 .. 2023-08   "Total amount: $305.02"
    2023-10 .. current   "Total amount: 446.50"   -- Paymentus rebuilt the
                         template around a table and dropped the "$"

"Bill due date: Mar 11, 2015" and "account number: 604056400003" have survived
all three, so unlike extractors/pge_bill.py this needs no reader per layout:
one pattern per field, tolerant about the label and the symbol, covers the lot.
The dates above are here so a future change can be dated against them.

Both labels sit in a `<b>` in the current template and a `<strong>` in the old
one, and the values are separated from the label by &nbsp; since 2023, so this
reads the rendered text rather than the markup -- `\\s` matches a non-breaking
space, CSS selectors would need two spellings.
"""

import logging
import re

from core.coerce import coerce
from extractors import ExtractionError

# "Jan 9, 2019" and "Jan 08, 2025" both appear; %d takes either. No mail so far
# spells the month out, but it costs one tuple entry to survive one that does.
DATE_FORMATS = ("%b %d, %Y", "%B %d, %Y")

# Requiring the cents is what keeps the account number -- the only other long
# run of digits in the mail -- from being read as an amount now that the
# newest layout states no "$".
AMOUNT_RE = re.compile(
    r"Total (?:amount|of your current bill):\s*\$?\s*([\d,]+\.\d{2})", re.I
)
DUE_DATE_RE = re.compile(r"Bill due date:\s*([A-Za-z]{3,9}\s+\d{1,2},\s*\d{4})", re.I)

# The colon went away with the 2023 redesign. Optional: a bill without it is
# still perfectly usable, and this household has had two account numbers.
ACCOUNT_RE = re.compile(r"account number:?\s*(\d[\d-]*)", re.I)

# Only the pre-2017 mail says this, and it is worth a line in the log if a
# recent message does: it would mean the template has been reverted or
# replaced, which is the sort of thing to notice before the amounts drift.
OLD_LABEL_RE = re.compile(r"Total of your current bill:", re.I)


def _one(text, pattern):
    match = pattern.search(text)
    return match.group(1) if match else None


def _due_date(value):
    for fmt in DATE_FORMATS:
        try:
            return coerce(value, "date", fmt)
        except Exception:  # CoercionError: this spelling of the month, not this date
            continue
    raise ExtractionError(
        f"cannot read {value!r} as a due date (tried {', '.join(DATE_FORMATS)}). "
        f"Add the new spelling to DATE_FORMATS in extractors/mv_utilities_bill.py"
    )


def extract(email, config, record):
    """Return {amount, due_date, bill_date, account_no?} for a Mountain View bill."""
    text = email.html_text or email.text

    amount = _one(text, AMOUNT_RE)
    due_date = _one(text, DUE_DATE_RE)
    if not amount or not due_date:
        missing = ", ".join(n for n, v in (("amount", amount), ("due date", due_date)) if not v)
        raise ExtractionError(
            f"no {missing} in this Mountain View bill. If Paymentus has changed the "
            f"template again, save it with --dump-body and adjust the patterns in "
            f"extractors/mv_utilities_bill.py"
        )

    if OLD_LABEL_RE.search(text):
        logging.debug("  Mountain View bill uses the pre-2017 'Total of your current bill'")

    values = {
        "amount": coerce(amount, "money"),
        "due_date": _due_date(due_date),
        # The mail states no statement date, but it goes out on it. `email.date`
        # is already in the configured timezone, so taking the date off it does
        # not slip a day -- these arrive just after midnight local.
        "bill_date": email.date.date(),
    }

    account = _one(text, ACCOUNT_RE)
    if account:
        values["account_no"] = account
    return values
