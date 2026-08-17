"""Tests for the pipelines runner.

Two kinds live here. Most are unit tests over the pieces that are easy to get
subtly wrong -- typed substitution, coercion, dependency inference, state. The
last one is the fixture replay: it re-extracts from real messages saved by
`--dump-body` and checks them against frozen expectations, so a sender
redesigning their email shows up as a failure here rather than as an empty
record months later.

Fixtures are real personal mail and gitignored, so that test skips when there
are none. The synthetic PG&E sample below is committed in its place, and keeps
the selector itself covered on a fresh clone.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
import simplejson
import yaml
from jinja2 import UndefinedError

from core.coerce import CoercionError, coerce, json_safe
from core.config import _Loader
from core.gmail import Email
from core.state import DEAD, OK, PipelineState
from core.templating import Expr, render_value

# The shape PG&E actually sends: both values are labelled by an image, so
# there is no text to anchor on -- only the structure and the image filename.
PGE_HTML = """
<html><body>
<p><span>Your paperless bill for account ending in ******1234-5 is now available.</span></p>
<table><tbody><tr>
  <td style="text-align:right"><img alt="" src="https://x/pge-kubra-amount-due.png"/></td>
  <td><span>&nbsp;<strong>$153.13</strong></span></td>
  <td style="text-align:right"><img alt="" src="https://x/pge-kubra-due-date.png"/></td>
  <td><span>&nbsp;<strong>09/03/2026 </strong></span></td>
</tr></tbody></table>
</body></html>
"""

PGE_STEPS = {
    "source": "html",
    "steps": [
        {
            "name": "amount",
            "using": "css",
            "selector": 'td:has(img[src*="amount-due"]) + td strong',
            "type": "money",
        },
        {
            "name": "due_date",
            "using": "css",
            "selector": 'td:has(img[src*="due-date"]) + td strong',
            "type": "date",
            "format": "%m/%d/%Y",
        },
        {
            "name": "account_no",
            "using": "regex",
            "source": "html_text",
            "pattern": r"account ending in \**([\w-]+)",
            "required": False,
        },
    ],
}


def make_email(html=PGE_HTML, **kwargs):
    return Email(
        id=kwargs.get("id", "test123"),
        thread_id="t1",
        headers={"subject": "Your PG&E Energy Statement is Ready to View",
                 "from": "DoNotReply@billpay.pge.com"},
        date=kwargs.get("date", datetime(2026, 8, 15, 12, 1)),
        html=html,
    )


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------

def test_extracts_the_pge_record():
    from extractors import run_extract

    record = run_extract(make_email(), PGE_STEPS)
    assert record["amount"] == Decimal("153.13")
    assert record["due_date"] == date(2026, 9, 3)
    assert record["account_no"] == "1234-5"


def test_computed_date_shift():
    from extractors import run_extract

    record = run_extract(
        make_email(), PGE_STEPS, {"remind_on": {"using": "date_shift", "from": "due_date",
                                                "days": -3}}
    )
    assert record["remind_on"] == date(2026, 8, 31)


def test_missing_required_field_raises():
    from extractors import MissingField, run_extract

    with pytest.raises(MissingField):
        run_extract(make_email(html="<html><body>nothing here</body></html>"), PGE_STEPS)


def test_optional_field_falls_back_to_default():
    from extractors import run_extract

    steps = {"steps": [{"name": "x", "using": "css", "selector": ".nope",
                        "required": False, "default": "unknown"}]}
    assert run_extract(make_email(), steps)["x"] == "unknown"


def test_unnamed_step_must_return_a_dict():
    from extractors import ExtractionError, run_extract

    steps = {"steps": [{"using": "css", "selector": "strong"}]}
    with pytest.raises(ExtractionError, match="must return a dict"):
        run_extract(make_email(), steps)


def test_selector_change_is_caught():
    """The failure this whole fixture story exists to surface."""
    from extractors import MissingField, run_extract

    redesigned = PGE_HTML.replace("pge-kubra-amount-due.png", "billing-amount-v2.png")
    with pytest.raises(MissingField, match="amount"):
        run_extract(make_email(html=redesigned), PGE_STEPS)


# --------------------------------------------------------------------------
# Fallbacks -- one field, several of a sender's layouts
# --------------------------------------------------------------------------

# The three layouts PG&E has actually used.
LAYOUT_IMAGE_LABELS = PGE_HTML
LAYOUT_TEXT_LABELS = """
<html><body><p>Your paperless bill for account ending in ******1234-5 is now available.</p>
<p>Statement balance:</p><p>$210.94</p>
<p>Payment due date:</p><p>10/04/2024</p></body></html>
"""
LAYOUT_PROSE = """
<html><body><p>The amount of $162.62 for account number ******1234-5
is due on 02/03/2023.</p></body></html>
"""

STEPS_WITH_FALLBACKS = {
    "source": "html",
    "steps": [
        {
            "name": "amount",
            "using": "css",
            "selector": 'td:has(img[src*="amount-due"]) + td strong',
            "type": "money",
            "fallbacks": [
                {"source": "html_text", "pattern": r"Statement balance:\s*\$\s*([\d,]+\.\d{2})"},
                {"source": "html_text", "pattern": r"The amount of \$([\d,]+\.\d{2})"},
            ],
        },
    ],
}


@pytest.mark.parametrize("html,expected", [
    (LAYOUT_IMAGE_LABELS, Decimal("153.13")),
    (LAYOUT_TEXT_LABELS, Decimal("210.94")),
    (LAYOUT_PROSE, Decimal("162.62")),
])
def test_fallbacks_cover_every_layout(html, expected):
    from extractors import run_extract

    record = run_extract(make_email(html=html), STEPS_WITH_FALLBACKS)
    assert record["amount"] == expected


# --------------------------------------------------------------------------
# extractors/pge_bill.py -- the same three layouts, as a Python step
# --------------------------------------------------------------------------

PGE_PYTHON_STEP = {"steps": [{"using": "python", "module": "pge_bill"}]}


@pytest.mark.parametrize("html,amount,due", [
    (LAYOUT_IMAGE_LABELS, Decimal("153.13"), date(2026, 9, 3)),
    (LAYOUT_TEXT_LABELS, Decimal("210.94"), date(2024, 10, 4)),
    (LAYOUT_PROSE, Decimal("162.62"), date(2023, 2, 3)),
])
def test_pge_module_reads_every_layout(html, amount, due):
    from extractors import run_extract

    record = run_extract(make_email(html=html), PGE_PYTHON_STEP)
    assert record["amount"] == amount
    assert record["due_date"] == due
    assert record["account_no"] == "1234-5"
    assert record["bill_date"] == date(2026, 8, 15)      # the day the mail arrived


def test_pge_module_runs_through_a_pipeline_step():
    """The step is unnamed, so its dict is merged into the record."""
    from extractors import run_extract

    record = run_extract(
        make_email(), PGE_PYTHON_STEP,
        {"remind_on": {"using": "date_shift", "from": "due_date", "days": -3}},
    )
    assert record == {"amount": Decimal("153.13"), "due_date": date(2026, 9, 3),
                      "bill_date": date(2026, 8, 15), "account_no": "1234-5",
                      "remind_on": date(2026, 8, 31)}


def test_pge_module_says_what_to_do_on_a_fourth_layout():
    from extractors import ExtractionError, run_extract

    with pytest.raises(ExtractionError, match="no PG&E layout matched"):
        run_extract(make_email(html="<html><body>redesigned again</body></html>"),
                    PGE_PYTHON_STEP)


def test_pge_module_account_number_is_optional():
    from extractors.pge_bill import extract

    html = LAYOUT_PROSE.replace("for account number ******1234-5\n", "")
    record = extract(make_email(html=html), {}, {})
    assert "account_no" not in record
    assert record["amount"] == Decimal("162.62")


def test_pge_module_needs_both_values_from_one_layout():
    """Half a layout is not a match -- it falls through rather than guessing."""
    from extractors import ExtractionError
    from extractors.pge_bill import extract

    half = "<html><body><p>Statement balance:</p><p>$210.94</p></body></html>"
    with pytest.raises(ExtractionError):
        extract(make_email(html=half), {}, {})


def test_fallback_infers_its_extractor_from_its_keys():
    """A fallback giving only a `pattern` does not have to restate `using`."""
    from extractors import _candidates

    step = {"name": "x", "using": "css", "selector": ".a",
            "fallbacks": [{"pattern": "b"}, {"selector": ".c"}]}
    kinds = [c["using"] for c in _candidates(step)]
    assert kinds == ["css", "regex", "css"]


def test_fallback_inherits_type_and_format():
    from extractors import run_extract

    steps = {"steps": [{
        "name": "due_date", "using": "css", "selector": ".nope",
        "type": "date", "format": "%m/%d/%Y", "source": "html",
        "fallbacks": [{"source": "html_text", "pattern": r"is due on (\d{2}/\d{2}/\d{4})"}],
    }]}
    assert run_extract(make_email(html=LAYOUT_PROSE), steps)["due_date"] == date(2023, 2, 3)


def test_all_candidates_failing_still_reports_missing():
    from extractors import MissingField, run_extract

    steps = {"steps": [{
        "name": "amount", "using": "css", "selector": ".nope", "type": "money",
        "fallbacks": [{"pattern": "nothing here either"}],
    }]}
    with pytest.raises(MissingField, match="tried 2 candidates"):
        run_extract(make_email(), steps)


def test_uncoercible_match_falls_through_to_the_next_candidate():
    """A match that will not coerce is no better than no match."""
    from extractors import run_extract

    html = "<html><body><p>Total: not-a-number</p><p>Balance: $42.00</p></body></html>"
    steps = {"steps": [{
        "name": "amount", "using": "regex", "source": "html_text",
        "pattern": r"Total: (\S+)", "type": "money",
        "fallbacks": [{"pattern": r"Balance: \$([\d.]+)"}],
    }]}
    assert run_extract(make_email(html=html), steps)["amount"] == Decimal("42.00")


def test_a_bad_pattern_raises_rather_than_falling_through():
    """A config mistake fails on every message; hiding it behind a fallback
    would turn it into a silent data gap."""
    from extractors import ExtractionError, run_extract

    steps = {"steps": [{
        "name": "amount", "using": "regex", "source": "html_text", "pattern": "([unclosed",
        "fallbacks": [{"pattern": r"(\d+)"}],
    }]}
    with pytest.raises(ExtractionError, match="bad pattern"):
        run_extract(make_email(), steps)


# --------------------------------------------------------------------------
# extractors/redfin_home_report.py
# --------------------------------------------------------------------------

REDFIN_SUBJECT = "1 Sample Street Home Report — 27 nearby homes listed recently"

# The four shapes Redfin has used. The label around the number moves every
# time; the address followed by the estimate is the only constant.
REDFIN_2017 = """<html><body>
<p>May 2017 Home Report</p><p>1 Sample Street</p>
<p>$1,250,000</p><p>Redfin Estimate</p><p>+$45,200</p>
</body></html>"""

REDFIN_2022 = """<html><body>
<p>JUNE 2022</p><p>Your Home Report</p><p>1 SAMPLE STREET</p>
<p>QUALIFIES FOR REDFIN PREMIER</p><p>$1,875,400</p><p>+18%</p>
</body></html>"""

REDFIN_2026 = """<html><body>
<p>August 2026 Home Report:</p><p>Your Home Estimate</p><p>1 Sample Street</p>
<p>$1,942,300</p><p>+$310K</p><p>since sold</p>
<p>Recently listed 12345:</p><p>$974</p><p>Median $/sq. ft.</p>
</body></html>"""


def redfin_email(html, subject=REDFIN_SUBJECT, when=datetime(2026, 8, 9, 10, 33)):
    return Email(id="r1", thread_id="t", headers={"subject": subject,
                 "from": "Redfin <redmail@redfin.com>"}, date=when, html=html)


@pytest.mark.parametrize("html,expected,month", [
    (REDFIN_2017, Decimal("1250000"), date(2017, 5, 1)),
    (REDFIN_2022, Decimal("1875400"), date(2022, 6, 1)),
    (REDFIN_2026, Decimal("1942300"), date(2026, 8, 1)),
])
def test_redfin_reads_every_layout(html, expected, month):
    from extractors.redfin_home_report import extract

    record = extract(redfin_email(html), {}, {})
    assert record["estimate"] == expected
    assert record["report_month"] == month
    assert record["address"] == "1 Sample Street"


def test_redfin_ignores_other_prices_on_the_page():
    """The page is full of listing and comp prices; only the estimate counts."""
    from extractors.redfin_home_report import extract

    assert extract(redfin_email(REDFIN_2026), {}, {})["estimate"] == Decimal("1942300")


def test_redfin_rejects_an_implausible_number():
    from extractors import ExtractionError
    from extractors.redfin_home_report import extract

    html = "<html><body><p>1 Sample Street</p><p>$1,234</p></body></html>"
    with pytest.raises(ExtractionError, match="out of range"):
        extract(redfin_email(html), {}, {})


def test_redfin_stops_looking_after_the_window():
    from extractors import ExtractionError
    from extractors.redfin_home_report import extract

    html = f"<html><body><p>1 Sample Street</p><p>{'filler ' * 200}</p>" \
           "<p>$1,942,300</p></body></html>"
    with pytest.raises(ExtractionError, match="no home estimate"):
        extract(redfin_email(html), {}, {})


def test_redfin_window_is_configurable():
    from extractors.redfin_home_report import extract

    html = f"<html><body><p>1 Sample Street</p><p>{'filler ' * 200}</p>" \
           "<p>$1,942,300</p></body></html>"
    assert extract(redfin_email(html), {"window": 3000}, {})["estimate"] == Decimal("1942300")


def test_redfin_takes_the_address_from_the_subject():
    """Nothing property-specific is baked into the module."""
    from extractors.redfin_home_report import extract

    html = "<html><body><p>12 Other Street</p><p>$900,000</p></body></html>"
    record = extract(redfin_email(html, subject="12 Other Street Home Report — 3 homes"),
                     {}, {})
    assert record["address"] == "12 Other Street"
    assert record["estimate"] == Decimal("900000")


def test_redfin_explicit_address_overrides_the_subject():
    from extractors.redfin_home_report import extract

    html = "<html><body><p>12 Other Street</p><p>$900,000</p></body></html>"
    record = extract(redfin_email(html, subject="Your monthly update"),
                     {"address": "12 Other Street"}, {})
    assert record["estimate"] == Decimal("900000")


def test_redfin_unparseable_subject_says_what_to_do():
    from extractors import ExtractionError
    from extractors.redfin_home_report import extract

    with pytest.raises(ExtractionError, match="Set `address:`"):
        extract(redfin_email(REDFIN_2026, subject="Your monthly update"), {}, {})


def test_redfin_falls_back_to_the_email_month():
    from extractors.redfin_home_report import extract

    html = "<html><body><p>1 Sample Street</p><p>$1,942,300</p></body></html>"
    record = extract(redfin_email(html, when=datetime(2026, 8, 9)), {}, {})
    assert record["report_month"] == date(2026, 8, 1)


# --------------------------------------------------------------------------
# Coercion
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("$153.13", Decimal("153.13")),
    ("₹1,234.50", Decimal("1234.50")),
    ("  $1,000  ", Decimal("1000")),
])
def test_money_coercion(raw, expected):
    assert coerce(raw, "money") == expected


def test_money_keeps_exact_digits_through_json():
    """153.13 must not become 153.13000000000001 via a binary float."""
    body = json_safe({"amount": coerce("$153.13", "money")})
    assert simplejson.dumps(body, use_decimal=True) == '{"amount": 153.13}'


def test_date_needs_a_format_when_not_iso():
    assert coerce("09/03/2026", "date", "%m/%d/%Y") == date(2026, 9, 3)
    with pytest.raises(CoercionError, match="ISO-8601"):
        coerce("09/03/2026", "date")


def test_string_type_does_not_become_a_number():
    """A zero-padded account number stays a string."""
    assert coerce("0012", "string") == "0012"


# --------------------------------------------------------------------------
# Templating
# --------------------------------------------------------------------------

def test_expr_keeps_the_python_type():
    out = render_value({"amount": Expr("amount")}, {"amount": Decimal("153.13")})
    assert out["amount"] == Decimal("153.13")


def test_expr_tag_parses_from_yaml():
    parsed = yaml.load("json:\n  amount: !expr amount\n", Loader=_Loader)
    assert isinstance(parsed["json"]["amount"], Expr)


def test_rendered_strings_stay_strings():
    out = render_value({"account": "{{ account_no }}"}, {"account_no": "0012"})
    assert out["account"] == "0012"


def test_typo_raises_rather_than_rendering_empty():
    with pytest.raises(UndefinedError):
        render_value("{{ amont }}", {"amount": 1})


def test_chained_reference_reaches_the_default_filter():
    """StrictUndefined alone raises on `.previous` before `default` can run."""
    out = render_value("{{ meter.previous.reading | default('?') }}", {})
    assert out == "?"


def test_regex_patterns_are_not_rendered():
    step = {"pattern": r"Due (\d{2})/(\d{2})", "selector": "{{ sel }}"}
    out = render_value(step, {"sel": ".x"}, skip_keys=("pattern",))
    assert out["pattern"] == r"Due (\d{2})/(\d{2})"
    assert out["selector"] == ".x"


def test_date_filter():
    assert render_value("{{ d | date('%Y-%m-%d') }}", {"d": date(2026, 9, 3)}) == "2026-09-03"


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def test_state_tracks_actions_separately(tmp_path, monkeypatch):
    import core.state as state_module

    monkeypatch.setattr(state_module, "PIPELINE_STATE_DIR", tmp_path)
    state = state_module.PipelineState("p")
    state.record_run("m1", {"save": OK, "remind": "failed: 503"})

    assert state.action_status("m1", "save") == OK
    # Not finished, so the next run retries -- but only the half that failed.
    assert not state.is_finished("m1", ["save", "remind"])
    assert state.is_finished("m1", ["save"])


def test_marking_dead_leaves_succeeded_actions_alone(tmp_path, monkeypatch):
    """`dead` means "stop trying", not "forget that half of it worked"."""
    import core.state as state_module

    monkeypatch.setattr(state_module, "PIPELINE_STATE_DIR", tmp_path)
    state = state_module.PipelineState("p")
    state.record_run("m1", {"save": OK, "remind": "failed: 503"})
    state.mark_dead("m1", ["save", "remind"])

    assert state.messages["m1"]["actions"] == {"save": OK, "remind": DEAD}


def test_state_prunes_by_age_not_count(tmp_path, monkeypatch):
    import core.state as state_module

    monkeypatch.setattr(state_module, "PIPELINE_STATE_DIR", tmp_path)
    state = state_module.PipelineState("p")
    state.messages = {
        "old": {"actions": {}, "last_seen":
                (datetime.now().astimezone() - timedelta(days=200)).isoformat()},
        "new": {"actions": {}, "last_seen": datetime.now().astimezone().isoformat()},
    }
    state.prune(90)
    assert set(state.messages) == {"new"}


def test_state_round_trips(tmp_path, monkeypatch):
    import core.state as state_module

    monkeypatch.setattr(state_module, "PIPELINE_STATE_DIR", tmp_path)
    state = state_module.PipelineState("p")
    state.record_run("m1", {"save": OK})
    state.save()

    assert PipelineState("p").action_status("m1", "save") == OK


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------

def test_idempotency_key_is_stable_and_scoped():
    from actions import idempotency_key

    assert idempotency_key("p", "m", "a") == idempotency_key("p", "m", "a")
    assert idempotency_key("p", "m", "a") != idempotency_key("p", "m", "b")
    assert idempotency_key("p", "m", "a") != idempotency_key("q", "m", "a")


def test_calendar_event_id_is_base32hex():
    from actions import idempotency_key
    from actions.google_calendar import _event_id

    event_id = _event_id(idempotency_key("p", "m", "a"))
    assert 5 <= len(event_id) <= 1024
    assert set(event_id) <= set("0123456789abcdefghijklmnopqrstuv")


# --------------------------------------------------------------------------
# The file action
# --------------------------------------------------------------------------

def _file_context(message_id="m1", action_id="save"):
    return {"email": make_email(id=message_id), "action_id": action_id}


def test_file_action_writes_jsonl(tmp_path):
    from actions.save_file import run

    path = tmp_path / "bills.jsonl"
    record = {"amount": Decimal("153.13"), "due_date": date(2026, 9, 3)}
    run(record, {"path": str(path)}, _file_context())

    written = simplejson.loads(path.read_text().strip(), use_decimal=True)
    assert written["amount"] == Decimal("153.13")
    assert written["due_date"] == "2026-09-03"


def test_file_action_keeps_money_exact(tmp_path):
    from actions.save_file import run

    path = tmp_path / "bills.jsonl"
    run({"amount": Decimal("153.13")}, {"path": str(path)}, _file_context())
    assert '"amount": 153.13' in path.read_text()


def test_file_action_dedupes_without_state(tmp_path):
    """State can be lost; the file itself must not gain a second row."""
    from actions.save_file import run

    path = tmp_path / "bills.jsonl"
    record = {"amount": Decimal("1.00")}
    first = run(record, {"path": str(path)}, _file_context())
    second = run(record, {"path": str(path)}, _file_context())

    assert first["written"] is True
    assert second["written"] is False and second["duplicate"] is True
    assert len(path.read_text().strip().splitlines()) == 1


def test_file_action_appends_a_different_message(tmp_path):
    from actions.save_file import run

    path = tmp_path / "bills.jsonl"
    run({"amount": Decimal("1.00")}, {"path": str(path)}, _file_context("m1"))
    run({"amount": Decimal("2.00")}, {"path": str(path)}, _file_context("m2"))
    assert len(path.read_text().strip().splitlines()) == 2


def test_file_action_writes_csv_with_a_header(tmp_path):
    from actions.save_file import run

    path = tmp_path / "bills.csv"
    record = {"amount": Decimal("153.13"), "due_date": date(2026, 9, 3)}
    run(record, {"path": str(path), "fields": ["amount", "due_date"]}, _file_context())
    run(record, {"path": str(path), "fields": ["amount", "due_date"]},
        _file_context(message_id="m2"))

    lines = path.read_text().strip().splitlines()
    assert lines[0] == "amount,due_date,_key"
    assert len(lines) == 3          # header plus two rows


def test_file_action_refuses_to_misalign_csv_columns(tmp_path):
    from actions import ActionError
    from actions.save_file import run

    path = tmp_path / "bills.csv"
    run({"amount": Decimal("1")}, {"path": str(path), "fields": ["amount"]}, _file_context())
    with pytest.raises(ActionError, match="already has columns"):
        run({"amount": Decimal("1"), "tax": Decimal("2")},
            {"path": str(path), "fields": ["amount", "tax"]}, _file_context("m2"))


def test_file_action_fields_must_exist(tmp_path):
    from actions import ActionError
    from actions.save_file import run

    with pytest.raises(ActionError, match="did not produce"):
        run({"amount": 1}, {"path": str(tmp_path / "x.jsonl"), "fields": ["nope"]},
            _file_context())


def test_file_action_text_format(tmp_path):
    from actions.save_file import run

    path = tmp_path / "bills.txt"
    run({}, {"path": str(path), "format": "text", "line": "hello"}, _file_context())
    assert path.read_text() == "hello\n"


def test_file_action_dry_run_writes_nothing(tmp_path):
    from actions.save_file import run

    path = tmp_path / "bills.jsonl"
    result = run({"amount": Decimal("1")}, {"path": str(path)}, _file_context(), dry_run=True)
    assert result["written"] is False
    assert not path.exists()


def test_file_action_creates_parent_directories(tmp_path):
    from actions.save_file import run

    path = tmp_path / "a" / "b" / "bills.jsonl"
    run({"amount": Decimal("1")}, {"path": str(path)}, _file_context())
    assert path.exists()


def test_file_action_overwrite_mode(tmp_path):
    from actions.save_file import run

    path = tmp_path / "latest.jsonl"
    run({"amount": Decimal("1")}, {"path": str(path), "mode": "overwrite"}, _file_context("m1"))
    run({"amount": Decimal("2")}, {"path": str(path), "mode": "overwrite"}, _file_context("m2"))
    assert len(path.read_text().strip().splitlines()) == 1
    assert '"amount": 2' in path.read_text()


# --------------------------------------------------------------------------
# The runner: resolving an action, and retrying half a message
# --------------------------------------------------------------------------

class _Args:
    """The subset of the parsed command line that process_message reads."""

    def __init__(self, **kwargs):
        self.dry_run = False
        self.explain = False
        self.dump_body = False
        self.only_action = None
        self.skip_action = None
        self.__dict__.update(kwargs)


def _config(actions, defaults=None, targets=None):
    from core.config import Config

    raw = {
        "defaults": defaults or {},
        "targets": targets or {},
        "pipelines": [{"name": "bills", "query": "x", "actions": actions}],
    }
    return Config(raw, "test-config.yaml")


def _run_actions(config, state, recorder, args=None):
    """Run the pipeline's actions against one message, returning True on success."""
    import pipelines_runner as runner
    from actions import _REGISTRY

    pipeline = config.pipelines[0]
    summary = {"matched": 0, "processed": 0, "failed": 0, "dead": 0, "reasons": []}
    saved = dict(_REGISTRY)
    _REGISTRY.update(recorder)
    try:
        return runner.process_message(
            config, pipeline, make_email(), state, None, args or _Args(),
            "2026-08-16T00:00:00", summary,
        )
    finally:
        _REGISTRY.clear()
        _REGISTRY.update(saved)


def test_a_failed_action_retries_alone_on_the_next_run(tmp_path, monkeypatch):
    """The whole point of per-action state.

    The earlier action succeeded, so the retry must leave it alone and run
    only the one that failed -- which is where an earlier design, letting a
    later action read `actions.<id>`, blocked the retry indefinitely instead.
    """
    import core.state as state_module

    monkeypatch.setattr(state_module, "PIPELINE_STATE_DIR", tmp_path)
    calls = []
    healthy = False

    def record_bill(record, config, context, dry_run=False):
        calls.append("record_bill")
        return {"status": 200, "response": {"id": "srv-1"}}

    def pay_reminder(record, config, context, dry_run=False):
        calls.append("pay_reminder")
        if not healthy:
            raise RuntimeError("503 from the calendar")
        return {"id": "ev-1"}

    config = _config([
        {"id": "record_bill", "type": "http", "url": "https://example.test/bills"},
        {"id": "pay_reminder", "type": "google_calendar", "summary": "Pay it"},
    ])
    recorder = {"http": record_bill, "google_calendar": pay_reminder}

    state = state_module.PipelineState("bills")
    assert _run_actions(config, state, recorder) is False
    assert calls == ["record_bill", "pay_reminder"]
    assert state.action_status("test123", "record_bill") == OK

    healthy = True
    calls.clear()
    assert _run_actions(config, state, recorder) is True
    assert calls == ["pay_reminder"]           # not record_bill, a second time
    assert state.action_status("test123", "pay_reminder") == OK


def test_referring_to_another_action_is_an_error_not_a_blank(tmp_path, monkeypatch):
    """Actions cannot read each other. Fail loudly rather than render nothing.

    Nothing is caught before any action runs, so the message is left untouched
    for the config to be fixed -- an empty `description` would have been the
    quiet alternative.
    """
    import core.state as state_module

    monkeypatch.setattr(state_module, "PIPELINE_STATE_DIR", tmp_path)
    calls = []
    config = _config([
        {"id": "record_bill", "type": "http", "url": "https://example.test/bills"},
        {"id": "pay_reminder", "type": "google_calendar",
         "summary": "Pay it", "description": "bill {{ actions.record_bill.response.id }}"},
    ])
    state = state_module.PipelineState("bills")
    ok = _run_actions(config, state,
                      {"http": lambda *a, **k: calls.append("http") or {},
                       "google_calendar": lambda *a, **k: calls.append("cal") or {}})

    assert ok is False
    assert calls == []
    assert state.action_status("test123", "record_bill") is None


def test_only_action_can_run_a_later_action_by_itself(tmp_path, monkeypatch):
    import core.state as state_module

    monkeypatch.setattr(state_module, "PIPELINE_STATE_DIR", tmp_path)
    calls = []

    def note(record, config, context, dry_run=False):
        calls.append(config.get("summary") or config.get("url"))
        return {}

    config = _config([
        {"id": "record_bill", "type": "http", "url": "https://example.test/bills"},
        {"id": "pay_reminder", "type": "google_calendar", "summary": "Pay it"},
    ])
    state = state_module.PipelineState("bills")
    ok = _run_actions(config, state, {"http": note, "google_calendar": note},
                      _Args(only_action=["pay_reminder"]))

    assert ok is True
    assert calls == ["Pay it"]


def test_a_skipped_action_does_not_need_its_targets_secrets(tmp_path, monkeypatch):
    """--skip-action means "do not go near it", ${VAR} included."""
    import core.state as state_module

    monkeypatch.setattr(state_module, "PIPELINE_STATE_DIR", tmp_path)
    monkeypatch.delenv("NOT_EXPORTED_ANYWHERE", raising=False)

    config = _config(
        [
            {"id": "log_it", "type": "log", "message": "hello"},
            {"id": "record_bill", "target": "finance_api", "path": "/bills"},
        ],
        targets={"finance_api": {
            "type": "http",
            "base_url": "https://example.test",
            "headers": {"Authorization": "Bearer ${NOT_EXPORTED_ANYWHERE}"},
        }},
    )
    state = state_module.PipelineState("bills")
    called = []

    ok = _run_actions(
        config, state,
        {"log": lambda *a, **k: called.append("log") or {}},
        _Args(skip_action=["record_bill"]),
    )
    assert ok is True and called == ["log"]


def test_defaults_reach_an_action_that_did_not_restate_them():
    from pipelines_runner import resolve_action

    config = _config(
        [{"id": "remind", "type": "google_calendar", "summary": "x"}],
        defaults={"timezone": "America/Los_Angeles", "http_timeout": 45},
    )
    kind, merged = resolve_action(config, config.pipelines[0]["actions"][0])
    assert (kind, merged["timezone"]) == ("google_calendar", "America/Los_Angeles")

    config = _config([{"id": "post", "type": "http", "url": "https://example.test"}],
                     defaults={"http_timeout": 45})
    _, merged = resolve_action(config, config.pipelines[0]["actions"][0])
    assert merged["timeout"] == 45


def test_an_action_overrides_the_default():
    from pipelines_runner import resolve_action

    config = _config([{"id": "post", "type": "http", "url": "https://x", "timeout": 5}],
                     defaults={"http_timeout": 45})
    _, merged = resolve_action(config, config.pipelines[0]["actions"][0])
    assert merged["timeout"] == 5


def test_action_headers_merge_with_the_targets():
    from pipelines_runner import resolve_action

    config = _config(
        [{"id": "post", "target": "api", "headers": {"X-Trace": "1"}}],
        targets={"api": {"type": "http", "base_url": "https://example.test",
                         "headers": {"Authorization": "Bearer t"}}},
    )
    _, merged = resolve_action(config, config.pipelines[0]["actions"][0])
    assert merged["headers"] == {"Authorization": "Bearer t", "X-Trace": "1"}


# --------------------------------------------------------------------------
# The calendar action
# --------------------------------------------------------------------------

def test_calendar_timed_event_carries_a_timezone():
    from actions.google_calendar import _timing

    block = _timing({"start_datetime": date(2026, 9, 3), "timezone": "America/Los_Angeles"})
    assert block["start"] == {"dateTime": "2026-09-03T09:00:00",
                              "timeZone": "America/Los_Angeles"}


def test_calendar_refuses_a_timed_event_with_no_timezone():
    """Google answers this with a 400; say so in terms the config can act on."""
    from actions import ActionError
    from actions.google_calendar import _timing

    with pytest.raises(ActionError, match="no time zone"):
        _timing({"start_datetime": date(2026, 9, 3)})


def test_calendar_all_day_event_needs_no_timezone():
    from actions.google_calendar import _timing

    block = _timing({"all_day": True, "start_date": date(2026, 9, 3)})
    assert block == {"start": {"date": "2026-09-03"}, "end": {"date": "2026-09-04"}}


# --------------------------------------------------------------------------
# Picking a PDF attachment
# --------------------------------------------------------------------------

def _pdf(filename, mime="application/pdf"):
    from core.gmail import Attachment

    return Attachment(filename=filename, mime_type=mime, attachment_id="a", size=1)


def test_pdf_filename_match_beats_attachment_order():
    """A leaflet attached ahead of the statement must not win on mime type."""
    from extractors.attachment_pdf import _select

    email = make_email()
    email.attachments = [_pdf("insert-offers.pdf"), _pdf("statement-aug.pdf")]
    assert _select(email, {"filename_match": "statement-*.pdf"}).filename == "statement-aug.pdf"


def test_pdf_explicit_pattern_matching_nothing_selects_nothing():
    from extractors.attachment_pdf import _select

    email = make_email()
    email.attachments = [_pdf("insert-offers.pdf")]
    assert _select(email, {"filename_match": "statement-*.pdf"}) is None


def test_pdf_falls_back_to_mime_type_when_unnamed():
    from extractors.attachment_pdf import _select

    email = make_email()
    email.attachments = [_pdf("", mime="application/pdf")]
    assert _select(email, {}) is not None


# --------------------------------------------------------------------------
# Fixture replay -- the regression net for real senders
# --------------------------------------------------------------------------

def _fixture_cases():
    from core.paths import DEFAULT_CONFIG_PATH, FIXTURE_DIR

    if not FIXTURE_DIR.exists() or not DEFAULT_CONFIG_PATH.exists():
        return []
    cases = []
    for pipeline_dir in sorted(FIXTURE_DIR.iterdir()):
        if not pipeline_dir.is_dir():
            continue
        for meta in sorted(pipeline_dir.glob("*.json")):
            if meta.name.endswith(".expected.json"):
                continue
            if meta.with_name(f"{meta.stem}.expected.json").exists():
                cases.append((pipeline_dir.name, meta))
    return cases


@pytest.mark.parametrize("pipeline_name,meta_path", _fixture_cases(),
                         ids=lambda v: getattr(v, "stem", v))
def test_fixtures_still_extract_as_expected(pipeline_name, meta_path):
    from core import gmail
    from core.config import load_config
    from core.paths import DEFAULT_CONFIG_PATH
    from extractors import run_extract
    from pipelines_runner import canonical

    config = load_config(DEFAULT_CONFIG_PATH)
    pipeline = config.pipeline(pipeline_name)
    if pipeline is None:
        pytest.skip(f"no pipeline named {pipeline_name} in the config")

    email = gmail.from_fixture(meta_path, meta_path.with_suffix(".html"),
                              meta_path.with_suffix(".txt"))
    record = run_extract(email, pipeline.get("extract"), pipeline.get("computed"))
    expected = simplejson.loads(
        meta_path.with_name(f"{meta_path.stem}.expected.json").read_text(), use_decimal=True
    )
    assert canonical(record) == canonical(expected)


def test_replay_skips_cleanly_with_no_fixtures():
    """A fresh clone has no fixtures (they are gitignored) and must still pass."""
    assert isinstance(_fixture_cases(), list)
