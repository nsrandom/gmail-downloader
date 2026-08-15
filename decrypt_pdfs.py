#!/usr/bin/env python3
"""
Decrypt PDFs that were already saved to disk.

The downloader strips passwords as it saves, but only with whatever
passwords were configured at the time -- so files archived before a
password was added (or before an older one was discovered) are still
sitting there encrypted. Their message IDs are recorded in the pipeline
state, so re-running the downloader will not revisit them.

This script fixes those in place instead: for each pipeline it walks the
pipeline's dest_folder, and rewrites every encrypted PDF it can open with
that pipeline's configured passwords. Files that are already readable are
left untouched, and a file that cannot be unlocked is never modified.

Usage:
    python decrypt_pdfs.py                    # every pipeline in the config
    python decrypt_pdfs.py --dry-run          # report only, change nothing
    python decrypt_pdfs.py --pipeline NAME    # just one pipeline
"""

import argparse
import logging
import os
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

from attachments_downloader import (
    DEFAULT_CONFIG_PATH,
    decrypt_pdf,
    load_config,
    resolve_passwords,
    setup_logging,
)

# decrypt_pdf statuses that mean the file on disk is fine as it stands.
OK_STATUSES = ("decrypted", "not encrypted")

STATUS_LABELS = {
    "decrypted": "decrypted",
    "not encrypted": "already readable",
    "wrong password": "no configured password worked",
    "unsupported": "unsupported encryption",
    "unreadable": "not a readable PDF",
    "rewrite failed": "unlocked but could not be rewritten",
    "write error": "could not be written back",
}


def folder_root(dest_folder):
    """The literal directory prefix of a dest_folder template.

    dest_folder templates embed placeholders ("~/Statements/Bank/{year}"),
    so everything up to the first placeholder is the real directory to walk.
    """
    expanded = os.path.expanduser(dest_folder)
    parts = []
    for part in Path(expanded).parts:
        if "{" in part:
            break
        parts.append(part)
    return Path(*parts) if parts else Path(".")


def rewrite_in_place(path, data):
    """Replace a file's contents atomically, preserving its timestamps.

    The decrypted bytes go to a temp file in the same directory and are
    then moved over the original, so an interruption can never leave a
    truncated or half-written statement behind. Timestamps are carried
    over because they are the only record of when a file was downloaded.
    """
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".decrypt-", suffix=".tmp")
    tmp = Path(tmp)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        shutil.copystat(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def process_pipeline(pipeline, dry_run=False):
    """Walk one pipeline's folder. Returns (Counter, failures)."""
    name = pipeline["name"]
    tally = Counter()
    failures = []

    passwords = resolve_passwords(pipeline)
    if not passwords:
        logging.info(f"[{name}] no passwords configured, skipping")
        return tally, failures

    root = folder_root(pipeline["dest_folder"])
    if not root.is_dir():
        logging.warning(f"[{name}] folder does not exist yet: {root}")
        return tally, failures

    pdfs = sorted(p for p in root.rglob("*.pdf") if p.is_file())
    logging.info(f"[{name}] scanning {len(pdfs)} PDF(s) under {root}")

    for pdf in pdfs:
        try:
            data = pdf.read_bytes()
        except OSError as e:
            logging.error(f"  [{name}] {pdf}: could not be read ({e})")
            tally["unreadable"] += 1
            failures.append((pdf, "unreadable"))
            continue

        new_data, status = decrypt_pdf(data, passwords, f"[{name}] {pdf.name}")

        if status == "decrypted" and not dry_run:
            try:
                rewrite_in_place(pdf, new_data)
            except OSError as e:
                logging.error(f"  [{name}] {pdf}: could not be written back ({e})")
                tally["write error"] += 1
                failures.append((pdf, "write error"))
                continue

        tally[status] += 1
        if status == "decrypted":
            verb = "would decrypt" if dry_run else "decrypted"
            logging.info(f"  [{name}] {verb}: {pdf}")
        elif status not in OK_STATUSES:
            failures.append((pdf, status))

    return tally, failures


def report(tally, failures, dry_run):
    """Print the end-of-run summary."""
    total = sum(tally.values())
    width = 72

    print()
    print("=" * width)
    print("DRY RUN -- nothing was modified" if dry_run else "Decryption summary")
    print("=" * width)

    if not total:
        print("No PDFs found. Check that dest_folder is right and that the "
              "pipelines have passwords configured.")
        return

    decrypted = tally.get("decrypted", 0)
    readable = tally.get("not encrypted", 0)
    failed = len(failures)

    verb = "would be decrypted" if dry_run else "decrypted"
    print(f"  {total:>5}  PDF(s) scanned")
    print(f"  {decrypted:>5}  {verb}")
    print(f"  {readable:>5}  already readable, left untouched")
    print(f"  {failed:>5}  failed")

    if failed:
        # Break the failures out by cause -- "wrong password" across a
        # contiguous run of years usually means one more old password is
        # missing, which reads very differently from a corrupt file.
        by_status = Counter(status for _, status in failures)
        print()
        print("  Failures by cause:")
        for status, count in by_status.most_common():
            print(f"    {count:>5}  {STATUS_LABELS.get(status, status)}")

        print()
        print(f"  Files still encrypted ({failed}):")
        for path, status in failures:
            print(f"    {path}  [{STATUS_LABELS.get(status, status)}]")
        print()
        print("  These were left exactly as they were. If they share a date range,")
        print("  the sender likely used a different password then -- add it to the")
        print("  pipeline's `passwords:` list and re-run.")

    print("=" * width)


def main():
    parser = argparse.ArgumentParser(
        description="Decrypt already-downloaded PDFs using the passwords in the config.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Walks each pipeline's dest_folder and rewrites encrypted PDFs in place,
using that pipeline's `passwords` / `passwords_env` values. Files that
already open are left untouched; files that no password unlocks are left
untouched too, and listed at the end.

Use this after adding a password to a pipeline that has already run --
the downloader itself will not revisit messages it has already processed.

examples:
  decrypt_pdfs.py --dry-run         see what would change, change nothing
  decrypt_pdfs.py                   decrypt everything it can
  decrypt_pdfs.py --pipeline bank_statements
""",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="Path to the pipeline config file (default: %(default)s)",
    )
    parser.add_argument("--pipeline", help="Only walk the pipeline with this name")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be decrypted without modifying any file",
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
        logging.info("[dry-run] No files will be modified.")

    tally = Counter()
    failures = []
    for pipeline in pipelines:
        p_tally, p_failures = process_pipeline(pipeline, dry_run=args.dry_run)
        tally.update(p_tally)
        failures.extend(p_failures)

    report(tally, failures, args.dry_run)

    # Non-zero exit if anything is still encrypted, so a cron wrapper notices.
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
