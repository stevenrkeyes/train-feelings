from __future__ import annotations

from app.config import TRAIN_IN_FRONT_MAX_STOPS_AHEAD
from app.schedule import is_train_late


def _stops_ahead(train: dict, other: dict) -> int | None:
    train_seq = train.get("current_stop_sequence")
    other_seq = other.get("current_stop_sequence")
    if train_seq is None or other_seq is None:
        return None
    gap = other_seq - train_seq
    if gap < 1 or gap > TRAIN_IN_FRONT_MAX_STOPS_AHEAD:
        return None
    return gap


def _same_line(train: dict, other: dict) -> bool:
    if other["train_id"] == train["train_id"]:
        return False
    if other.get("route_id") != train.get("route_id"):
        return False
    if other.get("direction") != train.get("direction"):
        return False
    shape_id = train.get("shape_id")
    if not shape_id or other.get("shape_id") != shape_id:
        return False
    return True


def find_train_in_front_also_late(train: dict, trains: list[dict]) -> dict | None:
    """Return the closest late train 1–2 stops ahead on the same route, direction, and shape."""
    if not is_train_late(train.get("trip_arrival_delay"), train.get("trip_departure_delay")):
        return None
    if train.get("current_stop_sequence") is None:
        return None

    best: dict | None = None
    best_gap: int | None = None

    for other in trains:
        if not _same_line(train, other):
            continue
        gap = _stops_ahead(train, other)
        if gap is None:
            continue
        if not is_train_late(other.get("trip_arrival_delay"), other.get("trip_departure_delay")):
            continue
        if best_gap is None or gap < best_gap:
            best = other
            best_gap = gap

    return best


def enrich_train_in_front_also_late(trains: list[dict]) -> list[dict]:
    enriched: list[dict] = []
    for train in trains:
        ahead = find_train_in_front_also_late(train, trains)
        gap = _stops_ahead(train, ahead) if ahead else None
        enriched.append(
            {
                **train,
                "train_in_front_also_late": ahead is not None,
                "train_in_front_id": ahead["train_id"] if ahead else None,
                "train_in_front_stops_ahead": gap,
            }
        )
    return enriched
