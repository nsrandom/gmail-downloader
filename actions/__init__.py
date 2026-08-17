"""The act stage: registered sinks that consume a record.

Actions are independent. Each one sees the record and nothing else -- not the
results of the actions before it -- which is what lets a partial failure retry
exactly the action that failed, on its own, without re-running its neighbours
or reasoning about what an earlier one would have returned this time.

An action returns a JSON-serializable dict (or None), which is logged at debug
level. Raising means the action failed and will be retried on the next run;
returning normally means it succeeded and will not run again for that message.
"""

import hashlib
import importlib
from pathlib import Path

_REGISTRY = {}

# Keys that route the action rather than configure it.
ROUTING_KEYS = ("id", "type", "target")


class ActionError(Exception):
    """An action could not complete. Retried on the next run."""


def action(name):
    def register(fn):
        _REGISTRY[name] = fn
        return fn
    return register


def get_action(name):
    if name in _REGISTRY:
        return _REGISTRY[name]
    raise ActionError(
        f"unknown action type '{name}' (known: {', '.join(sorted(_REGISTRY))}, "
        f"or `type: python` with a module in actions/)"
    )


def load_python_action(module_name, function_name):
    try:
        module = importlib.import_module(f"actions.{module_name}")
    except ImportError as e:
        available = sorted(
            p.stem for p in Path(__file__).parent.glob("*.py") if not p.stem.startswith("_")
        )
        raise ActionError(
            f"no module actions/{module_name}.py ({e}). Available: {', '.join(available)}"
        ) from e
    fn = getattr(module, function_name, None)
    if fn is None:
        raise ActionError(f"actions/{module_name}.py has no function '{function_name}'")
    return fn


def idempotency_key(pipeline_name, message_id, action_id):
    """A key that is the same on every retry and different for everything else.

    Sent as `Idempotency-Key` by the http action and used as the event id by
    the calendar action, so a lost or hand-edited state file cannot produce a
    duplicate at a server that honours it.
    """
    material = f"{pipeline_name}|{message_id}|{action_id}".encode()
    return hashlib.sha256(material).hexdigest()


# Registering the builtins. Imported for the side effect, at the bottom so the
# decorator above is defined by the time they run.
from actions import google_calendar, http, log, save_file  # noqa: E402,F401
