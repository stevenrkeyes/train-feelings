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
        coordinates = [[lon, lat] for _, lat, lon in points]
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
