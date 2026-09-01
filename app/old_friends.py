from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from app.config import OLD_FRIEND_DURATION_MINUTES, OLD_FRIEND_MIN_GAP_MINUTES
from app.stops import colocation_site_id

AT_STATION_STATUSES = frozenset({"STOPPED_AT"})


def _parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def canonical_consist_pair(left: int, right: int) -> tuple[int, int]:
    return (left, right) if left < right else (right, left)


def trains_at_station_by_consist(
    rows: list[dict],
    consist_by_train: dict[str, int],
) -> dict[str, dict[int, str]]:
    """Map colocation site id -> {feelings_consist_id: train_id} for trains stopped at a station."""
    by_stop: dict[str, dict[int, str]] = defaultdict(dict)
    seen_trains: set[str] = set()

    for row in rows:
        train_id = row["train_id"]
        if train_id in seen_trains:
            continue
        seen_trains.add(train_id)

        if row.get("location_status") not in AT_STATION_STATUSES:
            continue

        stop_id = colocation_site_id(row.get("location_stop_id"))
        consist_id = consist_by_train.get(train_id)
        if not stop_id or consist_id is None:
            continue

        by_stop[stop_id][consist_id] = train_id

    return by_stop


def colocation_pairs(
    by_stop: dict[str, dict[int, str]],
) -> list[tuple[str, int, int, str, str]]:
    """Return (stop_id, consist_a, consist_b, train_a_id, train_b_id) for each active pair."""
    pairs: list[tuple[str, int, int, str, str]] = []

    for stop_id, consists_at_stop in by_stop.items():
        consist_items = list(consists_at_stop.items())
        if len(consist_items) < 2:
            continue

        for index, (consist_a, train_a) in enumerate(consist_items):
            for consist_b, train_b in consist_items[index + 1 :]:
                pair = canonical_consist_pair(consist_a, consist_b)
                if pair[0] == consist_a:
                    pairs.append((stop_id, pair[0], pair[1], train_a, train_b))
                else:
                    pairs.append((stop_id, pair[0], pair[1], train_b, train_a))

    return pairs


def reunion_gap_elapsed(last_ended_at: str, collected_at: str) -> timedelta:
    return _parse_iso(collected_at) - _parse_iso(last_ended_at)


def should_trigger_reunion(last_ended_at: str, collected_at: str) -> bool:
    gap = reunion_gap_elapsed(last_ended_at, collected_at)
    return gap > timedelta(minutes=OLD_FRIEND_MIN_GAP_MINUTES)


def reunion_until(collected_at: str) -> str:
    until = _parse_iso(collected_at) + timedelta(minutes=OLD_FRIEND_DURATION_MINUTES)
    return until.isoformat()
