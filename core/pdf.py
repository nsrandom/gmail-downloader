"""Reading text out of a PDF attachment, unlocking it first if need be.

The unlocking logic mirrors `attachments_downloader.decrypt_pdf`: senders
rotate their password scheme, so every configured candidate is tried in order.
That script keeps its own copy for now and folds onto this one later.

Extraction is pypdf's, which reads a text layer and not a scan. A scanned bill
needs OCR, which is out of scope.
"""

import logging
import os
from io import BytesIO

try:
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError
except ImportError:  # pragma: no cover
    PdfReader = None
    PyPdfError = Exception


class PdfError(Exception):
    pass


def resolve_passwords(config, label=""):
    """Ordered password candidates from a step's config.

    Environment-variable keys come first, so the secret can stay out of the
    config file.
    """
    candidates = []
    for key in ("password_env", "passwords_env"):
        for name in _as_list(config.get(key)):
            value = os.environ.get(name)
            if value:
                candidates.append(value)
            else:
                logging.warning(f"  {label}{key} '{name}' is unset or empty; skipping it")
    for key in ("password", "passwords"):
        candidates.extend(_as_list(config.get(key)))

    seen = set()
    return [p for p in candidates if not (p in seen or seen.add(p))]


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v is not None and str(v) != ""]
    return [str(value)] if str(value) != "" else []


def extract_text(data, passwords=(), pages=None, label=""):
    """Return the text of a PDF, or raise PdfError."""
    if PdfReader is None:
        raise PdfError("reading PDF attachments needs pypdf: pip install -r requirements.txt")

    try:
        reader = PdfReader(BytesIO(data))
    except (PyPdfError, ValueError) as e:
        raise PdfError(f"{label}not readable as a PDF ({e})") from e

    if reader.is_encrypted:
        attempts = list(passwords)
        if "" not in attempts:
            # Covers files carrying only an owner password -- restrictions on
            # printing or copying, but none on opening.
            attempts.append("")
        for index, attempt in enumerate(attempts):
            try:
                if reader.decrypt(attempt):
                    if index > 0 and attempt != "":
                        logging.info(f"  {label}unlocked with password #{index + 1}")
                    break
            except (PyPdfError, NotImplementedError) as e:
                raise PdfError(f"{label}uses unsupported encryption ({e})") from e
        else:
            count = len(passwords)
            which = "no password was configured" if not count else (
                "the configured password did not work" if count == 1
                else f"none of the {count} configured passwords worked"
            )
            raise PdfError(f"{label}is encrypted and {which}")

    selected = reader.pages
    if pages is not None:
        wanted = pages if isinstance(pages, (list, tuple)) else [pages]
        try:
            selected = [reader.pages[int(p)] for p in wanted]
        except IndexError as e:
            raise PdfError(f"{label}has {len(reader.pages)} pages; asked for {wanted}") from e

    try:
        return "\n".join(page.extract_text() or "" for page in selected)
    except (PyPdfError, ValueError) as e:
        raise PdfError(f"{label}text could not be extracted ({e})") from e
