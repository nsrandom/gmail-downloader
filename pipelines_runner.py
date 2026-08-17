#!/usr/bin/env python3
"""Generic email pipelines: match some mail, extract fields, act on them.

Each pipeline in the config searches Gmail, pulls a record (one JSON-shaped
dict) out of every new message, and hands it to one or more actions -- an HTTP
POST to an API you own, a calendar event, a Python function of your own.

    python pipelines_runner.py                              # every pipeline
    python pipelines_runner.py --pipeline electric_bill
    python pipelines_runner.py --dry-run                    # act on nothing
    python pipelines_runner.py --explain --message-id ID    # what would I extract?
    python pipelines_runner.py --replay                     # offline, against fixtures

See docs/pipelines.md for the config reference.
"""

import argparse
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import simplejson
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

from actions import (
    ROUTING_KEYS as ACTION_ROUTING_KEYS,
)
from actions import (
    ActionError,
    get_action,
    idempotency_key,
    load_python_action,
)
from core import gmail
from core.auth import (
    CALENDAR_EVENTS,
    GMAIL_READONLY,
    AuthError,
    Services,
    get_credentials,
    token_expired_message,
    token_path_for,
)
from core.coerce import json_safe
from core.config import ConfigError, load_config
from core.paths import (
    DEFAULT_CONFIG_PATH,
    DRY_RUN_SUMMARY_PATH,
    FIXTURE_DIR,
    LOG_PATH,
    RUN_SUMMARY_PATH,
    STATE_DIR,
)
from core.redact import redact
from core.state import OK, PipelineState, write_run_summary
from core.templating import render_value
from extractors import ExtractionError, MissingField, run_extract

EPILOG = """\
examples:
  pipelines_runner.py                                run every pipeline
  pipelines_runner.py --pipeline electric_bill       run one
  pipelines_runner.py --dry-run                      extract and render, act on nothing
  pipelines_runner.py --explain --message-id ID      print the record for one message
  pipelines_runner.py --dump-body ID --pipeline P    save a message as a test fixture
  pipelines_runner.py --replay                       re-extract from fixtures, offline

developing a pipeline:
  --dump-body saves a real message under tests/fixtures/<pipeline>/, --explain
  shows what the current config extracts from it, and --freeze records that as
  the expectation. --replay then re-checks every fixture with no network at
  all, so a sender redesigning their template shows up as a failing test rather
  than an empty record months later.

state:
  state/pipelines/<name>.json tracks which actions have run for which message,
  so a partial failure retries only what actually failed. state/pipelines/
  last_run.json holds the last run's summary, for anything that wants to alert
  on it. --dry-run touches neither.
"""

# Run-wide defaults an action inherits when neither it nor its target says
# otherwise. Keeps `defaults:` from being a table that only the runner reads.
ACTION_DEFAULTS = {
    "http": {"timeout": "http_timeout"},
    "google_calendar": {"timezone": "timezone"},
}


def setup_logging(verbose=False):
    STATE_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler(sys.stdout)],
    )


def _will_run(action_config, args):
    if args.only_action and action_config["id"] not in args.only_action:
        return False
    if args.skip_action and action_config["id"] in args.skip_action:
        return False
    return True


def required_scopes(config, pipelines, args=None):
    """The union of what the actions that will actually run need.

    A config with no Google actions asks for gmail.readonly alone, so it reuses
    the token attachments_downloader already has -- no second consent screen
    for the common case.

    Modes that perform nothing (--explain, --dump-body, --dry-run) and actions
    excluded by --skip-action ask for nothing extra either. Otherwise looking
    at what a calendar pipeline extracts would drag you through a consent
    screen for an event you were not going to create.
    """
    scopes = {GMAIL_READONLY}
    if args is not None and (args.explain or args.dump_body or args.dry_run):
        return sorted(scopes)

    for pipeline in pipelines:
        for action_config in pipeline["actions"]:
            if args is not None and not _will_run(action_config, args):
                continue
            kind = action_config.get("type") or config.target_type(action_config.get("target"))
            if kind == "google_calendar":
                scopes.add(CALENDAR_EVENTS)
    return sorted(scopes)


def resolve_action(config, action_config):
    """Merge a target's settings under an action's own, over the run defaults."""
    kind = action_config.get("type")
    merged = {}

    target_name = action_config.get("target")
    if target_name:
        target = config.target(target_name)
        kind = kind or target.get("type")
        merged.update({k: v for k, v in target.items() if k != "type"})

    own = {k: v for k, v in action_config.items() if k not in ACTION_ROUTING_KEYS}
    # Headers merge rather than replace, so an action can add one without
    # losing the target's Authorization.
    if "headers" in merged and "headers" in own:
        own = {**own, "headers": {**merged["headers"], **own["headers"]}}
    merged.update(own)

    if not kind:
        raise ActionError(f"action '{action_config['id']}' has no type, and no target gave one")

    # A run-wide default fills a key the action and its target both left alone,
    # so `defaults.timezone` reaches a calendar event without being restated on
    # every action that needs it.
    for key, default_key in ACTION_DEFAULTS.get(kind, {}).items():
        value = config.defaults.get(default_key)
        if key not in merged and value is not None:
            merged[key] = value

    return kind, merged


def build_context(record, email, pipeline_name, started_at):
    """What a template can see.

    Deliberately not in here: the results of other actions. Actions are
    independent -- each one sees the record and nothing else -- which is what
    makes a partial failure retry cleanly. See docs/pipelines_design.md.
    """
    return {
        **record,
        "email": email,
        "pipeline": {"name": pipeline_name},
        "run": {"started_at": started_at},
    }


def run_one_action(config, action_config, record, email, pipeline_name, started_at,
                   services, dry_run):
    kind, merged = resolve_action(config, action_config)
    context = build_context(record, email, pipeline_name, started_at)
    rendered = render_value(merged, context)

    runtime = {
        "services": services,
        "idempotency_key": idempotency_key(pipeline_name, email.id, action_config["id"]),
        "email": email,
        "pipeline": pipeline_name,
        "action_id": action_config["id"],
    }

    if kind == "python":
        fn = load_python_action(rendered.get("module"), rendered.get("function", "run"))
    else:
        fn = get_action(kind)
    return fn(record, rendered, runtime, dry_run=dry_run)


def process_message(config, pipeline, email, state, services, args, started_at, summary):
    """Extract from one message and run its actions. Returns True on success."""
    name = pipeline["name"]
    defaults = config.defaults

    try:
        record = run_extract(
            email,
            pipeline.get("extract"),
            pipeline.get("computed"),
            on_missing_field=defaults["on_missing_field"],
            label=f"[{name}] ",
        )
    except (MissingField, ExtractionError) as e:
        logging.error(f"  [{name}] {email.id}: {e}")
        summary["reasons"].append(f"{email.id}: {e}")
        if not args.dry_run:
            state.record_run(email.id, {}, record=None)
        return False

    if args.explain:
        print_record(name, email, record)
        return True

    # Every action's config is rendered before any of them runs, so a typo in
    # action 3 cannot leave a half-applied message behind. Actions excluded by
    # --only-action / --skip-action are left out: they will not run, so their
    # target's ${VAR}s have no business failing the message.
    prerender_context = build_context(record, email, name, started_at)
    for action_config in pipeline["actions"]:
        if not _will_run(action_config, args):
            continue
        try:
            _, merged = resolve_action(config, action_config)
            render_value(merged, prerender_context)
        except Exception as e:
            reason = f"{email.id}: action '{action_config['id']}' config: {e}"
            logging.error(f"  [{name}] {reason}")
            summary["reasons"].append(reason)
            if not args.dry_run:
                state.record_run(email.id, {}, record=json_safe_record(record))
            return False

    statuses = {}
    ok = True

    for action_config in pipeline["actions"]:
        action_id = action_config["id"]

        if not _will_run(action_config, args):
            continue
        if state.action_status(email.id, action_id) == OK:
            logging.debug(f"  [{name}] {email.id}: '{action_id}' already done")
            continue

        try:
            result = run_one_action(
                config, action_config, record, email, name, started_at,
                services, args.dry_run,
            )
            logging.debug(f"  [{name}] {email.id}: '{action_id}' -> {redact(result)}")
            statuses[action_id] = OK
        except Exception as e:
            reason = f"{email.id}: action '{action_id}' failed: {e}"
            logging.error(f"  [{name}] {reason}")
            summary["reasons"].append(reason)
            statuses[action_id] = f"failed: {e}"
            ok = False

    # No statuses means every action was already done (or excluded by
    # --only-action / --skip-action). Nothing was attempted, so counting it as
    # an attempt would walk a perfectly healthy message toward `dead`.
    if not args.dry_run and statuses:
        state.record_run(email.id, statuses, record=json_safe_record(record))
    return ok


def json_safe_record(record):
    """The record as plain JSON, for the debugging copy kept in state."""
    return simplejson.loads(simplejson.dumps(json_safe(record), use_decimal=False, default=str))


def print_record(pipeline_name, email, record):
    print(f"\n[{pipeline_name}] {email.id}  {email.date:%Y-%m-%d %H:%M}  {email.subject}")
    if not record:
        print("  (no fields extracted)")
        return
    width = max(len(k) for k in record)
    for key, value in record.items():
        print(f"  {key:<{width}}  {value!r}  ({type(value).__name__})")


def build_query(pipeline, state, overlap_days, since=None):
    parts = [pipeline["query"]]
    anchor = since or state.last_run_date
    if anchor:
        try:
            start = datetime.strptime(anchor, "%Y-%m-%d") - timedelta(days=overlap_days)
        except ValueError as e:
            where = "--since" if since else f"last_run_date in {state.path}"
            raise ConfigError(f"{where} should be YYYY-MM-DD, got {anchor!r} ({e})") from e
        parts.append(f"after:{start:%Y/%m/%d}")
    return " ".join(parts)


def run_pipeline(config, pipeline, services, args, started_at, summary):
    name = pipeline["name"]
    prefix = "[dry-run] " if args.dry_run else ""
    logging.info(f"{prefix}Running pipeline: {name}")

    state = PipelineState(name, read_only=args.dry_run)
    action_ids = [a["id"] for a in pipeline["actions"]]
    defaults = config.defaults

    if args.message_id:
        message_ids = [args.message_id]
        logging.info(f"  [{name}] one message requested: {args.message_id}")
    else:
        query = build_query(pipeline, state, defaults["overlap_days"], args.since)
        logging.info(f"  query: {query}")
        try:
            message_ids = gmail.search(services.gmail, query, limit=args.limit)
        except HttpError as e:
            logging.error(f"  [{name}] Gmail API error while listing messages: {e}")
            summary["reasons"].append(f"listing messages: {e}")
            return

    pending = []
    for message_id in message_ids:
        if args.message_id:
            pending.append(message_id)
        elif state.is_dead(message_id, defaults["max_attempts"]):
            summary["dead"] += 1
        elif not state.is_finished(message_id, action_ids):
            pending.append(message_id)

    summary["matched"] = len(message_ids)
    logging.info(f"  {prefix}[{name}] {len(message_ids)} matched, {len(pending)} to process")

    for message_id in pending:
        try:
            email = gmail.fetch(services.gmail, message_id, defaults["timezone"])
        except HttpError as e:
            logging.error(f"  [{name}] Gmail API error on message {message_id}: {e}")
            summary["reasons"].append(f"{message_id}: {e}")
            summary["failed"] += 1
            continue

        if args.dump_body:
            dump_fixture(name, email)
            continue

        if process_message(config, pipeline, email, state, services, args, started_at, summary):
            summary["processed"] += 1
        else:
            summary["failed"] += 1
            if state.is_dead(message_id, defaults["max_attempts"]):
                logging.error(
                    f"  [{name}] {message_id} has failed {defaults['max_attempts']} times; "
                    f"giving up on it. Fix the config, then delete its entry from "
                    f"{state.path} to retry."
                )
                state.mark_dead(message_id, action_ids)
                summary["dead"] += 1

    if args.dry_run:
        logging.info(f"  {prefix}[{name}] state not updated")
    elif not (args.explain or args.dump_body):
        # --explain and --dump-body run no actions, so advancing last_run_date
        # would make the next real run skip mail nothing has acted on yet.
        state.save(defaults["retain_days"])


def fixture_dir(pipeline_name):
    return FIXTURE_DIR / pipeline_name


def dump_fixture(pipeline_name, email):
    directory = fixture_dir(pipeline_name)
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    if email.html:
        path = directory / f"{email.id}.html"
        path.write_text(email.html)
        written.append(path)
    if email.text:
        path = directory / f"{email.id}.txt"
        path.write_text(email.text)
        written.append(path)
    meta = directory / f"{email.id}.json"
    meta.write_text(simplejson.dumps(gmail.to_fixture(email), indent=2))
    written.append(meta)
    for path in written:
        logging.info(f"  wrote fixture {path}")


def load_fixtures(pipeline_name):
    directory = fixture_dir(pipeline_name)
    if not directory.exists():
        return []
    out = []
    for meta_path in sorted(directory.glob("*.json")):
        if meta_path.name.endswith(".expected.json"):
            continue
        out.append(
            (
                gmail.from_fixture(
                    meta_path,
                    meta_path.with_suffix(".html"),
                    meta_path.with_suffix(".txt"),
                ),
                meta_path,
            )
        )
    return out


def canonical(record):
    return simplejson.dumps(json_safe(record), use_decimal=True, sort_keys=True, default=str)


def replay(config, pipelines, args):
    """Re-extract from saved fixtures, with no network and no actions."""
    failures = 0
    checked = 0

    for pipeline in pipelines:
        name = pipeline["name"]
        fixtures = load_fixtures(name)
        if not fixtures:
            logging.info(f"  [{name}] no fixtures in {fixture_dir(name)}")
            continue

        for email, meta_path in fixtures:
            checked += 1
            expected_path = meta_path.with_name(f"{meta_path.stem}.expected.json")
            try:
                record = run_extract(
                    email, pipeline.get("extract"), pipeline.get("computed"),
                    on_missing_field=config.defaults["on_missing_field"],
                    label=f"[{name}] ",
                )
            except (MissingField, ExtractionError) as e:
                logging.error(f"  FAIL [{name}] {email.id}: {e}")
                failures += 1
                continue

            if args.freeze:
                expected_path.write_text(canonical(record))
                logging.info(f"  froze [{name}] {email.id} -> {expected_path.name}")
                continue

            if not expected_path.exists():
                logging.warning(
                    f"  [{name}] {email.id}: no expectation yet "
                    f"(--freeze records the current result)"
                )
                print_record(name, email, record)
                continue

            expected = simplejson.loads(expected_path.read_text(), use_decimal=True)
            if canonical(record) == canonical(expected):
                logging.info(f"  ok   [{name}] {email.id}")
            else:
                failures += 1
                logging.error(f"  FAIL [{name}] {email.id}")
                logging.error(f"       expected {canonical(expected)}")
                logging.error(f"       got      {canonical(record)}")

    logging.info(f"replay: {checked} fixture(s), {failures} failure(s)")
    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser(
        description="Extract structured data from Gmail and act on it.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG,
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH),
                        help="Config file to read (default: %(default)s)")
    parser.add_argument("--env-file", help="Read ${VAR} values from here instead of ./.env")
    parser.add_argument("--no-env-file", action="store_true", help="Ignore .env entirely")
    parser.add_argument("--pipeline", action="append",
                        help="Run only this pipeline (repeatable)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Extract and render everything, perform no action, write no state")
    parser.add_argument("--message-id", help="Run against one specific message, ignoring state")
    parser.add_argument("--explain", action="store_true",
                        help="Print the extracted record and run no actions")
    parser.add_argument("--dump-body", action="store_true",
                        help="Save matched messages as fixtures under tests/fixtures/")
    parser.add_argument("--replay", action="store_true",
                        help="Re-extract from saved fixtures, offline, and check expectations")
    parser.add_argument("--freeze", action="store_true",
                        help="With --replay: record the current extraction as the expectation")
    parser.add_argument("--only-action", action="append", help="Run only this action (repeatable)")
    parser.add_argument("--skip-action", action="append", help="Skip this action (repeatable)")
    parser.add_argument("--since", help="Override the incremental window (YYYY-MM-DD)")
    parser.add_argument("--limit", type=int, help="Process at most this many messages")
    parser.add_argument("--reauth", action="store_true",
                        help="Ignore the saved token and sign in again. Needs a browser.")
    parser.add_argument("--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()

    setup_logging(args.verbose)

    try:
        config = load_config(args.config, args.env_file, not args.no_env_file)
    except ConfigError as e:
        logging.error(str(e))
        return 1

    pipelines = config.pipelines
    if args.pipeline:
        wanted = set(args.pipeline)
        pipelines = [p for p in pipelines if p["name"] in wanted]
        missing = wanted - {p["name"] for p in pipelines}
        if missing:
            logging.error(f"No pipeline named {', '.join(sorted(missing))} in {args.config}")
            return 1

    if args.replay:
        return replay(config, pipelines, args)

    if args.dry_run:
        logging.info("[dry-run] Nothing will be sent and no state will be written.")

    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    summaries = {}
    scopes = [GMAIL_READONLY]

    try:
        scopes = required_scopes(config, pipelines, args)
        logging.debug(f"scopes: {' '.join(scopes)} -> {token_path_for(scopes).name}")

        if args.dry_run:
            # A dry run asks for nothing extra, so say plainly what the real
            # run will want rather than letting the consent screen surprise it.
            for_real = required_scopes(config, pipelines)
            extra = set(for_real) - set(scopes)
            if extra:
                logging.info(
                    "[dry-run] a real run also needs: " + ", ".join(sorted(extra))
                    + " (one browser sign-in, saved separately from "
                    + f"{token_path_for([GMAIL_READONLY]).name})"
                )
        services = Services(get_credentials(scopes, force_reauth=args.reauth))

        for pipeline in pipelines:
            summary = {"matched": 0, "processed": 0, "failed": 0, "dead": 0, "reasons": []}
            summaries[pipeline["name"]] = summary
            try:
                run_pipeline(config, pipeline, services, args, started_at, summary)
            except (ConfigError, ActionError) as e:
                logging.error(f"  [{pipeline['name']}] {e}")
                summary["reasons"].append(str(e))
                summary["failed"] += 1
    except AuthError as e:
        logging.error(str(e))
        return 1
    except RefreshError as e:
        logging.error(token_expired_message(e, token_path_for(scopes)))
        return 1

    failed = sum(s["failed"] + s["dead"] for s in summaries.values())
    status = 1 if failed else 0

    if not args.explain:
        path = DRY_RUN_SUMMARY_PATH if args.dry_run else RUN_SUMMARY_PATH
        write_run_summary(path, redact({
            "started_at": started_at,
            "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "exit_status": status,
            "dry_run": args.dry_run,
            "pipelines": summaries,
        }))
        logging.info(f"summary: {path}")

    return status


if __name__ == "__main__":
    sys.exit(main())
