from __future__ import annotations

from functools import lru_cache

from nyct_gtfs.gtfs_static_types import Stations

from app.transfer_complexes import transfer_complex_by_parent


@lru_cache
def _stations() -> Stations:
    return Stations()


def base_stop_id(stop_id: str | None) -> str | None:
    """Strip NYCT direction suffix (N/S) from a platform stop id."""
    if not stop_id:
        return None
    if stop_id.endswith(("N", "S")):
        return stop_id[:-1]
    return stop_id


def _parent_station_id(stop_id: str) -> str:
    row = _stations().stops.get(stop_id)
    if not row:
        return base_stop_id(stop_id) or stop_id

    parent = row.get("parent_station") or ""
    if parent:
        return parent
    return base_stop_id(stop_id) or stop_id


def colocation_site_id(stop_id: str | None) -> str | None:
    """Map a platform stop to a shared site id for colocation matching."""
    if not stop_id:
        return None

    parent = _parent_station_id(stop_id)
    return transfer_complex_by_parent().get(parent, parent)


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
