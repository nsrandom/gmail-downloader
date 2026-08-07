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
   place it in this folder (next to `gmail_pipeline.py`).

`credentials.json` and the `token.json` it later generates both contain
sensitive access to your mailbox -- don't commit them to a public repo.

## 2. Install dependencies

```bash
pip install -r requirements.txt
```

(Consider a virtualenv: `python -m venv venv && source venv/bin/activate`.)

## 3. Configure your pipelines

Edit `config.yaml`. Each pipeline needs:

- `query` -- Gmail search syntax. Test it in the Gmail search bar first to
  confirm it matches what you expect.
- `dest_folder` / `filename_template` -- support `{year}`, `{month}`,
  `{day}`, `{sender}`, `{subject}`, and (filename only) `{orig_filename}`.

## 4. First run (interactive)

```bash
python gmail_pipeline.py
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
0 7 * * * cd /path/to/gmail_pdf_pipeline && /path/to/venv/bin/python gmail_pipeline.py >> cron.log 2>&1
```

On macOS, cron jobs can be skipped if your machine is asleep at the
scheduled time -- a `launchd` job (with `RunAtLoad` / catch-up behavior) is
more reliable if that matters to you.

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
python gmail_pipeline.py --pipeline chase_checking_statement
```

## Logs

Every run appends to `pipeline.log` in this folder, in addition to
printing to stdout.
