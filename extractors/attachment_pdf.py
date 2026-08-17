"""Pull a value out of a PDF attachment.

Often a steadier target than the HTML wrapped around it: a sender redesigns
the email far more readily than the statement it links to. Accepts the same
`password` / `passwords_env` keys as the attachments config, so an encrypted
statement can be read without being written to disk anywhere.
"""

import fnmatch
import re

from core.pdf import PdfError, extract_text, resolve_passwords
from extractors import ExtractionError, extractor


def _select(email, config):
    """The attachment to read: the first name match, else the first PDF.

    The name has to win outright. A statement mail often carries an insert or
    a marketing leaflet as well, and matching on `application/pdf` alongside
    the pattern would quietly read whichever happened to be attached first --
    making `filename_match` look like it worked while ignoring it.
    """
    pattern = config.get("filename_match")
    for attachment in email.attachments:
        if fnmatch.fnmatch((attachment.filename or "").lower(), (pattern or "*.pdf").lower()):
            return attachment

    if pattern:
        # An explicit pattern that matched nothing is an answer, not an
        # invitation to guess at the other attachments.
        return None
    return next((a for a in email.attachments if a.mime_type == "application/pdf"), None)


@extractor("attachment_pdf")
def extract(email, config, record):
    attachment = _select(email, config)
    if attachment is None:
        return None

    label = f"[{attachment.filename or 'attachment.pdf'}] "
    passwords = resolve_passwords(config, label)

    try:
        text = extract_text(attachment.read(), passwords, config.get("page"), label)
    except PdfError as e:
        raise ExtractionError(str(e)) from e

    pattern = config.get("pattern")
    if not pattern:
        # No pattern means the step wants the whole text, usually to hand to a
        # later python step.
        return text

    flags = re.S if config.get("dotall", True) else 0
    match = re.search(pattern, text, flags)
    if not match:
        return None
    if match.groupdict():
        named = match.groupdict()
        return named if len(named) > 1 else next(iter(named.values()))
    return match.group(config.get("group", 1 if match.re.groups else 0))
