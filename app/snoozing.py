from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import SNORING_STATION_MINUTES

AT_STATION_STATUSES = frozenset({"STOPPED_AT", "INCOMING_AT"})


def _parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def station_dwell_minutes(dwell_since: str | None, now: datetime | None = None) -> int | None:
    if not dwell_since:
        return None
    now = now or datetime.now(timezone.utc)
    elapsed = now - _parse_iso(dwell_since)
    if elapsed < timedelta(0):
        return 0
    return int(elapsed.total_seconds() // 60)


def is_snoozing_at_station(dwell_since: str | None, now: datetime | None = None) -> bool:
    minutes = station_dwell_minutes(dwell_since, now)
    if minutes is None:
        return False
    return minutes >= SNORING_STATION_MINUTES


def apply_snoozing(
    trains: list[dict],
    dwell_since_by_train_id: dict[str, str] | None = None,
    now: datetime | None = None,
) -> list[dict]:
    dwell_since = dwell_since_by_train_id or {}
    now = now or datetime.now(timezone.utc)
    enriched: list[dict] = []
    for train in trains:
        since = dwell_since.get(train["train_id"])
        if not is_snoozing_at_station(since, now):
            enriched.append(train)
            continue
        minutes = station_dwell_minutes(since, now)
        enriched.append(
            {
                **train,
                "snoozing_at_station": True,
                "station_dwell_minutes": minutes,
            }
        )
    return enriched
