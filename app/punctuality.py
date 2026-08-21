from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.config import PUNCTUALITY_DAY_THRESHOLD, PUNCTUALITY_MIN_TRACKING_MINUTES
from app.schedule import is_on_time_or_early

NY_TZ = ZoneInfo("America/New_York")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def today_bounds_utc(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Calendar-day window in America/New_York, as UTC datetimes."""
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(NY_TZ)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _has_scorable_delay(arrival_delay: int | None, departure_delay: int | None) -> bool:
    return arrival_delay is not None or departure_delay is not None


def empty_day_punctuality() -> dict:
    return {
        "consistent_day": False,
        "day_on_time_rate": None,
        "day_on_time_samples": 0,
        "day_tracking_minutes": 0.0,
    }


def ny_calendar_date(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.astimezone(NY_TZ).date().isoformat()


def punctuality_bucket(now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    return int(now.timestamp()) // 300


def punctuality_from_counters(
    *,
    day_first_seen_at: str | None,
    collected_at: str | None,
    day_on_time_samples: int,
    day_good_samples: int,
) -> dict:
    """Build punctuality stats from running day counters on train_state."""
    if not day_first_seen_at or not collected_at:
        return empty_day_punctuality()

    first = _parse_iso(day_first_seen_at)
    latest = _parse_iso(collected_at)
    if first is None or latest is None:
        return empty_day_punctuality()

    tracking_minutes = max(0.0, (latest - first).total_seconds() / 60)
    scored_count = max(0, int(day_on_time_samples))
    good_count = max(0, int(day_good_samples))
    on_time_rate = good_count / scored_count if scored_count else None
    consistent_day = (
        tracking_minutes >= PUNCTUALITY_MIN_TRACKING_MINUTES
        and scored_count > 0
        and on_time_rate is not None
        and on_time_rate >= PUNCTUALITY_DAY_THRESHOLD
    )
    return {
        "consistent_day": consistent_day,
        "day_on_time_rate": round(on_time_rate, 3) if on_time_rate is not None else None,
        "day_on_time_samples": scored_count,
        "day_tracking_minutes": round(tracking_minutes, 1),
    }


def score_delay_sample(
    arrival_delay: int | None,
    departure_delay: int | None,
) -> bool | None:
    """Return True/False for an on-time sample, or None if not scorable."""
    if not _has_scorable_delay(arrival_delay, departure_delay):
        return None
    return is_on_time_or_early(arrival_delay, departure_delay)


def summarize_day_punctuality(samples: list[dict]) -> dict:
    """Summarize on-time/early rate since first observation today for one train."""
    if not samples:
        return empty_day_punctuality()

    times = [
        parsed
        for sample in samples
        if (parsed := _parse_iso(sample.get("collected_at"))) is not None
    ]
    if not times:
        return empty_day_punctuality()

    scored = [
        sample
        for sample in samples
        if _has_scorable_delay(sample.get("trip_arrival_delay"), sample.get("trip_departure_delay"))
    ]
    good_count = sum(
        1
        for sample in scored
        if is_on_time_or_early(sample.get("trip_arrival_delay"), sample.get("trip_departure_delay"))
    )
    return punctuality_from_counters(
        day_first_seen_at=_iso_or_none(min(times)),
        collected_at=_iso_or_none(max(times)),
        day_on_time_samples=len(scored),
        day_good_samples=good_count,
    )


def _iso_or_none(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat()


def apply_day_punctuality(
    trains: list[dict],
    stats_by_train_id: dict[str, dict],
    consist_ids_by_train_id: dict[str, int] | None = None,
) -> list[dict]:
    consist_ids = consist_ids_by_train_id or {}
    enriched: list[dict] = []
    for train in trains:
        stats = stats_by_train_id.get(train["train_id"], {})
        enriched.append(
            {
                **train,
                "feelings_consist_id": consist_ids.get(train["train_id"]),
                "consistent_day": stats.get("consistent_day", False),
                "day_on_time_rate": stats.get("day_on_time_rate"),
                "day_on_time_samples": stats.get("day_on_time_samples", 0),
                "day_tracking_minutes": stats.get("day_tracking_minutes", 0.0),
            }
        )
    return enriched
