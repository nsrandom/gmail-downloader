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
    python gmail_pipeline.py                  # run all pipelines in config.yaml
    python gmail_pipeline.py --pipeline NAME   # run just one pipeline
    python gmail_pipeline.py --config other.yaml
    python gmail_pipeline.py --dry-run         # report what would be saved
"""

import argparse
import base64
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import yaml
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Read-only scope -- this pipeline never sends, deletes, or modifies mail.
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "config.yaml"
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
  gmail_pipeline.py                          run every pipeline in config.yaml
  gmail_pipeline.py --pipeline electric_bill run one pipeline by name
  gmail_pipeline.py --dry-run                preview without downloading
  gmail_pipeline.py --config other.yaml      use a different config file

config file:
  Each entry under `pipelines:` needs name, query, dest_folder and
  filename_template. `query` uses Gmail search syntax -- test it in the
  Gmail search bar first.

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


def get_gmail_service():
    """Authenticate and return a Gmail API service object.

    Uses a cached token (token.json) if available and valid. Otherwise runs
    an interactive OAuth consent flow once, using credentials.json (the
    OAuth client file downloaded from Google Cloud Console), and caches the
    resulting token for future runs -- including unattended cron runs.
    """
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_PATH.exists():
                raise FileNotFoundError(
                    f"Missing {CREDENTIALS_PATH}. Download an OAuth client ID "
                    "(Desktop app type) from Google Cloud Console and save it "
                    "at this path. See README.md for the full setup steps."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def load_config(path):
    with open(path, "r") as f:
        cfg = yaml.safe_load(f)
    pipelines = cfg.get("pipelines", [])
    for p in pipelines:
        for required in ("name", "query", "dest_folder", "filename_template"):
            if required not in p:
                raise ValueError(f"Pipeline config missing required key '{required}': {p}")
    return pipelines


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

    for part in pdf_parts:
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
            logging.info(
                f"  {prefix}[{pipeline['name']}] would save: {dest_path} ({size} bytes)"
            )
            continue

        dest_folder.mkdir(parents=True, exist_ok=True)
        dest_path = unique_path(dest_folder / filename)
        dest_path.write_bytes(data)
        logging.info(f"  [{pipeline['name']}] saved: {dest_path} ({size} bytes)")


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

    service = get_gmail_service()
    for pipeline in pipelines:
        run_pipeline(service, pipeline, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
