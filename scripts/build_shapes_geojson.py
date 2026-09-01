#!/usr/bin/env python3
"""Build subway route GeoJSON from MTA GTFS shapes.txt for the map overlay."""

from __future__ import annotations

import csv
import json
import zipfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GTFS_ZIP = ROOT / "data" / "gtfs" / "google_transit.zip"
OUTPUT = ROOT / "static" / "subway-shapes.geojson"
# ~15 m tolerance — enough detail for the map, far fewer points than raw GTFS shapes.
SIMPLIFY_TOLERANCE = 0.00015


def _perpendicular_distance(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    x0, y0 = point
    x1, y1 = start
    x2, y2 = end
    dx = x2 - x1
    dy = y2 - y1
    if dx == 0 and dy == 0:
        return ((x0 - x1) ** 2 + (y0 - y1) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((x0 - x1) * dx + (y0 - y1) * dy) / (dx * dx + dy * dy)))
    proj_x = x1 + t * dx
    proj_y = y1 + t * dy
    return ((x0 - proj_x) ** 2 + (y0 - proj_y) ** 2) ** 0.5


def _simplify_line(
    coordinates: list[list[float]],
    tolerance: float,
) -> list[list[float]]:
    if len(coordinates) <= 2:
        return coordinates

    start = coordinates[0]
    end = coordinates[-1]
    max_distance = 0.0
    index = 0
    for i in range(1, len(coordinates) - 1):
        distance = _perpendicular_distance(
            (coordinates[i][0], coordinates[i][1]),
            (start[0], start[1]),
            (end[0], end[1]),
        )
        if distance > max_distance:
            max_distance = distance
            index = i

    if max_distance > tolerance:
        left = _simplify_line(coordinates[: index + 1], tolerance)
        right = _simplify_line(coordinates[index:], tolerance)
        return left[:-1] + right
    return [start, end]


def _read_csv(zip_file: zipfile.ZipFile, name: str) -> list[dict[str, str]]:
    with zip_file.open(name) as handle:
        return list(csv.DictReader(line.decode("utf-8-sig") for line in handle))


def build() -> None:
    if not GTFS_ZIP.exists():
        raise SystemExit(f"GTFS zip not found: {GTFS_ZIP}")

    with zipfile.ZipFile(GTFS_ZIP) as zf:
        routes = {row["route_id"]: row for row in _read_csv(zf, "routes.txt")}
        trips = _read_csv(zf, "trips.txt")
        shapes_rows = _read_csv(zf, "shapes.txt")

    shape_to_route: dict[str, str] = {}
    for trip in trips:
        shape_id = trip["shape_id"]
        if shape_id and shape_id not in shape_to_route:
            shape_to_route[shape_id] = trip["route_id"]

    points_by_shape: dict[str, list[tuple[int, float, float]]] = defaultdict(list)
    for row in shapes_rows:
        shape_id = row["shape_id"]
        points_by_shape[shape_id].append(
            (
                int(row["shape_pt_sequence"]),
                float(row["shape_pt_lat"]),
                float(row["shape_pt_lon"]),
            )
        )

    features = []
    for shape_id, points in points_by_shape.items():
        route_id = shape_to_route.get(shape_id)
        if not route_id:
            continue

        route = routes.get(route_id)
        if not route:
            continue

        color = route.get("route_color", "808183").strip() or "808183"
        points.sort(key=lambda item: item[0])
        coordinates = _simplify_line(
            [[lon, lat] for _, lat, lon in points],
            SIMPLIFY_TOLERANCE,
        )
        if len(coordinates) < 2:
            continue

        features.append(
            {
                "type": "Feature",
                "properties": {
                    "route_id": route_id,
                    "route_name": route.get("route_short_name") or route_id,
                    "color": f"#{color}",
                    "shape_id": shape_id,
                },
                "geometry": {"type": "LineString", "coordinates": coordinates},
            }
        )

    features.sort(key=lambda feature: (feature["properties"]["route_id"], feature["properties"]["shape_id"]))

    geojson = {"type": "FeatureCollection", "features": features}
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(geojson, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {len(features)} shapes to {OUTPUT} ({OUTPUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    build()
