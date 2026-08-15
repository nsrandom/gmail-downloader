# Gmail PDF Pipeline

Scans Gmail for messages matching a filter, downloads their PDF attachments,
and saves them into a structured folder. Designed to run daily via cron:
the first run scans everything matching the filter, and every run after
that only scans new mail.

## 1. One-time Google Cloud setup

1. Go to https://console.cloud.google.com/ and create a project (or use an
   existing one).
2. Enable the **Gmail API** for that project (APIs & Services -> Library ->
   search "Gmail API" -> Enable).
3. Go to APIs & Services -> Credentials -> Create Credentials -> OAuth
   client ID.
   - If prompted, configure the OAuth consent screen first. Choose
     "External" and add yourself as a test user -- you don't need to
     publish the app since this is just for your own account.
   - Application type: **Desktop app**.
4. Download the resulting JSON file, rename it `credentials.json`, and
   place it in this folder (next to `attachments_downloader.py`).

`credentials.json` and the `token.json` it later generates both contain
sensitive access to your mailbox -- don't commit them to a public repo.

## 2. Install dependencies

```bash
pip install -r requirements.txt
```

(Consider a virtualenv: `python -m venv venv && source venv/bin/activate`.)

## 3. Configure your pipelines

Config files live in `configs/`. The attachment downloader reads
`configs/attachments_config.yaml` by default — start from the tracked template:

```bash
cp configs/attachments_config.yaml.example configs/attachments_config.yaml
```

Real configs (`configs/*.yaml`) are gitignored since they tend to hold account
details and passwords; only the `*.yaml.example` templates are committed. Point
either script at a different file with `--config`.

Each pipeline needs:

- `query` -- Gmail search syntax. Test it in the Gmail search bar first to
  confirm it matches what you expect.
- `dest_folder` / `filename_template` -- support `{year}`, `{month}`,
  `{day}`, `{sender}`, `{subject}`, and (filename only) `{orig_filename}`.

### Password-protected PDFs

Some senders (banks especially) encrypt their statement PDFs. A pipeline can
optionally carry the password, in which case the PDF is unlocked and the
**saved copy is written with no password at all** -- so the archive stays
readable without having to remember which password went with which sender.

Prefer keeping the password out of the config file:

```yaml
  - name: bank_statements
    query: '...'
    dest_folder: "..."
    filename_template: "..."
    passwords_env: BANK_PDF_PASSWORD    # name of an env var
```

```bash
export BANK_PDF_PASSWORD='...'          # in ~/.zshrc, or the cron entry
```

A literal `passwords: ["..."]` field also works if you'd rather not bother,
but it leaves the password in plaintext in the config file.

### When the password changed over time

Senders rotate their password scheme, which leaves older mail needing an
older password. Every password key accepts a **list**, and each candidate is
tried in order until one opens the file:

```yaml
    passwords:
      - "current-one"
      - "the-one-before-that"
      - "the-original"
```

```yaml
    passwords_env:                      # same, via env vars
      - BANK_PDF_PASSWORD
      - BANK_PDF_PASSWORD_OLD
```

Both spellings work for either form: `password`/`passwords` and
`password_env`/`passwords_env` are interchangeable, and each takes a single
value or a list. If you set several, env vars are tried before literals.
When a file opens with something other than the first candidate, the log
says which one worked.

**Quote numeric passwords.** Unquoted, YAML reads `01234` as octal (`668`)
and `yes` as `true`. The runner re-reads password fields as raw text so this
can't silently corrupt them, but quoting is the habit to keep.

If a PDF can't be unlocked -- none of the passwords match, or an encryption
scheme pypdf doesn't support -- it is still saved in its original encrypted
form and an `ERROR` line is written to the log. Nothing is ever dropped
because decryption failed, so it's worth grepping the log for `ERROR` after
enabling a password for the first time.

### Fixing PDFs that were already saved encrypted

Adding a password only affects future downloads. Files already on disk stay
encrypted, and the downloader won't revisit them -- their message IDs are in
the pipeline state, so they count as done.

`decrypt.py` fixes those in place. It walks each pipeline's `dest_folder`,
and rewrites every encrypted PDF it can open with that pipeline's passwords:

```bash
python decrypt.py --dry-run     # report only, change nothing
python decrypt.py               # decrypt everything it can
python decrypt.py --pipeline bank_statements
```

Files that already open are left untouched, and a file no password unlocks is
never modified -- it's just listed at the end, grouped by cause. Rewrites are
atomic (temp file plus rename) and preserve timestamps, so an interruption
can't leave a half-written statement behind. It's safe to re-run, and exits
non-zero if anything is still encrypted.

Don't try to fix these by deleting the state file and re-downloading: the
re-fetched copies land alongside the encrypted ones as `..._1.pdf` rather
than replacing them.

Note that this only applies to PDFs whose password you already know; there's
no cracking involved.

## 4. First run (interactive)

```bash
python attachments_downloader.py
```

This opens a browser window for you to sign in and grant read-only Gmail
access. It caches the resulting token in `token.json`, so future runs --
including unattended cron runs -- don't need a browser.

The first run will scan your entire mailbox history for each pipeline's
query, since there's no prior state yet. If a query is broad, this could
take a little while and pull in a lot of matches -- consider narrowing the
query (e.g. adding `after:2025/01/01`) for the first run if needed.

## 5. Automate it with cron

```bash
crontab -e
```

Add a line to run it daily, e.g. at 7am:

```
0 7 * * * cd /path/to/gmail_pdf_pipeline && /path/to/venv/bin/python attachments_downloader.py >> cron.log 2>&1
```

On macOS, cron jobs can be skipped if your machine is asleep at the
scheduled time -- a `launchd` job (with `RunAtLoad` / catch-up behavior) is
more reliable if that matters to you.

## When authorization expires

If a run stops with **"Gmail authorization has expired"**, the login cached in
`token.json` is no longer accepted by Google. Sign in again:

```bash
python attachments_downloader.py --reauth
```

That ignores the saved token, opens a browser once, and writes a fresh
`token.json` — after which unattended runs work again without a browser. The
run prints these instructions itself, including the exact command for your
interpreter, so there's nothing to look up when it happens.

The usual cause is the OAuth consent screen still being in **Testing** mode:
Google expires refresh tokens for test-mode apps after **7 days**, so a cron
job dies about weekly. To stop that, go to Google Cloud Console → APIs &
Services → OAuth consent screen → **Publish app**. The app stays private to
your own account; publishing only lifts the test-mode expiry. Access being
revoked at https://myaccount.google.com/permissions, a password change, or
~6 months of disuse will also expire the token.

`--reauth` needs a terminal and a browser. If it's run where neither exists
(cron, launchd), it says so and exits non-zero rather than hanging while it
waits for a sign-in nobody can complete — so run it by hand once, and let the
scheduled job pick up the new token on its next run.

## How incremental scanning works

Each pipeline has its own state file under `state/<pipeline_name>.json`,
storing:

- `last_run_date` -- appended to the query as `after:` (with a 1-day
  overlap buffer) so each run only asks Gmail for recent mail.
- `processed_message_ids` -- so even within that overlap window, messages
  already handled are skipped rather than re-saved.

To force a full rescan for a pipeline, delete its state file.

## Running a single pipeline

```bash
python attachments_downloader.py --pipeline bank_statements
```

## Previewing without downloading

```bash
python attachments_downloader.py --dry-run
```

Reports the destination path each matched message would be written to,
without fetching attachments, writing files, or updating state -- so the
next real run still sees exactly the same messages as new. Run
`python attachments_downloader.py --help` for the full flag and config reference.

## Logs

Every run appends to `pipeline.log` in this folder, in addition to
printing to stdout.
