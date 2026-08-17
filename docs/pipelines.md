# pipelines_runner.py

Searches Gmail for messages matching each configured pipeline's query, extracts
a record — one JSON dictionary — from every new message, and hands it to one or
more actions: an HTTP POST to an API you own, a Google Calendar event, or a
Python function of your own. Re-runs are incremental, and track which actions
have already succeeded for which message, so a partial failure retries only the
part that actually failed.

Needs the [setup](setup.md) done first. The design reasoning behind all of this
is in [pipelines_design.md](pipelines_design.md).

- [Usage](#usage)
- [Configuration reference](#configuration-reference)
- [Extract steps](#extract-steps)
- [Templating](#templating)
- [Actions](#actions)
- [Writing your own Python steps](#writing-your-own-python-steps)
- [Developing a pipeline](#developing-a-pipeline)
- [State, retries, and the run summary](#state-retries-and-the-run-summary)
- [Authorization](#authorization)

## Usage

```bash
python pipelines_runner.py                                  # every pipeline
python pipelines_runner.py --pipeline electric_bill
python pipelines_runner.py --dry-run                        # act on nothing
python pipelines_runner.py --explain --message-id MSGID     # what would I extract?
python pipelines_runner.py --replay                         # offline, against fixtures
```

| Flag | Effect |
| --- | --- |
| `--config PATH` | Config file to read. Defaults to `configs/pipelines_config.yaml`. |
| `--env-file PATH` | Read `${VAR}` values from here instead of `./.env`. |
| `--no-env-file` | Ignore `.env` entirely. |
| `--pipeline NAME` | Run only this pipeline. Repeatable. |
| `--dry-run` | Extract and render everything, perform no action, write no state. |
| `--message-id ID` | Run against one specific message, ignoring state. |
| `--explain` | Print the extracted record as a table and run no actions. |
| `--dump-body` | Save matched messages as fixtures under `tests/fixtures/<pipeline>/`. |
| `--replay` | Re-extract from saved fixtures, offline, and check against expectations. |
| `--freeze` | With `--replay`: record the current extraction as the expectation. |
| `--only-action ID` / `--skip-action ID` | Restrict which actions run. Repeatable. |
| `--since YYYY-MM-DD` | Override the incremental window. |
| `--limit N` | Process at most N messages. |
| `--reauth` | Ignore the saved token and sign in again. Needs a browser. |
| `--verbose` | Debug logging. |

## Configuration reference

Copy `configs/pipelines_config.yaml.example` to `configs/pipelines_config.yaml`
and edit. Secrets go in `.env`, never in the config.

```yaml
version: 1

defaults:
  timezone: America/Los_Angeles   # reading email dates, placing calendar events
  overlap_days: 1                 # re-scan this far back on every run
  http_timeout: 20
  on_missing_field: fail          # fail | skip | warn
  max_attempts: 5                 # give up on a message after this many failed runs
  retain_days: 90                 # how long a message stays in the state file

targets:                          # connection-level settings, referenced by name
  finance_api:
    type: http
    base_url: https://api.example.com
    headers:
      Authorization: "Bearer ${FINANCE_API_TOKEN}"

pipelines:
  - name: electric_bill           # unique; names the state file
    query: 'from:... subject:"..."'   # Gmail search syntax
    extract: {...}                # -> the record
    computed: {...}               # derived fields (optional)
    actions: [...]                # what to do with it
```

### Environment variables

`${VAR}` anywhere in the config is replaced from the environment. Values are
read from two places:

1. the real process environment, and
2. a `.env` file in the project root (`KEY=value` per line).

**The process environment wins**, so `FINANCE_API_TOKEN=... python
pipelines_runner.py` overrides `.env` without editing anything. An unset
variable is an error, not an empty string.

Resolution is lazy: only targets that a selected pipeline actually posts to are
resolved, so `--pipeline redfin_home_value` never fails because an unrelated
pipeline's token is not set.

`.env` is gitignored. `.env.example` lists the key names and is not.

## Extract steps

`extract.steps` is an ordered list. Each step contributes to the record:

- a step **with** a `name` contributes that one key;
- a step **without** one must return a dict, which is merged in.

Later steps see the record built so far, and their config is rendered as a
template against it first — so a step can key off an earlier value. A regex
`pattern` is the one key never rendered, so `\d{2}` needs no escaping.

| `using` | Purpose | Key options |
| --- | --- | --- |
| `regex` | Match against the selected source | `pattern`, `group`, `flags` (`i m s x`), `all` |
| `css` | BeautifulSoup/soupsieve selector | `selector`, `attr`, `all`, `strict` |
| `attachment_pdf` | Text of a PDF attachment | `filename_match`, `page`, `pattern`, `password` / `passwords_env` |
| `python` | Anything | `module`, `function` |

A `regex` with named groups and no `name:` returns a dict of all of them.

### Fallbacks: one field, several of a sender's layouts

Senders redesign their mail, and the old messages in your mailbox do not change
to match. `fallbacks:` lists alternative ways to find the same field, tried in
order until one produces a value:

```yaml
- name: amount
  using: css
  selector: 'td:has(img[src*="amount-due"]) + td strong'   # current layout
  type: money
  required: true
  fallbacks:
    - source: html_text                                     # previous layout
      pattern: 'Statement balance:\s*\$\s*([\d,]+\.\d{2})'
    - source: html_text                                     # the one before that
      pattern: 'The amount of \$([\d,]+\.\d{2})'
```

A fallback inherits the field's identity — `name`, `type`, `format`,
`required`, `default` — and brings only its own way of finding the value, so
three layouts need one step rather than three near-identical ones. `using` is
inferred from the keys you give it (`selector` → `css`, `pattern` → `regex`),
so a fallback that only swaps the selector need not restate it.

Two behaviours worth knowing:

- **A match that will not coerce counts as a miss**, so the next candidate gets
  a turn. A layout whose "amount" is the word `Pending` falls through instead
  of failing the message.
- **A broken selector or pattern raises instead of falling through.** It would
  fail on every message, so hiding it behind a fallback would turn a config
  mistake into a silent data gap.

When a value comes from anything but the primary, the log says so —
`field 'amount' came from fallback #2` — because that usually means the sender
has changed something and the config deserves another look.

**When to stop using fallbacks.** One or two alternatives per field reads fine.
Past that — several fields each with several layouts, where which layout you
are in depends on when the mail was sent — the config becomes a matrix that
YAML has no good way to annotate, and the useful information (*which* layout,
*when* it was in use, why it looks like that) has nowhere to live. That is the
point to move the whole thing into a Python step.
[`extractors/pge_bill.py`](../extractors/pge_bill.py) is exactly this case, and
the config it replaced is in git history if you want the comparison.

### Sources

`source` picks what a step reads. Set it once on `extract` and override it per
step.

| `source` | What it is |
| --- | --- |
| `html` | The HTML part (falls back to the text part if there is none) |
| `html_text` | The **visible text** of the HTML, tags stripped |
| `text` | The plain-text part |
| `subject` | The subject line |
| `headers` | All headers, one `Name: value` per line |

`html_text` is usually the better regex target: senders wrap values in nested
tables that a pattern over raw markup will not survive.

### Types

`type` coerces the captured string. A coercion failure is treated like a
missing value.

| `type` | Result |
| --- | --- |
| `string` | `str` with whitespace collapsed (default) |
| `int`, `float` | numbers, commas stripped |
| `money` | `Decimal` — strips `$`, `₹`, commas, non-breaking spaces |
| `date`, `datetime` | needs `format` (strptime) unless the value is ISO-8601 |
| `bool` | `true/yes/y/1/on` vs `false/no/n/0/off` |
| `raw` | the captured string, untouched |

`money` is a `Decimal` all the way to the wire: `153.13` is sent as exactly
`153.13`, never as a binary float.

### Computed

`computed` adds derived fields after the steps have run.

| `using` | Options |
| --- | --- |
| `date_shift` | `from`, `days` (may be negative) |
| `const` | `value` |
| `format` | `template` |
| `python` | `module`, `function` |

```yaml
computed:
  remind_on:
    using: date_shift
    from: due_date
    days: -3
```

That is the whole list, on purpose. Anything more involved is a Python step.

## Templating

Action config is rendered with Jinja2. Every field of the record is in scope,
plus:

| Name | Contents |
| --- | --- |
| `email` | `id`, `subject`, `sender`, `to`, `date`, `link`, `headers`, `thread_id` |
| `pipeline` | `name` |
| `run` | `started_at` |

Filters: `date(fmt)`, `money(places)`, `shift_days(n)`, `slug`, plus Jinja's own
(`default`, `join`, `round`, …).

A typo raises rather than rendering an empty string. Use `| default('?')` where
a value may legitimately be absent.

### Keeping a value's type: `!expr`

Jinja renders to strings, which is right for a description and wrong for
`{"amount": 153.13}`. The `!expr` tag takes a Jinja *expression* — no braces —
and keeps the Python value:

```yaml
json:
  amount:  !expr amount                          # 153.13    number
  items:   !expr line_items                      # [...]     array
  account: "{{ account_no }}"                    # "0012"    string
  due_on:  "{{ due_date | date('%Y-%m-%d') }}"   # "2026-09-03"
```

Use it for numbers, booleans, lists, and dates an API wants typed. Use plain
`{{ }}` for everything else — especially identifiers like account numbers,
which must not be turned into integers.

## Actions

Every action needs an `id` unique within its pipeline, and either a `target` or
an inline `type`. Actions run in declaration order.

Actions are **independent**: each one sees the extracted record and nothing
else. One cannot read what another returned, and referring to `actions.<id>`
in a template is an error rather than a blank. That is what makes a partial
failure retry cleanly — if one action fails, the others still run, and the
next run retries only the one that failed, on its own. Needing two sinks to
share a value means the value belongs in the record: extract it, or compute
it.

Every action's config is rendered **before any of them runs**, so a typo cannot
leave a half-applied message behind.

Keys an action does not set are filled from `defaults:` where it makes sense —
`http_timeout` for an `http` action's `timeout`, `timezone` for a
`google_calendar` action. A `target:` sits between the two: action first, then
target, then defaults.

### `http`

| Key | Meaning |
| --- | --- |
| `base_url`, `headers` | usually from the `target` |
| `method`, `path` | joined onto `base_url` |
| `url` | an absolute URL, instead of `base_url` + `path` |
| `json` / `form` / `body` | the payload |
| `query` | query-string parameters |
| `timeout`, `retry` | `retry: {attempts: 3, backoff_seconds: 2}` |
| `expect_status` | accepted statuses, default `2xx` |
| `money_format` | `number` (default) or `string` |

Retries connection errors and 429/5xx; a 4xx is the server saying the request
itself is wrong, so it is never retried. Sends an `Idempotency-Key` derived
from pipeline + message + action. Returns `{status, response, headers}`.

Each action names its own destination, so one pipeline can post to several
unrelated services.

### `google_calendar`

| Key | Meaning |
| --- | --- |
| `calendar_id` | defaults to `primary` |
| `summary`, `description`, `location` | event fields |
| `start_date` + `all_day`, or `start_datetime` | when |
| `duration_days` / `duration_minutes` | how long |
| `timezone` | defaults to `defaults.timezone` |
| `reminders` | list of `{method, minutes}` |

Uses a deterministic event id, so re-running cannot create a second copy: the
API answers a repeat with 409, which counts as success.

A timed event needs a time zone — Google rejects one without it, and a
`type: date` field becomes a naive 09:00 — so `defaults.timezone` fills it in
and the action says which key to set if you have cleared it.

`reminders` minutes count back from the *start* of the event. An all-day event
starts at midnight, so `minutes: 900` is 9am the day before.

### `file`

Appends the record to a file on disk — a running ledger of everything a
pipeline has extracted, with no API needed.

| Key | Meaning |
| --- | --- |
| `path` | Where to write. Templated, `~` expanded, parent directories created. |
| `format` | `jsonl` (default), `csv`, or `text`. Inferred from the extension when omitted. |
| `mode` | `append` (default) or `overwrite` |
| `fields` | Which record fields to write, in this order. Omit for all of them. |
| `values` | An explicit mapping to write instead, templated like an `http` body. |
| `line` | `text` format only: the line to append. |
| `dedupe` | Default `true` for appends. See below. |
| `money_format` | `number` (default) or `string` |

```yaml
- id: save_bill
  type: file
  path: "~/records/bills-{{ email.date | date('%Y') }}.csv"
  fields: [amount, due_date]
```

```csv
amount,due_date,_key
153.13,2026-09-03,1a006cd2a5f1da81:save_bill
```

**About `_key` and duplicate rows.** The other sinks get duplicate protection
from the far end — `http` sends an `Idempotency-Key`, and `google_calendar`
reuses a deterministic event id that Google rejects on a repeat. A file has
nobody to ask. State normally stops a second write, but state can be deleted,
pruned, or hand-edited, and a silently doubled row in a ledger you later add up
is a bad way to find that out.

So each row carries `_key` (`<message_id>:<action_id>`), and the file is
scanned for it before anything is appended. Delete the state directory entirely
and re-run: the rows do not double. Set `dedupe: false` to turn the scan off,
or `include_key: false` to leave the column out (which turns dedupe off too).

`text` format cannot do this — there is no reliable key in a free-text line —
so a `text` action is protected by state alone.

Two actions writing to the same CSV need the same columns; a row with a new
column raises rather than silently misaligning the file.

There is no `json` array format on purpose: appending to one means reading and
rewriting the whole file on every message, and `jsonl` is a better fit for a
log that only ever grows. `jq -s .` turns it into an array if you need one.

### `python`

`module` + `function`, resolved from `actions/`.

### `log`

Writes the record to the log. Useful while developing a pipeline.

## Writing your own Python steps

Drop a module into `extractors/` or `actions/` and name it from the config. No
decorator, no registration.

```python
# extractors/pge_bill.py
def extract(email, config, record):
    """Return a dict to merge in (or one value, for a named step)."""
    return {"amount": Decimal("153.13"), "due_date": date(2026, 9, 3)}
```

```yaml
extract:
  steps:
    - using: python
      module: pge_bill        # function defaults to `extract`
```

A step with no `name:` merges the returned dict into the record, which is how
one module can produce several fields at once. Coerce your own types — the
`type:` key applies to named steps only. Raise `ExtractionError` to fail the
message with a useful reason.

Any other key on the step reaches the function as `config`, so a module can be
reusable rather than hard-coded:

```yaml
- using: python
  module: redfin_home_report
  window: 800            # arrives as config["window"]
```

Two worked examples in this repo, taking opposite approaches to the same
problem of a sender who keeps redesigning:

- [`extractors/pge_bill.py`](../extractors/pge_bill.py) — **one reader per
  layout**, tried newest first. Right when the layouts are few, known, and
  genuinely different from each other.
- [`extractors/redfin_home_report.py`](../extractors/redfin_home_report.py) —
  **one rule that outlives the layouts**. Redfin has redesigned four times
  since 2017 and every label around the number changed, but the address is
  always followed by the estimate, so that is what it keys on. It needs no
  edit when they redesign a fifth time.

The second is better where you can find such an invariant, because the
maintenance cost does not grow with the sender's redesign schedule. It needs
guards to stay honest, though: that module stops searching after a set distance
and rejects values outside a plausible range, since the same page carries
listing prices, sold comps, and a median price per square foot.

```python
# actions/notify_phone.py
def run(record, config, context, dry_run=False):
    """Return a JSON-serializable dict, or None.

    context has .services (Google clients), .idempotency_key, .email, .pipeline
    """
    if dry_run:
        return {"skipped": True}
    ...
```

Raising means the action failed and will be retried next run. Honouring
`dry_run` is up to you — the runner cannot enforce it.

Because the config names a module and a function, **a config file is
executable code**. Never run someone else's.

## Developing a pipeline

The work of adding a pipeline is writing a selector against someone's
marketing HTML. The loop:

```bash
# 1. Save a real message
python pipelines_runner.py --dump-body --pipeline electric_bill --limit 5

# 2. Iterate on the config until the record looks right
python pipelines_runner.py --explain --pipeline electric_bill --message-id MSGID

# 3. Freeze what it extracts as the expectation
python pipelines_runner.py --replay --freeze --pipeline electric_bill

# 4. From then on, check every fixture offline in under a second
python pipelines_runner.py --replay
pytest                       # the same check, as a test
```

`--explain` and `--dump-body` run no actions and need no permissions beyond
read-only Gmail, so neither can send anything.

Fixtures are real messages and are gitignored.

A worked example — PG&E labels both the amount and the due date with an
*image*, so there is no text to anchor on, only the structure and the image
filename:

```yaml
- name: amount
  using: css
  selector: 'td:has(img[src*="amount-due"]) + td strong'
  type: money
```

## State, retries, and the run summary

`state/pipelines/<name>.json` records, per message, which actions have
succeeded:

```json
{
  "messages": {
    "1a006cd2a5f1da81": {
      "actions": { "record_bill": "ok", "pay_reminder": "failed: 503" },
      "attempts": 2
    }
  }
}
```

The next run retries `pay_reminder` and leaves `record_bill` alone. A retry
re-fetches and re-extracts the message; the `last_record` kept in state is for
your eyes only and is never replayed.

After `max_attempts` failed runs a message is marked `dead` and skipped, with
one loud log line, so a single bad message never blocks the pipeline. To retry
it, fix the config and delete its entry from the state file.

Entries are pruned by age (`retain_days`), not by count.

Every run writes `state/pipelines/last_run.json`:

```json
{
  "started_at": "...", "finished_at": "...", "exit_status": 1,
  "pipelines": {
    "electric_bill": { "matched": 1, "processed": 0, "failed": 1, "dead": 0,
                       "reasons": ["1a00...: action 'record_bill' failed: HTTP 500"] }
  }
}
```

The exit status is non-zero if anything failed. Point a launchd job or a
menu-bar script at that file if you want to be told. `--dry-run` writes
`last_run.dry.json` instead, so a preview never overwrites the real record.

Logs go to `state/pipelines.log`, separate from the attachments downloader's
`state/pipeline.log`.

## Authorization

Shares `state/credentials.json` and the sign-in flow with
[attachments_downloader.py](attachments_downloader.md).

Scopes are requested per config. A pipeline set with only `http` actions needs
`gmail.readonly` and **reuses the existing `state/token.json`** — no second
consent screen. Adding a calendar action needs `calendar.events`, which is a
wider grant stored separately as `state/token_<hash>.json`; the attachments
downloader's token is never touched.

`--explain`, `--dump-body`, and `--dry-run` perform nothing, so they never ask
for more than read-only Gmail. A dry run tells you what the real run will want.

If your OAuth consent screen is still in "Testing" mode, Google expires refresh
tokens after 7 days. Publishing the app (Cloud Console → APIs & Services →
OAuth consent screen → Publish) lifts that; it stays private to your account.
See [when authorization expires](setup.md#when-authorization-expires).
