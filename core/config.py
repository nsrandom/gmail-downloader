"""Loading, validating, and environment-interpolating the pipelines config."""

import logging
import os
import re
from pathlib import Path

import yaml
from dotenv import load_dotenv

from core.paths import DEFAULT_ENV_PATH
from core.redact import remember_secret
from core.templating import Expr

_ENV_RE = re.compile(r"\$\{([A-Za-z_]\w*)\}")

DEFAULTS = {
    # Reading email dates, and placing calendar events. An explicit zone rather
    # than the machine's: a calendar event needs one to be accepted at all, and
    # a laptop that travels should not move when a bill is due.
    "timezone": "America/Los_Angeles",
    "overlap_days": 1,
    "http_timeout": 20,
    "on_missing_field": "fail",
    "max_attempts": 5,
    "retain_days": 90,
}

ON_MISSING_FIELD = ("fail", "skip", "warn")


class ConfigError(Exception):
    """A problem with the config file, phrased for the person who wrote it."""


class _Loader(yaml.SafeLoader):
    """SafeLoader that understands the `!expr` tag."""


_Loader.add_constructor("!expr", lambda loader, node: Expr(loader.construct_scalar(node)))


def load_env(env_file=None, use_env_file=True):
    """Populate os.environ from a .env file, without overriding what is set.

    The real environment wins, so `FOO=x python pipelines_runner.py` beats the
    same key in .env with nothing to edit. That is `load_dotenv`'s default and
    the reason it is a dependency rather than a hand-rolled parser -- quoting,
    escapes, and multi-line values are where the five-line version goes wrong.
    """
    if not use_env_file:
        return None
    path = Path(env_file) if env_file else DEFAULT_ENV_PATH
    if not path.exists():
        if env_file:
            raise ConfigError(f"--env-file {path} does not exist")
        return None
    load_dotenv(path, override=False)
    logging.debug(f"Read environment defaults from {path}")
    return path


def interpolate(node, where="config"):
    """Replace every ${VAR} in `node` from the environment."""
    if isinstance(node, str):
        def swap(match):
            name = match.group(1)
            value = os.environ.get(name)
            if value is None:
                raise ConfigError(
                    f"{where} refers to ${{{name}}}, which is not set. Export it, or "
                    f"add it to {DEFAULT_ENV_PATH.name}."
                )
            remember_secret(value)
            return value
        return _ENV_RE.sub(swap, node)
    if isinstance(node, dict):
        return {k: interpolate(v, where) for k, v in node.items()}
    if isinstance(node, list):
        return [interpolate(v, where) for v in node]
    return node


class Config:
    def __init__(self, raw, path):
        self.path = path
        self.defaults = {**DEFAULTS, **(raw.get("defaults") or {})}
        self.pipelines = raw.get("pipelines") or []
        self._targets = raw.get("targets") or {}
        self._resolved = {}

    def target(self, name):
        """Return a target with its ${VAR}s resolved.

        Resolution is deliberately lazy: only targets that a selected pipeline
        actually posts to are resolved, so running one pipeline never fails
        because an unrelated pipeline's API token happens not to be exported.
        """
        if name not in self._resolved:
            if name not in self._targets:
                known = ", ".join(sorted(self._targets)) or "none defined"
                raise ConfigError(f"No target named '{name}' (known targets: {known})")
            self._resolved[name] = interpolate(self._targets[name], f"target '{name}'")
        return self._resolved[name]

    def target_type(self, name):
        """A target's `type:` without resolving its ${VAR}s.

        Used to work out which OAuth scopes a config needs, which has to
        happen before anyone has been asked to export an API token.
        """
        return (self._targets.get(name) or {}).get("type")

    def pipeline(self, name):
        for p in self.pipelines:
            if p["name"] == name:
                return p
        return None


def load_config(path, env_file=None, use_env_file=True):
    load_env(env_file, use_env_file)

    path = Path(path)
    if not path.exists():
        raise ConfigError(
            f"No config file at {path}. Copy configs/pipelines_config.yaml.example "
            f"to {path.name} and edit it."
        )
    with open(path, "r") as f:
        raw = yaml.load(f, Loader=_Loader) or {}

    if not isinstance(raw, dict):
        raise ConfigError(f"{path} should be a mapping with a `pipelines:` key")

    config = Config(raw, path)
    _validate(config)
    return config


def _validate(config):
    if not config.pipelines:
        raise ConfigError(f"{config.path} defines no pipelines")

    if config.defaults["on_missing_field"] not in ON_MISSING_FIELD:
        raise ConfigError(
            f"defaults.on_missing_field must be one of {', '.join(ON_MISSING_FIELD)}"
        )

    seen = set()
    for pipeline in config.pipelines:
        for key in ("name", "query", "actions"):
            if key not in pipeline:
                raise ConfigError(f"A pipeline is missing required key '{key}': {pipeline}")

        name = pipeline["name"]
        if name in seen:
            raise ConfigError(f"Two pipelines are both named '{name}'")
        seen.add(name)
        if "/" in name or name.startswith("."):
            raise ConfigError(f"Pipeline name '{name}' is not usable as a file name")
        if name in ("last_run", "last_run.dry"):
            raise ConfigError(f"Pipeline name '{name}' is reserved for the run summary")

        extract = pipeline.get("extract") or {}
        steps = extract.get("steps") or []
        for step in steps:
            if "using" not in step:
                raise ConfigError(f"[{name}] an extract step is missing 'using': {step}")

        action_ids = set()
        for action in pipeline["actions"]:
            if "id" not in action:
                raise ConfigError(f"[{name}] an action is missing 'id': {action}")
            if action["id"] in action_ids:
                raise ConfigError(f"[{name}] two actions share the id '{action['id']}'")
            action_ids.add(action["id"])
            if "target" not in action and "type" not in action:
                raise ConfigError(
                    f"[{name}] action '{action['id']}' needs either a `target:` or a `type:`"
                )
