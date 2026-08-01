from __future__ import annotations

from functools import lru_cache

from nyct_gtfs.gtfs_static_types import Stations


@lru_cache
def _stations() -> Stations:
    return Stations()


def lookup_stop(stop_id: str | None) -> dict | None:
    if not stop_id:
        return None

    row = _stations().stops.get(stop_id)
    if not row:
        return None

    return {
        "stop_id": stop_id,
        "stop_name": row.get("stop_name", stop_id),
        "lat": float(row["stop_lat"]),
        "lon": float(row["stop_lon"]),
    }


from app.schedule import is_train_late


def enrich_train_locations(trains: list[dict]) -> list[dict]:
    enriched = []
    for train in trains:
        stop = lookup_stop(train.get("location_stop_id"))
        if stop is None:
            continue
        late = is_train_late(train.get("trip_arrival_delay"), train.get("trip_departure_delay"))
        enriched.append({**train, **stop, "is_late": late})
    return enriched
