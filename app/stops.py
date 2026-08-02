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
