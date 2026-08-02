from __future__ import annotations

from datetime import datetime, timezone

from app.following_late import enrich_train_in_front_also_late
from app.schedule import is_train_early, is_train_late
from app.stops import lookup_stop


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _lerp(start: float, end: float, progress: float) -> float:
    return start + (end - start) * progress


def _progress_between(
    *,
    depart_at: datetime | None,
    arrive_at: datetime | None,
    now: datetime,
) -> float:
    if depart_at is None or arrive_at is None or arrive_at <= depart_at:
        return 0.5
    elapsed = (now - depart_at).total_seconds()
    duration = (arrive_at - depart_at).total_seconds()
    if duration <= 0:
        return 0.5
    return max(0.0, min(1.0, elapsed / duration))


def _schedule_flags(train: dict) -> dict[str, bool]:
    arrival_delay = train.get("trip_arrival_delay")
    departure_delay = train.get("trip_departure_delay")
    return {
        "is_late": is_train_late(arrival_delay, departure_delay),
        "is_early": is_train_early(arrival_delay, departure_delay),
    }


def interpolate_position(
    train: dict,
    *,
    departed_from_stop_id: str | None,
    now: datetime | None = None,
) -> dict | None:
    """Compute map coordinates for a train based on status and schedule."""
    now = now or datetime.now(timezone.utc)
    status = train.get("location_status")
    current_stop_id = train.get("location_stop_id")
    current_stop = lookup_stop(current_stop_id)
    if current_stop is None:
        return None

    if status in {None, "STOPPED_AT", "INCOMING_AT"}:
        lat, lon = current_stop["lat"], current_stop["lon"]
        return {
            **train,
            "lat": lat,
            "lon": lon,
            "from_lat": lat,
            "from_lon": lon,
            "to_lat": lat,
            "to_lon": lon,
            "depart_at": None,
            "arrive_at": None,
            "stop_name": current_stop["stop_name"],
            "position_mode": "at_stop",
            **_schedule_flags(train),
        }

    if status == "IN_TRANSIT_TO" and departed_from_stop_id:
        from_stop = lookup_stop(departed_from_stop_id)
        if from_stop is None:
            lat, lon = current_stop["lat"], current_stop["lon"]
            return {
                **train,
                "lat": lat,
                "lon": lon,
                "from_lat": lat,
                "from_lon": lon,
                "to_lat": lat,
                "to_lon": lon,
                "depart_at": None,
                "arrive_at": None,
                "stop_name": current_stop["stop_name"],
                "position_mode": "at_stop",
                **_schedule_flags(train),
            }

        depart_at = train.get("last_position_update") or train.get("collected_at")
        arrive_at = train.get("next_stop_arrival_time") or train.get("next_stop_departure_time")
        depart_dt = _parse_iso(depart_at)
        arrive_dt = _parse_iso(arrive_at)
        progress = _progress_between(depart_at=depart_dt, arrive_at=arrive_dt, now=now)
        from_lat, from_lon = from_stop["lat"], from_stop["lon"]
        to_lat, to_lon = current_stop["lat"], current_stop["lon"]

        return {
            **train,
            "lat": _lerp(from_lat, to_lat, progress),
            "lon": _lerp(from_lon, to_lon, progress),
            "from_lat": from_lat,
            "from_lon": from_lon,
            "to_lat": to_lat,
            "to_lon": to_lon,
            "depart_at": depart_at,
            "arrive_at": arrive_at,
            "stop_name": current_stop["stop_name"],
            "departed_from_stop_id": departed_from_stop_id,
            "departed_from_stop_name": from_stop["stop_name"],
            "position_mode": "interpolated",
            "interpolation_progress": round(progress, 3),
            **_schedule_flags(train),
        }

    lat, lon = current_stop["lat"], current_stop["lon"]
    return {
        **train,
        "lat": lat,
        "lon": lon,
        "from_lat": lat,
        "from_lon": lon,
        "to_lat": lat,
        "to_lon": lon,
        "depart_at": None,
        "arrive_at": None,
        "stop_name": current_stop["stop_name"],
        "position_mode": "at_stop",
        **_schedule_flags(train),
    }


def enrich_map_trains(trains: list[dict], departed_from: dict[str, str]) -> list[dict]:
    enriched: list[dict] = []
    for train in trains:
        position = interpolate_position(
            train,
            departed_from_stop_id=departed_from.get(train["train_id"]),
        )
        if position is not None:
            enriched.append(position)
    return enrich_train_in_front_also_late(enriched)
