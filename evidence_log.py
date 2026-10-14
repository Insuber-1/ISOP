"""Append-only evidence records used by Vertical Chart PDF exports."""
import json
import os
import threading
from datetime import datetime

from paths import app_path


_LOCK = threading.Lock()
_PATH = app_path("operation_evidence.jsonl")


def record_evidence(kind: str, **data):
    """Persist one timestamped observation without allowing logging errors to disrupt the app."""
    entry = {"time": datetime.now().astimezone().isoformat(timespec="seconds"),
             "kind": str(kind), "data": data}
    try:
        os.makedirs(os.path.dirname(_PATH), exist_ok=True)
        with _LOCK, open(_PATH, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


def read_evidence(start=None, end=None):
    """Read records inside a local-time interval; malformed/partial lines are skipped."""
    try:
        with open(_PATH, "r", encoding="utf-8") as stream:
            lines = stream.readlines()
    except OSError:
        return []
    result = []
    for line in lines:
        try:
            entry = json.loads(line)
            at = datetime.fromisoformat(entry["time"])
            if at.tzinfo is not None:
                at = at.astimezone().replace(tzinfo=None)
            if start and at < start:
                continue
            if end and at > end:
                continue
            result.append(entry)
        except (ValueError, KeyError, TypeError):
            continue
    return result
