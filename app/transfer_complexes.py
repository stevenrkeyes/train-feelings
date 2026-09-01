"""Transfer complexes derived from MTA GTFS transfers.txt.

Refresh app/gtfs/transfers.txt from the subway static feed when MTA updates it:
  curl -sL -o /tmp/gtfs_subway.zip https://rrgtfsfeeds.s3.amazonaws.com/gtfs_subway.zip
  unzip -p /tmp/gtfs_subway.zip transfers.txt > app/gtfs/transfers.txt
"""

from __future__ import annotations

import csv
from functools import lru_cache
from pathlib import Path

TRANSFERS_PATH = Path(__file__).resolve().parent / "gtfs" / "transfers.txt"


def _load_cross_stop_edges(path: Path) -> list[tuple[str, str]]:
    edges: list[tuple[str, str]] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            left = row["from_stop_id"]
            right = row["to_stop_id"]
            if left != right:
                edges.append((left, right))
    return edges


def _connected_components(edges: list[tuple[str, str]]) -> tuple[tuple[str, ...], ...]:
    parent: dict[str, str] = {}

    def find(stop_id: str) -> str:
        if stop_id not in parent:
            parent[stop_id] = stop_id
        while parent[stop_id] != stop_id:
            parent[stop_id] = parent[parent[stop_id]]
            stop_id = parent[stop_id]
        return stop_id

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for left, right in edges:
        union(left, right)

    clusters: dict[str, set[str]] = {}
    for left, right in edges:
        clusters.setdefault(find(left), set()).add(left)
        clusters.setdefault(find(right), set()).add(right)

    return tuple(
        tuple(sorted(members))
        for members in clusters.values()
        if len(members) >= 2
    )


@lru_cache
def load_transfer_complexes() -> tuple[tuple[str, ...], ...]:
    """Return parent stop id groups connected by walk transfers in transfers.txt."""
    return _connected_components(_load_cross_stop_edges(TRANSFERS_PATH))


@lru_cache
def transfer_complex_by_parent() -> dict[str, str]:
    """Map GTFS parent stop ids to a shared colocation site id."""
    mapping: dict[str, str] = {}
    for members in load_transfer_complexes():
        site_id = min(members)
        for stop_id in members:
            mapping[stop_id] = site_id
    return mapping
