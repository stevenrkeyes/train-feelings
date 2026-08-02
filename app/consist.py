from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import CONSIST_REVERSAL_MAX_GAP_MINUTES
from app.train_id import parse_train_id

TERMINAL_APPROACH_STATUSES = frozenset({"STOPPED_AT", "INCOMING_AT", "IN_TRANSIT_TO"})


def _parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def opposite_direction(left: str | None, right: str | None) -> bool:
    if not left or not right or left == right:
        return False
    pair = {left, right}
    return pair == {"N", "S"}


def predecessor_end_terminal(parsed: dict[str, str] | None, location_status: str | None) -> str | None:
    """ATS terminal code when a train is finishing (or approaching) its trip destination."""
    if not parsed or location_status not in TERMINAL_APPROACH_STATUSES:
        return None
    return parsed["destination"]


def successor_origin_terminal(parsed: dict[str, str] | None) -> str | None:
    """ATS terminal code where a new trip begins (origin from the train ID)."""
    if not parsed:
        return None
    return parsed["origin"]


def reversal_gap_seconds(predecessor_last_seen_at: str, successor_first_seen_at: str) -> float:
    pred_last = _parse_iso(predecessor_last_seen_at)
    succ_first = _parse_iso(successor_first_seen_at)
    return (succ_first - pred_last).total_seconds()


def same_route_reversed(
    predecessor_route_id: str | None,
    successor_route_id: str | None,
) -> bool:
    return bool(
        predecessor_route_id
        and successor_route_id
        and predecessor_route_id == successor_route_id
    )


def reversal_link_priority(
    *,
    predecessor_route_id: str | None,
    successor_route_id: str | None,
    predecessor_last_seen_at: str,
    successor_first_seen_at: str,
) -> tuple[int, float]:
    """Lower sorts first: same route (reversed) beats cross-route, then shorter gap."""
    return (
        0 if same_route_reversed(predecessor_route_id, successor_route_id) else 1,
        reversal_gap_seconds(predecessor_last_seen_at, successor_first_seen_at),
    )


def can_link_terminal_reversal(
    *,
    predecessor_last_seen_at: str,
    successor_first_seen_at: str,
    predecessor_route_id: str | None,
    successor_route_id: str | None,
    predecessor_direction: str | None,
    successor_direction: str | None,
    predecessor_end_terminal: str | None,
    successor_origin_terminal: str | None,
    now: datetime | None = None,
) -> bool:
    """True when successor likely continues the same consist after a terminal turnaround."""
    if not predecessor_end_terminal or not successor_origin_terminal:
        return False
    if predecessor_end_terminal != successor_origin_terminal:
        return False
    if not opposite_direction(predecessor_direction, successor_direction):
        return False

    pred_last = _parse_iso(predecessor_last_seen_at)
    succ_first = _parse_iso(successor_first_seen_at)
    if succ_first <= pred_last:
        # Successor existed before (or at the same instant as) the predecessor ended.
        return False

    gap = succ_first - pred_last
    if gap > timedelta(minutes=CONSIST_REVERSAL_MAX_GAP_MINUTES):
        return False

    if now is not None and pred_last < now - timedelta(minutes=CONSIST_REVERSAL_MAX_GAP_MINUTES):
        return False

    return True


def snapshot_from_row(row: dict) -> dict:
    parsed = parse_train_id(row.get("train_id"))
    status = row.get("location_status")
    return {
        "train_id": row["train_id"],
        "route_id": row.get("route_id"),
        "direction": row.get("direction"),
        "location_status": status,
        "parsed": parsed,
        "end_terminal": predecessor_end_terminal(parsed, status),
        "origin_terminal": successor_origin_terminal(parsed),
    }
