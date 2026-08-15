#!/usr/bin/env python3
"""
Gmail PDF pipeline runner.

Runs one or more configured "pipelines" that:
  1. Search Gmail for messages matching a query (Gmail search syntax)
  2. Download PDF attachments from matching messages
  3. Save them into a structured folder, using a filename/path template
  4. Track processed message IDs so re-runs (e.g. a daily cron job) are
     incremental -- only new messages get scanned/processed after the
     first run.

Usage:
    python attachments_downloader.py                  # run all pipelines in the config
    python attachments_downloader.py --pipeline NAME   # run just one pipeline
    python attachments_downloader.py --config configs/other.yaml
    python attachments_downloader.py --dry-run         # report what would be saved
    python attachments_downloader.py --reauth          # sign in again, replacing token.json
"""

import argparse
import base64
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path

import yaml
from google.auth.exceptions import GoogleAuthError, RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

try:
    from pypdf import PdfReader, PdfWriter
    from pypdf.errors import PyPdfError
except ImportError:  # only needed by pipelines that configure a password
    PdfReader = PdfWriter = None
    PyPdfError = Exception

# Read-only scope -- this pipeline never sends, deletes, or modifies mail.
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "configs"
DEFAULT_CONFIG_PATH = CONFIG_DIR / "attachments_config.yaml"
TOKEN_PATH = BASE_DIR / "token.json"
CREDENTIALS_PATH = BASE_DIR / "credentials.json"
STATE_DIR = BASE_DIR / "state"
LOG_PATH = BASE_DIR / "pipeline.log"

# Re-scan this many days before the last run on every run. This is a safety
# buffer -- it protects against timezone quirks and mail that was delayed in
# delivery -- and is safe because processed message IDs are deduped anyway.
OVERLAP_DAYS = 1

# Cap on how many processed IDs we keep per pipeline, so the state file
# doesn't grow forever. Comfortably larger than any realistic daily volume.
MAX_STORED_IDS = 5000

EPILOG = """\
examples:
  attachments_downloader.py                          run every pipeline in the config
  attachments_downloader.py --pipeline electric_bill run one pipeline by name
  attachments_downloader.py --dry-run                preview without downloading
  attachments_downloader.py --config configs/x.yaml  use a different config file
  attachments_downloader.py --reauth                 sign in again after a token expires

authorization:
  The first run opens a browser and caches the login in token.json, which
  later runs (cron included) reuse. If Google stops accepting it -- most
  often because the OAuth consent screen is still in "Testing" mode, where
  refresh tokens expire after 7 days -- the run stops and prints how to
  recover; `--reauth` is that recovery.

config file:
  Config files live in configs/; this runner defaults to
  configs/attachments_config.yaml (copy configs/attachments_config.yaml.example
  to start one). Each entry under `pipelines:` needs name, query, dest_folder and
  filename_template. `query` uses Gmail search syntax -- test it in the
  Gmail search bar first. A pipeline whose sender password-protects its
  PDFs may also set `passwords_env` (names of environment variables
  holding them) or `passwords` (plaintext); singular `password_env` and
  `password` work too. Each accepts one value or a list, and every
  candidate is tried in turn -- useful when a sender rotated its password
  and older files still need the old one. The saved copy is written
  unencrypted.

template variables:
  dest_folder and filename_template accept {year} {month} {day} {sender}
  {subject}; filename_template additionally accepts {orig_filename}.

state:
  Processed message IDs are stored per pipeline under state/, so re-runs
  only look at new mail. Delete a pipeline's state file to re-scan from
  scratch. --dry-run never touches these files.
"""


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_PATH),
            logging.StreamHandler(sys.stdout),
        ],
    )


class AuthError(Exception):
    """A Gmail authorization problem, carrying user-facing instructions.

    Raised in place of google-auth's own exceptions. Theirs are accurate
    ("invalid_grant: Token has been expired or revoked") but say nothing
    about what to do next, and they arrive as a traceback that buries the
    one line that matters. The message on this exception is meant to be
    printed as-is -- no traceback -- and to end with the exact command to
    run.
    """


def _boxed(title, body):
    rule = "-" * 74
    return "\n".join(["", rule, title, rule, body.rstrip(), rule])


def reauth_command():
    """The exact command line that re-runs the interactive sign-in.

    Uses sys.executable so the suggestion stays correct inside a virtualenv,
    where a bare `python` may be a different interpreter without the
    dependencies installed.
    """
    return f"{sys.executable or 'python'} {Path(__file__).name} --reauth"


def _google_reason(err):
    """Pull the readable string out of a google-auth exception."""
    args = getattr(err, "args", ())
    if args and isinstance(args[0], str):
        return args[0]
    return str(err)


def token_expired_message(err):
    return _boxed(
        "Gmail authorization has expired",
        f"""\
Google rejected the saved login in {TOKEN_PATH.name}:

    {_google_reason(err)}

The refresh token can no longer be exchanged for access. The usual causes:

  * The OAuth consent screen is still in "Testing" mode -- Google expires
    those refresh tokens after 7 days. This is by far the most common one.
  * Access was revoked at https://myaccount.google.com/permissions
  * The account password changed, or the token went ~6 months unused.

To fix it, sign in again from a terminal on this machine (a browser window
will open, and the new token is saved for future runs including cron):

    cd {BASE_DIR}
    {reauth_command()}

To stop this from recurring every 7 days, publish the app: Google Cloud
Console -> APIs & Services -> OAuth consent screen -> Publish app. It stays
private to your own account; publishing only lifts the test-mode expiry.""",
    )


def _unreadable_token_message(err):
    return _boxed(
        "Saved Gmail login could not be read",
        f"""\
{TOKEN_PATH} exists but is not a usable token file:

    {err}

It was probably truncated or edited by hand. Delete it and sign in again:

    rm {TOKEN_PATH}
    cd {BASE_DIR}
    {reauth_command()}""",
    )


def _missing_credentials_message():
    return _boxed(
        "Missing OAuth client file",
        f"""\
Signing in needs {CREDENTIALS_PATH.name}, which is not at:

    {CREDENTIALS_PATH}

Download an OAuth client ID of type "Desktop app" from Google Cloud Console
(APIs & Services -> Credentials), rename the downloaded JSON file to
{CREDENTIALS_PATH.name}, and save it at that path. README.md section 1 has
the full walkthrough.""",
    )


def _non_interactive_message(why):
    return _boxed(
        "Gmail sign-in is needed, but this run cannot open a browser",
        f"""\
{why}

Signing in requires a browser and a terminal you can interact with, so an
unattended run (cron, launchd, a script with no terminal) cannot do it.

Run this once yourself, in a terminal on this machine:

    cd {BASE_DIR}
    {reauth_command()}

The refreshed token is written to {TOKEN_PATH.name}, and the unattended runs
will pick it up on their own from then on.""",
    )


def get_gmail_service(force_reauth=False):
    """Authenticate and return a Gmail API service object.

    Uses a cached token (token.json) if available and valid. Otherwise runs
    an interactive OAuth consent flow once, using credentials.json (the
    OAuth client file downloaded from Google Cloud Console), and caches the
    resulting token for future runs -- including unattended cron runs.

    Every failure path here raises AuthError with instructions attached.
    """
    creds = None
    if TOKEN_PATH.exists() and not force_reauth:
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        except (ValueError, json.JSONDecodeError) as e:
            raise AuthError(_unreadable_token_message(e)) from e

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logging.info("Access token expired; refreshing it.")
            try:
                creds.refresh(Request())
            except RefreshError as e:
                raise AuthError(token_expired_message(e)) from e
        else:
            creds = run_consent_flow(force_reauth)
        TOKEN_PATH.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def run_consent_flow(force_reauth=False):
    """Run the interactive browser sign-in and return fresh credentials."""
    if not CREDENTIALS_PATH.exists():
        raise AuthError(_missing_credentials_message())

    if not sys.stdin.isatty():
        why = (
            "Re-authorization was requested with --reauth."
            if force_reauth
            else f"There is no saved login in {TOKEN_PATH.name} that can be refreshed."
        )
        raise AuthError(_non_interactive_message(why))

    logging.info("Opening a browser to authorize read-only Gmail access...")
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
    try:
        creds = flow.run_local_server(port=0)
    except GoogleAuthError as e:
        raise AuthError(
            _boxed(
                "Gmail sign-in did not complete",
                f"""\
The browser sign-in failed:

    {_google_reason(e)}

If the consent screen showed an "access blocked" or "app not verified"
error, check that your Google account is listed as a test user under
APIs & Services -> OAuth consent screen. Then try again:

    cd {BASE_DIR}
    {reauth_command()}""",
            )
        ) from e
    logging.info(f"Authorized. Token saved to {TOKEN_PATH}.")
    return creds


# Keys holding literal passwords, and keys naming environment variables that
# hold them. Every one accepts either a single value or a list of values.
PASSWORD_KEYS = ("password", "passwords")
PASSWORD_ENV_KEYS = ("password_env", "passwords_env")
ALL_PASSWORD_KEYS = PASSWORD_KEYS + PASSWORD_ENV_KEYS


class _RawStringLoader(yaml.SafeLoader):
    """SafeLoader that tags every plain scalar as a string.

    Passwords are often numeric, and YAML's implicit typing mangles them:
    unquoted `01234` is read as octal (668), `1_2345` as 12345, and `yes` as
    True. Password values are pulled from a parse using this loader so they
    survive as the literal text, quoted or not.
    """


_RawStringLoader.yaml_implicit_resolvers = {}


def load_config(path):
    with open(path, "r") as f:
        text = f.read()
    cfg = yaml.safe_load(text) or {}
    # Second parse, used only to recover password values verbatim.
    raw_cfg = yaml.load(text, Loader=_RawStringLoader) or {}

    pipelines = cfg.get("pipelines", [])
    raw_pipelines = raw_cfg.get("pipelines", [])

    for i, p in enumerate(pipelines):
        for required in ("name", "query", "dest_folder", "filename_template"):
            if required not in p:
                raise ValueError(f"Pipeline config missing required key '{required}': {p}")

        # Overlay password fields from the raw-string parse (same file, so the
        # entries line up by position).
        raw = raw_pipelines[i] if i < len(raw_pipelines) else {}
        for key in ALL_PASSWORD_KEYS:
            if key in p and key in raw:
                p[key] = raw[key]

        if any(key in p for key in ALL_PASSWORD_KEYS) and PdfReader is None:
            raise ImportError(
                f"Pipeline '{p['name']}' configures a PDF password, which requires "
                "pypdf. Install it with: pip install -r requirements.txt"
            )
    return pipelines


def as_list(value):
    """Normalize a config value that may be a single item or a list."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v is not None and str(v) != ""]
    return [str(value)] if str(value) != "" else []


def resolve_passwords(pipeline):
    """Return the ordered list of passwords to try for a pipeline.

    Environment-variable keys come first (they keep the secret out of the
    config file), then literal ones. Senders change their password scheme
    over time, so every configured value is a candidate and the decrypter
    tries them in order. Returns [] when the pipeline configures none.
    """
    candidates = []

    for key in PASSWORD_ENV_KEYS:
        for name in as_list(pipeline.get(key)):
            value = os.environ.get(name)
            if value:
                candidates.append(value)
            else:
                logging.warning(
                    f"  [{pipeline['name']}] {key} '{name}' is unset or empty; skipping it"
                )

    for key in PASSWORD_KEYS:
        candidates.extend(as_list(pipeline.get(key)))

    # Preserve order, drop duplicates.
    seen = set()
    ordered = [p for p in candidates if not (p in seen or seen.add(p))]

    if not ordered and any(key in pipeline for key in ALL_PASSWORD_KEYS):
        logging.warning(
            f"  [{pipeline['name']}] no usable password resolved; "
            "encrypted PDFs will be saved as-is"
        )
    return ordered


def state_path_for(pipeline_name):
    return STATE_DIR / f"{pipeline_name}.json"


def load_state(pipeline_name):
    path = state_path_for(pipeline_name)
    if path.exists():
        return json.loads(path.read_text())
    return {"last_run_date": None, "processed_message_ids": []}


def save_state(pipeline_name, state):
    STATE_DIR.mkdir(exist_ok=True)
    path = state_path_for(pipeline_name)
    path.write_text(json.dumps(state, indent=2))


def sanitize(value):
    """Make a string safe to use as a folder/file name component."""
    value = re.sub(r"[^\w\-. ]", "_", value).strip()
    return value or "unknown"


def build_query(pipeline, last_run_date):
    query_parts = [pipeline["query"]]
    if last_run_date:
        since = datetime.strptime(last_run_date, "%Y-%m-%d") - timedelta(days=OVERLAP_DAYS)
        query_parts.append(f"after:{since.strftime('%Y/%m/%d')}")
    return " ".join(query_parts)


def list_matching_messages(service, query):
    message_ids = []
    request = service.users().messages().list(userId="me", q=query)
    while request is not None:
        response = request.execute()
        message_ids.extend(m["id"] for m in response.get("messages", []))
        request = service.users().messages().list_next(request, response)
    return message_ids


def get_header(headers, name):
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def find_pdf_parts(payload):
    """Recursively find message parts that are PDF attachments."""
    found = []
    for part in payload.get("parts", []):
        filename = part.get("filename", "")
        mime_type = part.get("mimeType", "")
        is_pdf = filename.lower().endswith(".pdf") or mime_type == "application/pdf"
        if is_pdf and part.get("body", {}).get("attachmentId"):
            found.append(part)
        if "parts" in part:
            found.extend(find_pdf_parts(part))
    return found


def render_template(template, context):
    try:
        return template.format(**context)
    except KeyError as e:
        raise KeyError(f"Unknown template variable {e} in '{template}'")


def unique_path(path, reserved=()):
    """If path already exists, append _1, _2, ... before the extension.

    `reserved` holds paths that don't exist on disk yet but are already
    spoken for -- during a dry run nothing is actually written, so this is
    what keeps two attachments in the same run from both reporting the same
    destination.
    """
    if not path.exists() and path not in reserved:
        return path
    stem, suffix = path.stem, path.suffix
    i = 1
    while True:
        candidate = path.with_name(f"{stem}_{i}{suffix}")
        if not candidate.exists() and candidate not in reserved:
            return candidate
        i += 1


def decrypt_pdf(data, passwords, label):
    """Strip password protection from PDF bytes.

    `passwords` is an ordered list of candidates -- senders change their
    password scheme over time, so older files in the same pipeline may need
    an older password. Each is tried until one opens the file.

    Returns (bytes, status). On any failure the ORIGINAL bytes come back
    unchanged, so a wrong password or an unsupported encryption scheme
    still archives the attachment rather than losing it -- the status is
    logged loudly instead.
    """
    passwords = as_list(passwords)

    try:
        reader = PdfReader(BytesIO(data))
    except (PyPdfError, ValueError) as e:
        logging.error(f"  {label} could not be parsed as a PDF ({e}); left encrypted")
        return data, "unreadable"

    if not reader.is_encrypted:
        return data, "not encrypted"

    # The trailing empty password covers files that carry only an owner
    # password (restrictions on printing/copying, but none to open).
    attempts = list(passwords)
    if "" not in attempts:
        attempts.append("")

    for index, attempt in enumerate(attempts):
        try:
            if reader.decrypt(attempt):
                if index > 0 and attempt != "":
                    # Worth surfacing: the first password no longer works for
                    # this file, which usually means the sender rotated it.
                    logging.info(f"  {label} unlocked with password #{index + 1}")
                break
        except (PyPdfError, NotImplementedError) as e:
            logging.error(f"  {label} uses unsupported encryption ({e}); left encrypted")
            return data, "unsupported"
    else:
        count = len(passwords)
        plural = "password" if count == 1 else f"all {count} passwords"
        logging.error(f"  {label} was not unlocked by {plural}; left encrypted")
        return data, "wrong password"

    try:
        writer = PdfWriter(clone_from=reader)
        buf = BytesIO()
        writer.write(buf)
    except (PyPdfError, ValueError) as e:
        logging.error(f"  {label} unlocked but could not be rewritten ({e}); left encrypted")
        return data, "rewrite failed"

    return buf.getvalue(), "decrypted"


def process_message(service, pipeline, message_id, dry_run=False, reserved=None):
    prefix = "[dry-run] " if dry_run else ""
    msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    headers = msg["payload"].get("headers", [])
    subject = get_header(headers, "Subject")
    sender = get_header(headers, "From")

    internal_ts = int(msg["internalDate"]) / 1000
    msg_date = datetime.fromtimestamp(internal_ts)

    pdf_parts = find_pdf_parts(msg["payload"])
    if not pdf_parts:
        logging.info(
            f"  {prefix}[{pipeline['name']}] {message_id}: no PDF attachment found, skipping"
        )
        return

    passwords = resolve_passwords(pipeline)

    for part in pdf_parts:
        pdf_status = None
        if dry_run:
            # Skip the attachment fetch entirely -- the body metadata already
            # carries the size, which is all we need to report.
            data = None
            size = part.get("body", {}).get("size", 0)
        else:
            attachment_id = part["body"]["attachmentId"]
            attachment = (
                service.users()
                .messages()
                .attachments()
                .get(userId="me", messageId=message_id, id=attachment_id)
                .execute()
            )
            data = base64.urlsafe_b64decode(attachment["data"])
            if passwords:
                label = f"[{pipeline['name']}] {part.get('filename', 'attachment.pdf')}"
                data, pdf_status = decrypt_pdf(data, passwords, label)
            size = len(data)

        context = {
            "year": msg_date.strftime("%Y"),
            "month": msg_date.strftime("%m"),
            "day": msg_date.strftime("%d"),
            "sender": sanitize(sender),
            "subject": sanitize(subject),
            "orig_filename": part.get("filename", "attachment.pdf"),
        }

        dest_folder = Path(os.path.expanduser(render_template(pipeline["dest_folder"], context)))
        filename = render_template(pipeline["filename_template"], context)
        if not filename.lower().endswith(".pdf"):
            filename += ".pdf"

        if dry_run:
            dest_path = unique_path(dest_folder / filename, reserved=reserved or set())
            if reserved is not None:
                reserved.add(dest_path)
            # Size here is the encrypted size straight from the Gmail metadata;
            # a real run rewrites the file, so the saved size will differ.
            note = ", would remove password" if passwords else ""
            logging.info(
                f"  {prefix}[{pipeline['name']}] would save: {dest_path} "
                f"({size} bytes{note})"
            )
            continue

        dest_folder.mkdir(parents=True, exist_ok=True)
        dest_path = unique_path(dest_folder / filename)
        dest_path.write_bytes(data)
        note = f", {pdf_status}" if pdf_status else ""
        logging.info(f"  [{pipeline['name']}] saved: {dest_path} ({size} bytes{note})")


def run_pipeline(service, pipeline, dry_run=False):
    name = pipeline["name"]
    prefix = "[dry-run] " if dry_run else ""
    logging.info(f"{prefix}Running pipeline: {name}")
    state = load_state(name)
    query = build_query(pipeline, state["last_run_date"])
    logging.info(f"  query: {query}")

    try:
        message_ids = list_matching_messages(service, query)
    except HttpError as e:
        logging.error(f"  [{name}] Gmail API error while listing messages: {e}")
        return

    processed = set(state["processed_message_ids"])
    new_ids = [m for m in message_ids if m not in processed]
    logging.info(f"  {prefix}[{name}] {len(message_ids)} matched, {len(new_ids)} new")

    reserved = set() if dry_run else None
    for message_id in new_ids:
        try:
            process_message(service, pipeline, message_id, dry_run=dry_run, reserved=reserved)
            processed.add(message_id)
        except HttpError as e:
            logging.error(f"  [{name}] Gmail API error on message {message_id}: {e}")
        except RefreshError:
            # Authorization died mid-run; every remaining message would fail
            # the same way, so let main() report it once and stop.
            raise
        except Exception:
            logging.exception(f"  [{name}] Unexpected error on message {message_id}")

    if dry_run:
        # Leave the state file untouched so the next real run sees exactly the
        # same set of new messages this dry run just reported.
        logging.info(f"  {prefix}[{name}] state not updated")
        return

    state["processed_message_ids"] = list(processed)[-MAX_STORED_IDS:]
    state["last_run_date"] = datetime.now().strftime("%Y-%m-%d")
    save_state(name, state)


def main():
    parser = argparse.ArgumentParser(
        description="Download PDF attachments from Gmail into structured folders.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG,
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="Path to the pipeline config file (default: %(default)s)",
    )
    parser.add_argument("--pipeline", help="Run only the pipeline with this name")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be downloaded without writing any files or "
        "updating pipeline state. Attachments are never fetched.",
    )
    parser.add_argument(
        "--reauth",
        action="store_true",
        help="Ignore the saved token and sign in again, replacing token.json. "
        "Needs a browser, so run it from a terminal (not from cron).",
    )
    args = parser.parse_args()

    setup_logging()
    pipelines = load_config(args.config)

    if args.pipeline:
        pipelines = [p for p in pipelines if p["name"] == args.pipeline]
        if not pipelines:
            logging.error(f"No pipeline named '{args.pipeline}' found in {args.config}")
            sys.exit(1)

    if args.dry_run:
        logging.info("[dry-run] No files will be written and no state will be updated.")

    try:
        service = get_gmail_service(force_reauth=args.reauth)
        for pipeline in pipelines:
            run_pipeline(service, pipeline, dry_run=args.dry_run)
    except AuthError as e:
        # Already a finished, instructional message -- print it as-is rather
        # than letting a traceback bury it.
        logging.error(str(e))
        sys.exit(1)
    except RefreshError as e:
        # A token that looked valid at startup (no recorded expiry, say) can
        # still fail when the client refreshes it mid-run, on the first API
        # call. Same cause, so the same instructions apply.
        logging.error(token_expired_message(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
