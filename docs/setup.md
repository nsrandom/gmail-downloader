# Setup

One-time setup, from a fresh clone to a scheduled daily run.

- [1. Google Cloud setup](#1-google-cloud-setup)
- [2. Install dependencies](#2-install-dependencies)
- [3. Create your config](#3-create-your-config)
- [4. First run (interactive)](#4-first-run-interactive)
- [5. Automate it](#5-automate-it)
- [When authorization expires](#when-authorization-expires)

## 1. Google Cloud setup

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
   place it in the `state/` folder (create it if it isn't there yet):

```bash
mkdir -p state && mv ~/Downloads/client_secret_*.json state/credentials.json
```

`state/credentials.json` and the `state/token.json` it later generates both
contain sensitive access to your mailbox. The whole `state/` folder is
gitignored for that reason -- don't commit it to a public repo.

Access is requested with Gmail's **read-only** scope, so nothing here can
send, delete, or modify mail.

## 2. Install dependencies

```bash
pip install -r requirements.txt
```

(Consider a virtualenv: `python -m venv venv && source venv/bin/activate`.)

## 3. Create your config

Config files live in `configs/`. The attachment downloader reads
`configs/attachments_config.yaml` by default -- start from the tracked
template:

```bash
cp configs/attachments_config.yaml.example configs/attachments_config.yaml
```

Real configs (`configs/*.yaml`) are gitignored since they tend to hold account
details and passwords; only the `*.yaml.example` templates are committed. Point
either script at a different file with `--config`.

Each pipeline needs a `name`, a `query`, a `dest_folder` and a
`filename_template`. See the
[configuration reference](attachments_downloader.md#configuration-reference)
for every field, the template variables, and the optional password settings.

## 4. First run (interactive)

```bash
python attachments_downloader.py
```

This opens a browser window for you to sign in and grant read-only Gmail
access. It caches the resulting token in `state/token.json`, so future runs --
including unattended cron runs -- don't need a browser.

The first run will scan your entire mailbox history for each pipeline's
query, since there's no prior state yet. If a query is broad, this could
take a little while and pull in a lot of matches -- consider narrowing the
query (e.g. adding `after:2025/01/01`) for the first run if needed.

A `--dry-run` first is a cheap way to confirm your queries and destination
paths before anything is written; see
[previewing without downloading](attachments_downloader.md#previewing-without-downloading).

## 5. Automate it

```bash
crontab -e
```

Add a line to run it daily, e.g. at 7am:

```
0 7 * * * cd /path/to/gmail-downloader && /path/to/venv/bin/python attachments_downloader.py >> state/cron.log 2>&1
```

On macOS, cron jobs can be skipped if your machine is asleep at the
scheduled time -- a `launchd` job (with `RunAtLoad` / catch-up behavior) is
more reliable if that matters to you.

If a pipeline reads its password from an environment variable, remember that
cron does not load your shell profile -- set the variable in the crontab
entry itself.

## When authorization expires

If a run stops with **"Gmail authorization has expired"**, the login cached in
`state/token.json` is no longer accepted by Google. Sign in again:

```bash
python attachments_downloader.py --reauth
```

That ignores the saved token, opens a browser once, and writes a fresh
`state/token.json` -- after which unattended runs work again without a
browser. The run prints these instructions itself, including the exact command
for your interpreter, so there's nothing to look up when it happens.

The usual cause is the OAuth consent screen still being in **Testing** mode:
Google expires refresh tokens for test-mode apps after **7 days**, so a cron
job dies about weekly. To stop that, go to Google Cloud Console -> APIs &
Services -> OAuth consent screen -> **Publish app**. The app stays private to
your own account; publishing only lifts the test-mode expiry. Access being
revoked at https://myaccount.google.com/permissions, a password change, or
~6 months of disuse will also expire the token.

`--reauth` needs a terminal and a browser. If it's run where neither exists
(cron, launchd), it says so and exits non-zero rather than hanging while it
waits for a sign-in nobody can complete -- so run it by hand once, and let the
scheduled job pick up the new token on its next run.
