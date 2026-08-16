# decrypt_pdfs.py

Removes passwords from PDFs that were **already saved** to disk, in place,
using the passwords configured for each pipeline.

- [When you need it](#when-you-need-it)
- [Usage](#usage)
- [What it does to your files](#what-it-does-to-your-files)
- [Reading the summary](#reading-the-summary)

## When you need it

[attachments_downloader.py](attachments_downloader.md) strips passwords as it
saves -- but only with the passwords configured at the time. Files archived
before a password was added (or before an older one was discovered) are still
sitting there encrypted, and the downloader will not revisit them: their
message IDs are recorded in the pipeline state, so they count as done.

This script fixes those instead. Reach for it after adding a password to a
pipeline that has already run.

Don't try to fix them by deleting the state file and re-downloading: the
re-fetched copies land alongside the encrypted ones as `..._1.pdf` rather
than replacing them.

This only applies to PDFs whose password you already know; there's no
cracking involved.

## Usage

```bash
python decrypt_pdfs.py --dry-run     # report only, change nothing
python decrypt_pdfs.py               # decrypt everything it can
python decrypt_pdfs.py --pipeline bank_statements
python decrypt_pdfs.py --config configs/other.yaml
```

| Flag | Effect |
| --- | --- |
| `--config PATH` | Config file to read. Defaults to `configs/attachments_config.yaml`. |
| `--pipeline NAME` | Only walk the pipeline with this name. |
| `--dry-run` | Report what would be decrypted without modifying any file. |

It reads the same config as the downloader, using each pipeline's
`dest_folder` and [password settings](attachments_downloader.md#password-protected-pdfs).
Pipelines with no password configured are skipped.

Exits non-zero if any file is still encrypted when it finishes, so a cron
wrapper notices.

## What it does to your files

For each pipeline it takes the literal directory prefix of `dest_folder`
(everything before the first `{placeholder}`), walks it recursively for
`*.pdf`, and for each file:

- **Already readable** -- left untouched.
- **Opens with one of the passwords** -- rewritten without the password.
- **Nothing opens it** -- left exactly as it was, and listed at the end.

Rewrites are atomic (temp file in the same directory, then a rename) and
preserve the original timestamps, so an interruption can't leave a
half-written statement behind and the download date isn't lost. It's safe to
re-run at any time.

## Reading the summary

The run ends with a count of PDFs scanned, decrypted, already readable, and
failed. Anything that failed is then broken out **by cause** and listed
individually:

- *no configured password worked* -- the usual one. If the failures cluster
  in a date range, the sender likely used a different password then; add it
  to that pipeline's `passwords:` list and re-run.
- *unsupported encryption* -- pypdf can't handle the scheme.
- *not a readable PDF* -- the file is corrupt, or isn't a PDF.
- *unlocked but could not be rewritten* / *could not be written back* -- the
  password worked but saving didn't; check disk space and permissions.
