"""Per-pipeline state: which actions have already run for which message.

Tracking is per message *per action*. With more than one sink, per-message
tracking has no correct answer when the second action fails -- marking the
message done drops the reminder silently, and leaving it undone re-posts the
bill on the next run.

The `record` stored alongside is written for debugging and read by nothing. A
retry re-fetches and re-extracts instead, which avoids round-tripping Decimals
and datetimes through JSON on the one code path that only runs after something
has already gone wrong.
"""

import json
import logging
from datetime import datetime, timedelta

from core.paths import PIPELINE_STATE_DIR

OK = "ok"
DEAD = "dead"

STATE_VERSION = 1


def state_path_for(pipeline_name):
    return PIPELINE_STATE_DIR / f"{pipeline_name}.json"


class PipelineState:
    def __init__(self, pipeline_name, read_only=False):
        self.name = pipeline_name
        self.path = state_path_for(pipeline_name)
        self.read_only = read_only
        if self.path.exists():
            data = json.loads(self.path.read_text())
        else:
            data = {}
        self.version = data.get("version", STATE_VERSION)
        if self.version != STATE_VERSION:
            logging.warning(
                f"  [{pipeline_name}] {self.path} is version {self.version}, and this "
                f"runner writes version {STATE_VERSION}"
            )
        self.last_run_date = data.get("last_run_date")
        self.messages = data.get("messages", {})

    def entry(self, message_id):
        return self.messages.setdefault(message_id, {"actions": {}, "attempts": 0})

    def action_status(self, message_id, action_id):
        return self.messages.get(message_id, {}).get("actions", {}).get(action_id)

    def is_finished(self, message_id, action_ids):
        """True when every action for this message has succeeded or given up."""
        statuses = self.messages.get(message_id, {}).get("actions", {})
        if not statuses:
            return False
        return all(statuses.get(a) in (OK, DEAD) for a in action_ids)

    def is_dead(self, message_id, max_attempts):
        return self.messages.get(message_id, {}).get("attempts", 0) >= max_attempts

    def record_run(self, message_id, statuses, record=None):
        entry = self.entry(message_id)
        entry["actions"].update(statuses)
        entry["attempts"] = entry.get("attempts", 0) + 1
        entry["last_seen"] = datetime.now().astimezone().isoformat(timespec="seconds")
        if record is not None:
            entry["last_record"] = record

    def mark_dead(self, message_id, action_ids):
        """Give up on a message, without disturbing what already succeeded.

        An action recorded `ok` stays `ok`: it really did run, and a later
        clean-up that re-ran everything on this message would repeat it.
        """
        actions = self.entry(message_id)["actions"]
        for action_id in action_ids:
            if actions.get(action_id) != OK:
                actions[action_id] = DEAD

    def prune(self, retain_days):
        """Drop entries older than the retention window.

        Capped by age rather than by count on purpose: a count cap can evict a
        message that is still inside the overlap window, which puts it straight
        back through the pipeline for a second POST.
        """
        cutoff = datetime.now().astimezone() - timedelta(days=retain_days)
        kept = {}
        for message_id, entry in self.messages.items():
            seen = entry.get("last_seen")
            if not seen:
                kept[message_id] = entry
                continue
            try:
                if datetime.fromisoformat(seen) >= cutoff:
                    kept[message_id] = entry
            except ValueError:
                kept[message_id] = entry
        dropped = len(self.messages) - len(kept)
        if dropped:
            logging.debug(f"  [{self.name}] pruned {dropped} state entries older than "
                          f"{retain_days} days")
        self.messages = kept

    def save(self, retain_days=90):
        if self.read_only:
            return
        self.prune(retain_days)
        self.last_run_date = datetime.now().strftime("%Y-%m-%d")
        PIPELINE_STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "version": STATE_VERSION,
                    "last_run_date": self.last_run_date,
                    "messages": self.messages,
                },
                indent=2,
                sort_keys=True,
            )
        )


def write_run_summary(path, summary):
    PIPELINE_STATE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2))
    return path
