# Email pipelines — design

Status: **proposal**. Nothing here is built yet.

Decisions taken so far: Jinja2 for templating, with an explicit tag for
type-preserving values; retries re-extract from Gmail rather than replaying a
cached record; `money` serializes as an exact JSON number; offline fixture
replay ships in v1; extraction sources are HTML and PDF attachments; HTTP auth
is a static bearer token, with a seam for OAuth2; failures surface through a
run-summary file; `core/` is new code and `attachments_downloader.py` migrates
onto it later. New dependencies: `jinja2`, `beautifulsoup4`, `simplejson`,
`python-dotenv`.

A generic runner for "find some emails, pull structured data out of them, and
do things with it". It sits alongside
[attachments_downloader.py](attachments_downloader.md), which keeps its own
config and code; the two share a Gmail account and a `state/` directory and
nothing else for now.

- [What this is for](#what-this-is-for)
- [The pipeline model](#the-pipeline-model)
- [Layout](#layout)
- [Config reference](#config-reference)
- [The extract stage](#the-extract-stage)
- [Templating](#templating)
- [Actions](#actions)
- [Writing Python steps](#writing-python-steps)
- [Testing pipelines](#testing-pipelines)
- [State and idempotency](#state-and-idempotency)
- [Authorization and scopes](#authorization-and-scopes)
- [CLI](#cli)
- [Failure semantics](#failure-semantics)
- [Deferred](#deferred)

## What this is for

Two motivating flows:

1. **Utility bill.** Find the monthly billing email, pull the billed amount and
   due date out of the HTML, POST them to a personal finance API, and create a
   Google Calendar reminder a few days before the due date.
2. **Redfin quarterly summary.** Find the email, pull the home appraisal value
   out of it, POST it to the same API.

Both are the same shape, and so is nearly everything else in this space: match
some mail, extract a few fields, fan out to one or more sinks.

`attachments_downloader.py` is a degenerate instance of that shape — match,
extract nothing, one hardcoded action (save the PDF). It is not being changed,
but the model below is deliberately general enough to absorb it later.

## The pipeline model

```
   query  ──▶  fetch  ──▶  extract  ──▶  record (JSON dict)  ──▶  action 1
                                                              ├─▶  action 2
                                                              └─▶  action N
```

Four stages, run per pipeline, per message:

| Stage | Input | Output |
| --- | --- | --- |
| **match** | Gmail query + incremental date window | list of message ids |
| **fetch** | message id | a normalized `Email` object |
| **extract** | `Email` | **one JSON dictionary** — the *record* |
| **act** | record | side effects, plus each action's return value |

The record is the contract between the halves of the system. Everything before
it is about reading Gmail; everything after it only sees a dict. That means
extractors and actions can be developed, tested, and reasoned about
independently, and a Python extractor is interchangeable with a declarative one
as long as it returns a dict.

Actions run in declaration order, and each action's return value is added to
the context, so a later action can reference an earlier one's result.

## Layout

```
core/
  auth.py            scope-aware Google service factory
  config.py          load, validate, ${ENV} interpolation
  state.py           per-pipeline state store
  gmail.py           search, fetch, and the Email object
  templating.py      render(), the context namespace, type coercion
extractors/
  __init__.py        registry + resolver
  regex.py  css.py  json_body.py  attachment_pdf.py
  <your_module>.py   your own extractors live here too
actions/
  __init__.py        registry + resolver
  http.py  google_calendar.py  log.py
  <your_module>.py   your own actions live here too
pipelines_runner.py  entry point
configs/
  pipelines_config.yaml
  pipelines_config.yaml.example
```

Builtin extractors and actions register themselves by name with a decorator.
Adding a builtin is one new file and no runner changes. Your own Python steps
go in the same two directories and are addressed by module name — see
[Writing Python steps](#writing-python-steps).

`core/` is written for this runner and `attachments_downloader.py` is left
alone, so auth, state, and logging exist in two copies for a while. That is
deliberate: the attachments flow works and runs on a schedule, and folding it
into brand-new shared code before that code has run against anything real
trades a working script for a refactor. It migrates in its own commit once
`core/` has a few weeks behind it — first `auth.py` (which is nearly verbatim
what it already does), then `state.py`, then PDF unlocking into `core/pdf.py`.

## Config reference

```yaml
version: 1

defaults:
  timezone: Asia/Kolkata   # interpreting email dates and placing calendar events
  overlap_days: 1          # re-scan this far before last_run_date
  http_timeout: 20
  on_missing_field: fail   # fail | skip | warn

# Connection-level settings, referenced by name from actions. Declared once so
# a base URL, an auth header, or a calendar id is not repeated per pipeline.
# Declare as many as you like; each action names the one it posts to.
targets:
  finance_api:
    type: http
    base_url: https://api.example.com
    headers:
      Authorization: "Bearer ${FINANCE_API_TOKEN}"    # from the environment
  home_assets_api:                                    # a different service
    type: http
    base_url: https://assets.internal.example.com/v2
    headers:
      X-Api-Key: "${ASSETS_API_KEY}"
  personal_calendar:
    type: google_calendar
    calendar_id: primary

pipelines:
  - name: electric_bill
    query: 'from:ebill@bses.com subject:"your bill"'

    extract:
      source: html                  # default source for steps below
      steps:
        - name: amount
          using: regex
          pattern: 'Amount Due[:\s]*₹\s*([\d,]+\.\d{2})'
          type: money
          required: true

        - name: due_date
          using: regex
          pattern: 'Due Date[:\s]*(\d{2}-\d{2}-\d{4})'
          type: date
          format: "%d-%m-%Y"
          required: true

        - name: account_no
          using: css
          selector: "td.account-number"
          required: false
          default: unknown

        # Produces several keys at once; the returned dict is merged.
        - using: python
          module: bses_bill
          function: extract_line_items

    computed:
      remind_on:
        using: date_shift
        from: due_date
        days: -3

    actions:
      - id: save_bill
        target: finance_api
        method: POST
        path: /bills
        json:
          provider: bses
          amount: !expr amount                        # stays a number
          account: "{{ account_no }}"                 # stays a string
          billed_on: "{{ email.date | date('%Y-%m-%d') }}"
          due_on: "{{ due_date | date('%Y-%m-%d') }}"
          source_message_id: "{{ email.id }}"

      - id: pay_reminder
        target: personal_calendar
        summary: "Pay electric bill ₹{{ amount }}"
        start_date: "{{ remind_on | date('%Y-%m-%d') }}"
        all_day: true
        description: |
          Due {{ due_date | date('%d %b %Y') }}.
          Recorded as bill {{ actions.save_bill.response.id | default('?') }}.
          {{ email.link }}

  - name: redfin_home_value
    query: 'from:redfin.com subject:"home value"'
    extract:
      source: html
      steps:
        - name: estimate
          using: css
          selector: "span.home-value"
          type: money
          required: true
    actions:
      # A different service from the one electric_bill posts to.
      - id: record_appraisal
        target: home_assets_api
        method: POST
        path: /home/appraisals
        json:
          value: !expr estimate
          as_of: "{{ email.date | date('%Y-%m-%d') }}"

      # Or skip targets entirely and give an absolute URL inline.
      - id: ping_dashboard
        type: http
        method: POST
        url: https://dashboard.example.net/hooks/home-value
        json:
          value: !expr estimate
```

### Environment variables

`${VAR}` anywhere in the config is replaced from the environment, and an unset
variable is an error rather than an empty string. No secrets belong in this
file.

Values are resolved from two places, in this order:

1. **The real process environment** — exported shell variables, or a one-off
   `FINANCE_API_TOKEN=… python pipelines_runner.py`.
2. **A `.env` file** in the project root, `KEY=value` per line, `#` comments,
   quotes optional.

The process environment wins on a conflict, so a value passed on the command
line overrides the same key in `.env` without editing anything. This is
`python-dotenv`'s default `load_dotenv()` behaviour, which is why it is the
dependency rather than a hand-rolled parser: it also handles quoting, escapes,
and multi-line values that a five-line parser gets wrong. `--env-file PATH`
points at a different file; `--no-env-file` skips it entirely.

`.env` holds live credentials and **must be gitignored** — the repo's
`.gitignore` needs a `.env` line (and one for `tests/fixtures/`) before this
lands. A `.env.example` with the key names and empty values is what gets
committed.

Resolution is **lazy**: only the targets the selected pipelines actually
reference are resolved. Otherwise `--pipeline redfin_home_value` would fail
because the electric bill's API token happens not to be set.

## The extract stage

`extract.steps` is an ordered list. Each step contributes keys to the record:

- A step **with** a `name` contributes exactly that one key.
- A step **without** a `name` must return a dict, which is merged into the
  record. Later steps overwrite earlier keys, and a merge that overwrites is
  logged at warning level.

Steps see the record built so far. A step's own config is rendered as a
[template](#templating) against it first, so a later step can key off an
earlier one's value — declarative steps included, not only Python ones. Regex
`pattern` is the one key that is never rendered, so `\d{2}` needs no escaping.

### Builtin step types

| `using` | Purpose | Key options |
| --- | --- | --- |
| `regex` | Match against the selected source | `pattern`, `group` (default 1), `flags`. A pattern with named groups and no `name` returns a dict of all of them. |
| `css` | BeautifulSoup selector | `selector`, `attr` (default: text), `all: true` for a list |
| `attachment_pdf` | Text of the first matching PDF attachment | `filename_match`, `page`, then `pattern` or `python` |
| `python` | Anything | `module`, `function` |

`attachment_pdf` is in v1 alongside the HTML extractors, because a sender's PDF
is usually a far more stable target than the HTML they wrap it in. It accepts
the same `password` / `passwords_env` keys `attachments_downloader` uses, so an
encrypted statement can be read without being saved anywhere; the unlocking
code moves to `core/pdf.py` and is the second thing the attachments runner
picks up when it migrates. Text extraction is `pypdf`'s, which handles
text-layer PDFs and not scans — a scanned bill needs OCR and is out of scope.

### Sources

`source` selects what a step reads: `html`, `text`, `subject`, `headers`, or
`attachment:pdf`. Set once on `extract` as the default, overridable per step.
`html` falls back to the text part when a message has no HTML, and logs it.

### Types

`type` coerces the captured string. Coercion failure is treated the same as a
missing value.

| `type` | Result | Notes |
| --- | --- | --- |
| `string` | `str` | Default. Whitespace collapsed. |
| `int`, `float` | numbers | Commas stripped |
| `money` | `Decimal` | Strips currency symbols, commas, spaces. Serialized to JSON as a number with its digits preserved. |
| `date`, `datetime` | `datetime` | Needs `format` (strptime) unless the value is ISO-8601 |
| `bool` | `bool` | `true/yes/y/1` vs `false/no/n/0`, case-insensitive |
| `raw` | `str` | No cleanup at all |

### Computed

`computed` runs after `steps` and adds derived keys. It exists so that the
common "three days before the due date" case does not need a Python file:

| `using` | Options |
| --- | --- |
| `date_shift` | `from`, `days` (may be negative) |
| `const` | `value` |
| `format` | `template` — a [template string](#templating) |
| `python` | `module`, `function` |

That is the complete list, and it is meant to stay short. Anything more
involved is a Python step; growing this table into a small programming language
is the failure mode to avoid.

The final record is what gets stored in state and passed to actions.

## Templating

Jinja2, configured with `StrictUndefined` so a typo is an error rather than a
silently empty string. Rendering walks dicts and lists recursively and applies
to string values only; keys are left alone.

`attachments_downloader` uses `str.format` for its path templates and keeps
doing so. Jinja earns the second syntax here because action config needs three
things `.format` cannot express: `| default(...)` for values that may be
absent, `{% for %}` over list-valued fields such as line items, and filters for
date and currency formatting. It also leaves regex quantifiers alone — `\d{2}`
is a `.format` syntax error but means nothing special to Jinja.

The context namespace:

| Name | Contents |
| --- | --- |
| the record's own keys | `{amount}`, `{due_date}`, … |
| `email` | `id`, `thread_id`, `subject`, `sender`, `to`, `date`, `link`, `headers` |
| `actions` | `{actions.<id>.response}` — whatever a completed action returned |
| `pipeline` | `name` |
| `run` | `started_at` |

Custom filters, kept to a small set: `date(fmt)`, `money(places=2)`,
`shift_days(n)`, `slug`. Jinja's own `default`, `join`, `round`, and friends
come for free.

### Typed values: the `!expr` tag

Jinja renders to strings. That is correct for a calendar description and wrong
for `{"amount": 1234.50}`, where an API wants a number.

A YAML tag marks the values that must keep their Python type. Its content is a
Jinja *expression* — no braces — evaluated rather than rendered:

```yaml
json:
  amount:   !expr amount              # -> 1234.50   (number)
  items:    !expr line_items          # -> [...]     (array)
  paid:     !expr false               # -> false     (boolean)
  account:  "{{ account_no }}"        # -> "0012"    (string)
  due_on:   "{{ due_date | date('%Y-%m-%d') }}"
```

Jinja ships a `NativeEnvironment` that does this implicitly for single-node
templates, and it is the wrong tool here: it runs `literal_eval` over the
result, so a genuinely-string field like the account number `"0012"` silently
becomes the integer `12`. Zero-padded account numbers, invoice ids, and phone
numbers are exactly the fields this project handles. An explicit tag applied to
the two or three values that need it is safer and reads better in a diff.

### Rendering happens before any action runs

Every action's config for a message is rendered up front, and a template error
fails the message with **zero** side effects. Otherwise a typo in action 3
surfaces only after action 1 has already POSTed a real bill.

The consequence for chained actions: `{{ actions.save_bill.response.id }}` is
not resolvable at pre-render time. Those references are detected during the
pre-render pass (which establishes the dependency graph, below), left as
placeholders, and resolved immediately before the dependent action runs.

## Actions

Every action entry has an `id` unique within its pipeline (it is the state key
and the handle other actions reference) and either a `target` naming an entry
under `targets:`, or an inline `type`. Remaining keys are the action's own,
rendered before the action sees them.

### Ordering and dependencies

Actions run in declaration order. When one fails, the rest still run — a
calendar reminder should not be lost because an unrelated API was down.

The exception is a **dependent** action. The pre-render pass records which
actions reference `actions.<id>` in their templates, and an action whose
upstream did not finish `ok` is skipped and recorded `blocked` rather than run
with a missing value. Inferring this from the templates beats an explicit
`depends_on:` key, which would silently drift out of sync the first time
someone edits a description.

### Secrets in logs

Rendered action config contains `Authorization: Bearer …`, and `--dry-run`
exists to print exactly that. Anything heading for the log or the console goes
through a redaction pass first, over keys matching `authorization`, `cookie`,
and `*token*` / `*secret*` / `*password*` / `*key*`, plus any value that came
from a `${VAR}` interpolation. `state/` is gitignored, but a bearer token
sitting in a plaintext log forever is still a leak.

### `http`

The generic sink. It talks to APIs **you** own or subscribe to; this project
defines none of them and assumes nothing about their shape.

Every action names its own destination, so one pipeline can fan out to several
unrelated services: a different `target`, a different `path` on the same
target, or an absolute `url` with no target at all. Nothing in the runner
assumes the actions of a pipeline share a host.

Authentication in v1 is a static bearer token or API key, supplied as a
`${VAR}` header on the target — no auth machinery at all beyond header
templating. An action using an inline `url` carries its own `headers`, since
there is no target to inherit them from. Because at least one of these APIs does not exist yet, the target
keeps an optional `auth:` block as the seam for a future OAuth2
client-credentials handler (fetch, cache, refresh on 401). Nothing reads it
yet; it exists so adding one later is a new file under `core/`, not a reshaped
config.

| Key | Meaning |
| --- | --- |
| `base_url`, `headers` | usually from the `target` |
| `method`, `path` | `POST /bills` — joined onto the target's `base_url` |
| `url` | an absolute URL, used instead of `base_url` + `path` |
| `json` / `form` / `body` | request payload |
| `query` | query-string parameters |
| `timeout` | seconds, defaults from `defaults.http_timeout` |
| `retry` | attempts and backoff for connection errors and 5xx; 4xx never retries |
| `expect_status` | accepted statuses, default `2xx` |
| `money_format` | `number` (default) or `string`, per target |

`money` fields serialize as exact JSON numbers — `1234.50`, never `1234.5` and
never through a binary float. That needs `simplejson`, which encodes `Decimal`
natively; the stdlib `json` module cannot emit a `Decimal` as a number at all.
Targets whose API insists on strings can set `money_format: string`.

Sends an `Idempotency-Key` header derived from
`pipeline + message id + action id` (see below). Returns
`{"status": 201, "response": <parsed JSON or raw text>, "headers": {...}}`, so
templates read `{{ actions.save_bill.response.<field> }}`.

### `google_calendar`

| Key | Meaning |
| --- | --- |
| `calendar_id` | usually from the `target`; `primary` is the default |
| `summary`, `description`, `location` | event fields |
| `start_date` / `start_datetime`, `all_day`, `duration_minutes`, `timezone` | when |
| `reminders` | list of `{method, minutes}` |

Sets a deterministic `iCalUID`, so Google itself rejects a duplicate even if
local state was lost. Returns the created event.

### `python`

`module` + `function`, resolved from `actions/`. The escape hatch for anything
the builtins do not cover.

### `log`

Writes the rendered record to the log. Useful while developing a pipeline.

## Writing Python steps

Both registries accept plain modules dropped into their directory. No
decorator, no registration, no runner change — the config names the module and
the function.

An extractor in `extractors/bses_bill.py`:

```python
def extract_line_items(email, config, record):
    """Return a dict merged into the record (or a single value for a named step).

    email  -- the normalized Email: .id .subject .sender .date .html .text
              .attachments .headers .link
    config -- this step's config dict, minus the routing keys
    record -- fields extracted so far, read-only
    """
    ...
    return {"line_items": [...], "tax": Decimal("42.00")}
```

An action in `actions/notify_phone.py`:

```python
def run(record, config, context, dry_run=False):
    """Perform the side effect and return a JSON-serializable dict, or None.

    record  -- the extracted fields
    config  -- this action's config, already rendered
    context -- .email, .actions, .pipeline, .run
    dry_run -- when True, do everything except the side effect
    """
    ...
    return {"delivered": True}
```

Contract: raising means the action failed and will be retried on the next run;
returning normally means it succeeded and will not run again for that message.
Honouring `dry_run` is the author's responsibility — the runner cannot enforce
it.

Because config names a module and a function, **a pipeline config is
executable code**. That is fine for your own configs and is the point of the
feature; never run someone else's.

## Testing pipelines

Extraction is the fragile half of this system: it runs against HTML written by
someone else's marketing team, and when they redesign the template the failure
mode is a quietly empty record months later. So fixtures and offline replay
ship in v1, not after the first breakage.

```
tests/
  fixtures/
    electric_bill/
      18f2ab9c.html          written by --dump-body
      18f2ab9c.expected.json the record it should produce
  test_pipelines.py
```

- `--dump-body <id>` writes the HTML part, the text part, and the message
  metadata into `tests/fixtures/<pipeline>/`.
- `--explain --message-id <id>` prints the record it extracts.
- `--replay` runs the extract stage against every fixture with **no network and
  no actions**, and diffs against the `.expected.json` beside it.

`test_pipelines.py` is a thin pytest wrapper over `--replay`, so `pytest` after
a config edit answers "did I break the other four pipelines" in under a second.
Adding a pipeline becomes: dump a real message, iterate on selectors with
`--explain`, freeze the result as the expectation.

Fixtures are real personal mail, so `tests/fixtures/` is gitignored by default
with a note in the README; a `--redact` flag on `--dump-body` can strip
addresses and account numbers for anything worth committing.

## State and idempotency

State lives at `state/pipelines/<name>.json`, a separate directory from the
attachments runner's `state/<name>.json` so the two can share a pipeline name
without colliding. The log is `state/pipelines.log`.

```json
{
  "version": 1,
  "last_run_date": "2026-08-16",
  "messages": {
    "18f2ab9c...": {
      "last_seen": "2026-08-16T09:02:11+05:30",
      "actions": { "save_bill": "ok", "pay_reminder": "failed: 503" },
      "attempts": 2,
      "last_record": { "amount": "1234.50", "due_date": "2026-08-20" }
    }
  }
}
```

Tracking is **per message per action**, not per message. With more than one
sink, per-message tracking has no correct answer when the second action fails:
marking the message done drops the reminder silently, and leaving it undone
re-posts the bill. Per-action state replays only the entries that are not `ok`.

**A retry re-fetches and re-extracts; it never replays `last_record`.** That
field is written for debugging and read by nothing. The alternative — treating
state as the input to a retry — means round-tripping `Decimal` and `datetime`
through JSON and rehydrating them correctly, a serialization layer that has to
stay in sync with the type table and whose bugs appear only on the retry path,
which by definition already runs after something went wrong. A second Gmail
fetch costs one API call and deletes the whole problem.

The message map is capped **by age** (default 90 days), not by count. A count
cap can evict a message that is still inside the `overlap_days` window, which
puts it straight back through the pipeline for a second POST.

State is not the only defence. Every action also carries a deterministic
idempotency key, `sha256(pipeline + message_id + action_id)`, sent as
`Idempotency-Key` for HTTP and as `iCalUID` for calendar events, so a lost or
hand-edited state file cannot cause a double POST at a server that honours it.
This is cheap now and near-impossible to retrofit.

Its strength differs by sink, and the doc should not pretend otherwise. Google
genuinely enforces `iCalUID`, with the wrinkle that deleting an event and
re-running can 409 on the recycled id — treated as success. `Idempotency-Key`
is a convention a self-built API most likely ignores, so it is free insurance
rather than a guarantee: state remains the real protection.

Incremental scanning works as it does today: query narrowed by
`after:last_run_date - overlap_days`, with the overlap absorbing timezone
skew and delayed delivery.

## Authorization and scopes

`attachments_downloader` holds a `gmail.readonly` token at `state/token.json`.
A calendar action needs `calendar.events`, and requesting it would re-consent
and overwrite that file.

So: each action class declares the scopes it needs, the runner unions the
scopes the loaded config actually uses, and the token is cached per scope set
at `state/token_<hash>.json`. A pipelines config with no Google actions asks
for `gmail.readonly` only. The attachments runner's token is never touched.

Auth failures reuse the existing `AuthError` treatment — a printed, actionable
message rather than a traceback — moved into `core/auth.py`.

## CLI

```bash
python pipelines_runner.py                          # every pipeline
python pipelines_runner.py --pipeline electric_bill
python pipelines_runner.py --dry-run
```

| Flag | Effect |
| --- | --- |
| `--config PATH` | Defaults to `configs/pipelines_config.yaml` |
| `--env-file PATH` | Read `${VAR}` values from this file instead of `./.env`. `--no-env-file` skips it. |
| `--pipeline NAME` | Run one pipeline |
| `--dry-run` | Extract, render, and report every action without performing it. No state written. |
| `--message-id ID` | Run against one specific message, ignoring state |
| `--explain` | Extract and print the record as a table; run no actions |
| `--dump-body ID` | Write the HTML and text parts into `tests/fixtures/<pipeline>/` |
| `--replay` | Run extraction against saved fixtures, offline, and diff against expectations |
| `--only-action ID`, `--skip-action ID` | Restrict which actions run |
| `--since YYYY-MM-DD` | Override the incremental window |
| `--limit N` | Process at most N messages |
| `--reauth` | Sign in again |

The four development flags are not a nice-to-have. Writing a selector against a
utility company's marketing HTML is the actual work of adding a pipeline, and
`--dump-body` then `--explain --message-id` is the loop that work happens in.

## Failure semantics

| Situation | Behaviour |
| --- | --- |
| Required field missing | Per `on_missing_field`: `fail` records the message as failed and moves on (default), `skip` drops the message without recording it, `warn` continues with the field absent |
| Optional field missing | `default` if given, else the key is absent from the record |
| Template error | The message fails before **any** action runs, so a typo cannot leave a half-applied message behind |
| Action raises | That action is recorded failed with the reason; independent actions still run, and actions that reference it are recorded `blocked` |
| Action fails repeatedly | After `max_attempts` (default 5) the message is marked `dead` and skipped, with one loud log line. It never blocks the pipeline. |
| Gmail API error | Logged per message; the run continues |
| Auth error | Run stops, actionable message printed |

A run's exit status is non-zero if any message ended in a failed or dead state.

Every run also writes `state/pipelines/last_run.json` — the finished summary,
overwritten each time:

```json
{
  "started_at": "2026-08-16T06:00:02+05:30",
  "finished_at": "2026-08-16T06:00:19+05:30",
  "exit_status": 1,
  "pipelines": {
    "electric_bill":     { "matched": 1, "processed": 1, "failed": 0, "dead": 0 },
    "redfin_home_value": { "matched": 1, "processed": 0, "failed": 1, "dead": 0,
                           "reasons": ["18f2ab9c: required field 'estimate' not found"] }
  }
}
```

A file rather than a notifier: the runner does not acquire a delivery channel
it would then have to authenticate, retry, and monitor — and a notifier that
fails silently is worse than no notifier. Anything that wants to alert (a
launchd job, a menu-bar script, a `--replay` cron) reads this file, and the
failure reason is already sitting in it. `--dry-run` writes it too, to a
`last_run.dry.json`, so a preview never overwrites the real record.

## Deferred

Explicitly not in the first version:

- **An `llm` extractor.** `using: llm` with a prompt and a JSON schema fits the
  step registry with no new machinery, and is the right answer for HTML that
  resists selectors — Redfin redesigns that email regularly. Add it when a real
  email defeats regex and CSS, not before. Regex is free, deterministic, and
  testable.
- **Folding in the attachments flow.** Once `actions/save_attachment.py`
  exists, `attachments_config.yaml` is expressible as pipelines, which would
  delete a duplicated copy of auth, state, and logging. Worth doing only after
  this runner has proven itself on a few real flows.
- **A `json_body` extractor.** Consumer mail rarely carries a JSON body, and
  it is a small file in the step registry whenever one does.
- **OAuth2 for the `http` action.** The `auth:` seam is reserved; the handler
  is written when an API needs it.
- **Gmail write scopes** (labelling processed mail, archiving).
- **Concurrency.** Messages are processed serially. Volume here is a handful of
  emails per run.
