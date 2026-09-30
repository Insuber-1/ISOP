"""Measured operations KPIs from persisted probe samples and event history."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from statistics import median

from paths import app_path


def read_events(limit: int = 2000) -> list[dict]:
    path = app_path("event_history.jsonl")
    try:
        with open(path, "r", encoding="utf-8") as stream:
            rows = []
            for line in stream:
                try:
                    item = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(item, dict):
                    rows.append(item)
        return rows[-max(1, int(limit)):]
    except OSError:
        return []


def _event_epoch(event: dict):
    value = event.get("timestamp")
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OSError):
        return None


def calculate_kpis(samples: list[dict], events: list[dict], *, days: int = 30,
                   now: datetime | None = None,
                   start_at: datetime | None = None,
                   end_at: datetime | None = None) -> dict:
    """Calculate transparent KPIs for a selected time window; unknown time is excluded."""
    now_epoch = (now or datetime.now()).timestamp()
    end_epoch = end_at.timestamp() if end_at is not None else now_epoch
    start_epoch = (start_at.timestamp() if start_at is not None
                   else end_epoch - max(1, int(days)) * 86400)
    if end_epoch < start_epoch:
        start_epoch, end_epoch = end_epoch, start_epoch

    by_server = {}
    for row in samples or []:
        stamp = row.get("timestamp")
        try:
            epoch = float(stamp.timestamp()) if hasattr(stamp, "timestamp") else datetime.fromisoformat(str(stamp)).timestamp()
        except (TypeError, ValueError, OSError):
            continue
        by_server.setdefault(str(row.get("server", "")), []).append(
            (epoch, bool(row.get("online"))))

    uptime_seconds = observed_seconds = 0.0
    servers_observed = 0
    for history in by_server.values():
        history.sort(key=lambda sample: sample[0])
        preceding = [sample for sample in history if sample[0] < start_epoch]
        selected = [sample for sample in history if start_epoch <= sample[0] <= end_epoch]
        following = [sample for sample in history if sample[0] > end_epoch]
        history = ([preceding[-1]] if preceding else []) + selected + following[:1]
        server_observed = server_uptime = 0.0
        for (previous_time, previous_online), (current_time, _) in zip(history, history[1:]):
            raw_delta = current_time - previous_time
            overlap_start = max(previous_time, start_epoch)
            overlap_end = min(current_time, end_epoch)
            delta = overlap_end - overlap_start
            # Ignore long gaps: the application may have been closed, so they are unknown time.
            if 0 < delta and 0 < raw_delta <= 150:
                server_observed += delta
                if previous_online:
                    server_uptime += delta
        if server_observed > 0:
            servers_observed += 1
            observed_seconds += server_observed
            uptime_seconds += server_uptime

    recent_events = []
    latest_pre_window_health = {}
    for event in events or []:
        epoch = _event_epoch(event)
        if epoch is None:
            continue
        if start_epoch <= epoch <= end_epoch:
            recent_events.append((epoch, event))
        elif epoch < start_epoch and event.get("category") == "service_health":
            details = event.get("details") or {}
            name = str(details.get("container") or "")
            if name and (name not in latest_pre_window_health
                         or epoch > latest_pre_window_health[name][0]):
                latest_pre_window_health[name] = (epoch, event)
    for epoch, event in latest_pre_window_health.values():
        if (event.get("details") or {}).get("current") == "unhealthy":
            recent_events.append((epoch, event))
    recent_events.sort(key=lambda item: item[0])
    attempts = [event for _, event in recent_events
                if event.get("category") == "auto_remediation"
                and (event.get("details") or {}).get("phase", "attempt") == "attempt"]
    active_incidents = {}
    completed = []
    for epoch, event in recent_events:
        category = event.get("category")
        details = event.get("details") or {}
        name = str(details.get("container") or "")
        if category == "service_health" and details.get("current") == "unhealthy" and name:
            active_incidents.setdefault(name, {"started": epoch, "auto_heal": False})
        elif category == "auto_remediation_recovery" and name and event.get("outcome") == "success":
            incident = active_incidents.get(name)
            if incident:
                incident["auto_heal"] = True
        elif category == "service_health" and details.get("current") == "healthy" and name:
            incident = active_incidents.pop(name, None)
            if incident:
                duration = max(0.0, epoch - incident["started"])
                completed.append({"container": name, "duration": duration,
                                  "auto_heal": incident["auto_heal"]})

    recovery_durations = [item["duration"] for item in completed]
    auto_recovered = [item["duration"] for item in completed if item["auto_heal"]]
    untreated = [item["duration"] for item in completed if not item["auto_heal"]]
    successful_attempts = min(len(auto_recovered), len(attempts))
    prevented_seconds = None
    baseline_seconds = None
    if len(untreated) >= 3 and auto_recovered:
        baseline_seconds = median(untreated)
        prevented_seconds = sum(max(0.0, baseline_seconds - duration)
                                 for duration in auto_recovered)

    return {
        "window_days": max(1, int((end_epoch - start_epoch + 86399) // 86400)),
        "uptime_percent": (round(100.0 * uptime_seconds / observed_seconds, 3)
                           if observed_seconds else None),
        "uptime_seconds": round(uptime_seconds, 1),
        "observed_seconds": round(observed_seconds, 1),
        "servers_observed": servers_observed,
        "auto_heal_attempts": len(attempts),
        "auto_heal_successes": successful_attempts,
        "auto_heal_success_percent": (round(100.0 * successful_attempts / len(attempts), 1)
                                       if attempts else None),
        "recovered_incidents": len(completed),
        "average_recovery_seconds": (round(sum(recovery_durations) / len(recovery_durations), 1)
                                     if recovery_durations else None),
        "recovery_samples": len(recovery_durations),
        "untreated_baseline_incidents": len(untreated),
        "untreated_median_recovery_seconds": (round(baseline_seconds, 1)
                                              if baseline_seconds is not None else None),
        "estimated_prevented_seconds": (round(prevented_seconds, 1)
                                        if prevented_seconds is not None else None),
        "prevented_estimate_samples": len(auto_recovered) if prevented_seconds is not None else 0,
    }
