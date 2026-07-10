"""Pure-Python habit miner: reads audit.jsonl and mines repeatable time-of-day habits.

No network, no API calls, no store writes here — this module only *reads* the
audit log and *produces* `Candidate` objects. The caller (Task 1's proposal
pipeline) decides whether to phrase and queue them via `ProposalStore`.

Only allowed tool calls are considered ("verdict" == "allowed"). Each
supported tool has a conservative `INPUT_PROJECTION` — the subset of its
`input` keys that make a repeated call "the same action" (e.g. which light
and which action for `control_light`). Tools not listed are never mined:
better to miss a habit than to propose noise from volatile or free-text
fields (transcripts, search queries, continuous values).

Phase 5 only mines *time-of-day* habits, so every candidate has
`kind="profile"` with a `{"fact": "..."}` payload — there is no time-based
rule/trigger engine (Phase 3 rules are Home-Assistant-event-driven, not
time-driven). `kind="rule"` is reserved for a future event-correlation miner.
"""

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# tool name -> ordered list of `input` keys that define "the same action".
# Conservative on purpose: only actionable, repeatable device/media/app
# actions are listed. Free-text fields (transcripts, search queries) and
# continuous/volatile fields (temperature, volume, brightness) are excluded.
INPUT_PROJECTION: dict[str, list[str]] = {
    "control_light": ["entity_id", "action"],
    "control_switch": ["entity_id", "action"],
    "control_media": ["entity_id", "action"],
    "spotify_play": ["uri"],
    "launch_app": ["target"],
    "close_app": ["name"],
}

_LIGHT_SWITCH_VERBS = {"turn_on": "turns on", "turn_off": "turns off", "toggle": "toggles"}
_MEDIA_VERBS = {
    "media_play": "plays media on",
    "media_pause": "pauses media on",
    "media_stop": "stops media on",
    "media_next_track": "skips to the next track on",
    "media_previous_track": "goes to the previous track on",
    "volume_set": "sets the volume on",
    "turn_on": "turns on",
    "turn_off": "turns off",
}


def _light_or_switch_summary(vals: dict) -> str:
    action = vals.get("action")
    entity = vals.get("entity_id", "a device")
    verb = _LIGHT_SWITCH_VERBS.get(action, action or "controls")
    return f"{verb} {entity}"


def _media_summary(vals: dict) -> str:
    action = vals.get("action")
    entity = vals.get("entity_id", "a media player")
    verb = _MEDIA_VERBS.get(action, action or "controls")
    return f"{verb} {entity}"


def _spotify_play_summary(vals: dict) -> str:
    uri = vals.get("uri")
    return f"plays {uri} on Spotify" if uri else "resumes Spotify playback"


def _launch_app_summary(vals: dict) -> str:
    return f"launches {vals.get('target', 'an app')}"


def _close_app_summary(vals: dict) -> str:
    return f"closes {vals.get('name', 'an app')}"


_SUMMARY_BUILDERS = {
    "control_light": _light_or_switch_summary,
    "control_switch": _light_or_switch_summary,
    "control_media": _media_summary,
    "spotify_play": _spotify_play_summary,
    "launch_app": _launch_app_summary,
    "close_app": _close_app_summary,
}


@dataclass
class Candidate:
    """A mined habit, ready for the phrasing/proposal step.

    Attributes:
        signature: Stable dedupe key — sha256 of (tool, canonical input
            projection, representative hour), truncated to 16 hex chars.
        kind: "profile" (Phase 5 only emits this) or "rule" (reserved).
        payload: JSON-serializable data for the eventual write. For
            "profile", `{"fact": "..."}`.
        evidence: Supporting data for the phrasing step: tool, hour, count,
            days, and a sample raw input.
    """

    signature: str
    kind: str
    payload: dict
    evidence: dict


def _as_aware_utc(dt: datetime) -> datetime:
    """Normalize a datetime to timezone-aware UTC, treating naive input as UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _format_hour(hour: int) -> str:
    """Render a 0-23 hour as a natural-language 12-hour clock string, e.g. 19 -> '7pm'."""
    period = "am" if hour < 12 else "pm"
    hour12 = hour % 12
    if hour12 == 0:
        hour12 = 12
    return f"{hour12}{period}"


def _project_input(tool: str, tool_input: dict) -> dict | None:
    """Reduce a tool's `input` dict to its projected (repeatable) keys, or None if unmapped."""
    keys = INPUT_PROJECTION.get(tool)
    if keys is None:
        return None
    return {k: tool_input.get(k) for k in keys}


def _cluster_hours(hours: list[int], tolerance: int) -> list[list[int]]:
    """Single-link cluster a list of hours (with duplicates) by adjacency within `tolerance`.

    Returns a list of clusters, each a list of the original hour values
    (duplicates preserved) belonging to that cluster.
    """
    if not hours:
        return []
    unique_sorted = sorted(set(hours))
    hour_groups = [[unique_sorted[0]]]
    for h in unique_sorted[1:]:
        if h - hour_groups[-1][-1] <= tolerance:
            hour_groups[-1].append(h)
        else:
            hour_groups.append([h])
    return [[h for h in hours if h in set(group)] for group in hour_groups]


def _make_candidate(tool: str, canonical: dict, hours_in_cluster: list[int], sample_input: dict) -> Candidate:
    hour_counts = Counter(hours_in_cluster)
    max_count = max(hour_counts.values())
    rep_hour = min(h for h, c in hour_counts.items() if c == max_count)

    sig_material = json.dumps([tool, canonical, rep_hour], sort_keys=True, default=str)
    signature = hashlib.sha256(sig_material.encode("utf-8")).hexdigest()[:16]

    summary_fn = _SUMMARY_BUILDERS.get(tool)
    summary = summary_fn(canonical) if summary_fn else f"calls {tool}"
    fact = f"Usually {summary} around {_format_hour(rep_hour)}"

    return Candidate(
        signature=signature,
        kind="profile",
        payload={"fact": fact},
        evidence={
            "tool": tool,
            "hour": rep_hour,
            "count": len(hours_in_cluster),
            "days": None,  # filled in by caller (needs per-entry day data)
            "sample_input": sample_input,
        },
    )


def mine_candidates(
    audit_path: str,
    now: datetime | None = None,
    min_occurrences: int = 3,
    window_days: int = 14,
    hour_tolerance: int = 1,
) -> list[Candidate]:
    """Mine `audit_path` for repeatable time-of-day habits.

    Pure function: no network/API calls, no store writes, no wall-clock
    reads except the single default for `now`.

    Args:
        audit_path: Path to an audit.jsonl file (see security/audit.py for
            the entry shape). A missing file yields [].
        now: Reference "current time" for the recency window; defaults to
            datetime.now(timezone.utc). Naive datetimes are treated as UTC.
        min_occurrences: Minimum call count AND minimum distinct calendar
            days required for a pattern to qualify.
        window_days: Only entries within this many days of `now` are mined.
        hour_tolerance: Hours within this distance of each other are
            merged into the same time-of-day bucket (single-link).

    Returns:
        list[Candidate]: sorted by signature, deterministic for a given
            file + now.
    """
    now_utc = _as_aware_utc(now) if now is not None else datetime.now(timezone.utc)
    window = timedelta(days=window_days)

    try:
        with open(audit_path, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []

    # canonical group key -> list of (hour, day, raw_input) in file order
    groups: dict[tuple, list[tuple]] = defaultdict(list)

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
            if record.get("verdict") != "allowed":
                continue
            tool = record["tool"]
            canonical = _project_input(tool, record["input"])
            if canonical is None:
                continue
            ts = _as_aware_utc(datetime.fromisoformat(record["ts"]))
        except (json.JSONDecodeError, KeyError, ValueError, TypeError, AttributeError):
            continue

        age = now_utc - ts
        if age < timedelta(0) or age > window:
            continue

        local_dt = ts.astimezone()
        key = (tool, tuple(sorted(canonical.items(), key=lambda kv: kv[0])))
        groups[key].append((local_dt.hour, local_dt.date(), record["input"]))

    candidates: list[Candidate] = []
    for (tool, canonical_items), records in groups.items():
        canonical = dict(canonical_items)
        hours = [r[0] for r in records]
        clusters = _cluster_hours(hours, hour_tolerance)

        hour_to_cluster_idx: dict[int, int] = {}
        for idx, cluster in enumerate(clusters):
            for h in set(cluster):
                hour_to_cluster_idx[h] = idx

        cluster_records: dict[int, list[tuple]] = defaultdict(list)
        for hour, day, raw_input in records:
            cluster_records[hour_to_cluster_idx[hour]].append((hour, day, raw_input))

        for cluster_recs in cluster_records.values():
            count = len(cluster_recs)
            days = {r[1] for r in cluster_recs}
            if count < min_occurrences or len(days) < min_occurrences:
                continue
            hours_in_cluster = [r[0] for r in cluster_recs]
            sample_input = cluster_recs[0][2]
            candidate = _make_candidate(tool, canonical, hours_in_cluster, sample_input)
            candidate.evidence["days"] = len(days)
            candidates.append(candidate)

    candidates.sort(key=lambda c: c.signature)
    return candidates
