# Gmail Downloader

Turns a Gmail search into a tidy folder of files on disk.

You describe a *pipeline* -- a Gmail query, a destination folder, a filename
pattern -- and the runner finds the matching mail, downloads the PDF
attachments, and files them where you said. Point it at your bank, your
utility company, and your building's maintenance invoices, and the statements
land in a consistent tree instead of a mailbox.

It's built to run unattended: the first run sweeps your whole mailbox history,
and every run after that only looks at mail that arrived since, so a daily cron
job stays cheap.

```yaml
pipelines:
  - name: electric_bill
    query: 'from:ebill@your-electric-company.com has:attachment filename:pdf'
    dest_folder: "~/Statements/Utilities/Electric/{year}"
    filename_template: "{year}-{month}-{day}_electric_bill.pdf"
```

```
~/Statements/Utilities/Electric/2026/2026-08-03_electric_bill.pdf
```

## What it can do

- **Many pipelines, one run.** Each has its own query, destination, naming
  scheme and state; they don't interfere with each other.
- **Templated paths and names** -- `{year}`, `{month}`, `{day}`, `{sender}`,
  `{subject}`, `{orig_filename}` -- driven by the message's own date, so late
  mail still files under the date it belongs to.
- **Unlocks password-protected PDFs** as it saves, so the archive is readable
  without remembering which bank used which password. Several passwords can be
  listed per pipeline, for senders that rotated theirs over time.
- **Incremental and idempotent.** Processed message IDs are remembered, so
  re-runs never re-download or duplicate; existing files are never overwritten.
- **Dry-run mode** that shows exactly where each match would land without
  fetching, writing, or advancing state.
- **Read-only Gmail access.** The OAuth scope granted can't send, delete, or
  modify mail.
- **Repairs old archives.** A separate script decrypts PDFs that were saved
  before you knew their password.

## Getting started

```bash
pip install -r requirements.txt
cp configs/attachments_config.yaml.example configs/attachments_config.yaml
# edit the config, then:
python attachments_downloader.py --dry-run
```

That needs Google Cloud credentials first -- **[docs/setup.md](docs/setup.md)**
walks through the whole thing, from enabling the Gmail API to scheduling the
daily run.

## Documentation

| | |
| --- | --- |
| **[Setup](docs/setup.md)** | Google Cloud credentials, install, config, first run, cron, and what to do when authorization expires. |
| **[attachments_downloader.py](docs/attachments_downloader.md)** | The downloader: flags, the full config reference, password handling, dry runs, and how incremental scanning works. |
| **[decrypt_pdfs.py](docs/decrypt_pdfs.md)** | Decrypting PDFs that were already archived encrypted. |

## Layout

```
attachments_downloader.py   the downloader
decrypt_pdfs.py             in-place decryption for already-saved PDFs
configs/                    pipeline configs (*.yaml gitignored, *.example tracked)
docs/                       the documentation above
state/                      credentials, token, log, and per-pipeline state
```

`state/` and your real configs are gitignored -- they hold OAuth credentials,
account details, and PDF passwords. Keep it that way in any fork.
