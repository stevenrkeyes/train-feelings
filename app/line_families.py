"""NYCT line families for old-friend matching.

Families come from route_color in MTA GTFS routes.txt (e.g. A/C/E share a color).
"""

from __future__ import annotations

import csv
from functools import lru_cache
from pathlib import Path

ROUTES_PATH = Path(__file__).resolve().parent / "gtfs" / "routes.txt"


@lru_cache
def _route_family_by_route_id() -> dict[str, str]:
    families: dict[str, str] = {}
    with ROUTES_PATH.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            route_id = row["route_id"]
            color = (row.get("route_color") or route_id).upper()
            families[route_id] = color
    return families


def line_family(route_id: str | None) -> str | None:
    if not route_id:
        return None
    return _route_family_by_route_id().get(route_id, route_id)


def qualifies_as_old_friend(route_a: str | None, route_b: str | None) -> bool:
    """True when two trains are on different NYCT line families."""
    if not route_a or not route_b:
        return False
    if route_a == route_b:
        return False
    family_a = line_family(route_a)
    family_b = line_family(route_b)
    if not family_a or not family_b:
        return False
    return family_a != family_b
