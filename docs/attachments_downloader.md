# attachments_downloader.py

Searches Gmail for messages matching each configured pipeline's query,
downloads their PDF attachments, and files them under a folder and filename
you template. Re-runs are incremental: only mail that arrived since the last
run is scanned.

Needs the [setup](setup.md) done first.

- [Usage](#usage)
- [Configuration reference](#configuration-reference)
- [Password-protected PDFs](#password-protected-pdfs)
- [Previewing without downloading](#previewing-without-downloading)
- [How incremental scanning works](#how-incremental-scanning-works)
- [Logs](#logs)

## Usage

```bash
python attachments_downloader.py                          # every pipeline in the config
python attachments_downloader.py --pipeline bank_statements
python attachments_downloader.py --dry-run                # preview, write nothing
python attachments_downloader.py --config configs/other.yaml
python attachments_downloader.py --reauth                 # sign in again
```

| Flag | Effect |
| --- | --- |
| `--config PATH` | Config file to read. Defaults to `configs/attachments_config.yaml`. |
| `--pipeline NAME` | Run only the pipeline with this name. Exits non-zero if there is no such pipeline. |
| `--dry-run` | Report what would be saved without fetching attachments, writing files, or updating state. |
| `--reauth` | Ignore the saved token and sign in again, replacing `state/token.json`. Needs a browser, so not for cron. See [when authorization expires](setup.md#when-authorization-expires). |

`python attachments_downloader.py --help` prints the same reference inline.

## Configuration reference

Every entry under `pipelines:` is one independent job, and the runner loops
over all of them. Both this script and [decrypt_pdfs.py](decrypt_pdfs.md) read
the same file.

| Key | Required | Meaning |
| --- | --- | --- |
| `name` | yes | Unique identifier. Names the state file and is what `--pipeline` matches. |
| `query` | yes | Gmail search syntax. Test it in the Gmail search bar first. |
| `dest_folder` | yes | Destination directory. Supports template variables and `~`. |
| `filename_template` | yes | Destination filename. `.pdf` is appended if missing. |
| `passwords` / `password` | no | Literal password(s) for encrypted PDFs. |
| `passwords_env` / `password_env` | no | Name(s) of environment variables holding them. |

Template variables for `dest_folder` and `filename_template`: `{year}`,
`{month}`, `{day}`, `{sender}`, `{subject}` -- and, for `filename_template`
only, `{orig_filename}`. Dates come from the message's own timestamp, not the
run date, so a statement that arrives late still files under its own date.

```yaml
pipelines:
  - name: electric_bill
    query: 'from:ebill@your-electric-company.com has:attachment filename:pdf'
    dest_folder: "~/Statements/Utilities/Electric/{year}"
    filename_template: "{year}-{month}-{day}_electric_bill.pdf"
```

Sender and subject values are sanitized before they land in a path, and a
destination that already exists gets `_1`, `_2`, ... appended rather than being
overwritten.

State files are named after the pipeline but live one level down, in
`state/attachments/`, so a pipeline name can never collide with the login files
in `state/` or with a pipeline of the same name in the extractor's config.

## Password-protected PDFs

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
enabling a password for the first time. To fix files that were already
archived encrypted, use [decrypt_pdfs.py](decrypt_pdfs.md).

## Previewing without downloading

```bash
python attachments_downloader.py --dry-run
```

Reports the destination path each matched message would be written to,
without fetching attachments, writing files, or updating state -- so the
next real run still sees exactly the same messages as new. Sizes shown are
the attachment's own size from Gmail's metadata; a real run that removes a
password rewrites the file, so the saved size will differ.

## How incremental scanning works

`state/` holds everything the runner generates or needs at runtime: the
OAuth `credentials.json` and `token.json`, `pipeline.log`, and one state file
per pipeline. Each pipeline's state lives in `state/attachments/<pipeline_name>.json`,
storing:

- `last_run_date` -- appended to the query as `after:` (with a 1-day
  overlap buffer) so each run only asks Gmail for recent mail.
- `processed_message_ids` -- so even within that overlap window, messages
  already handled are skipped rather than re-saved.

To force a full rescan for a pipeline, delete its state file.

A Gmail API error on one message is logged and the run moves on to the next;
only an authorization failure stops the run, since every remaining message
would fail the same way.

## Logs

Every run appends to `state/pipeline.log`, in addition to printing to stdout.
