from __future__ import annotations

import re

# NYCT ATS three-letter terminal/yard codes → rider-facing names (best-effort).
# No official MTA lookup table exists; names inferred from terminals and live feed usage.
LOCATION_NAMES: dict[str, str] = {
    "125": "125 St",
    "148": "148 St-Lenox",
    "14S": "14 St-Union Sq",
    "179": "179 St",
    "205": "205 St",
    "207": "Inwood-207 St",
    "239": "Eastchester-239 St",
    "241": "Wakefield-241 St",
    "242": "Wakefield-242 St",
    "34H": "34 St-Hudson Yards",
    "34S": "34 St-Hudson Yards",
    "8AV": "8th Ave",
    "95S": "95 St",
    "962": "962 Yard",
    "AST": "Astoria-Ditmars",
    "BBR": "Brooklyn Bridge",
    "BCR": "Broad Channel",
    "BDA": "Bedford Park Blvd",
    "BDN": "Church Ave",
    "BPK": "Bay Parkway",
    "BPL": "Bowling Green",
    "BRD": "Broad St",
    "BRN": "Bronx Park",
    "BWP": "Bay Parkway",
    "CHS": "Church Ave",
    "CRS": "Court Sq",
    "CTL": "Coney Island-Stillwell Av",
    "CTC": "Canarsie-Rockaway Pkwy",
    "CYN": "Coney Island",
    "DIT": "Ditmars Blvd",
    "DSB": "Dyre Ave",
    "DPM": "Ditmars Blvd",
    "DYR": "Dyre Ave",
    "ETS": "Eastchester",
    "EUC": "Euclid Av",
    "FAR": "Far Rockaway",
    "FKN": "Franklin Av",
    "FLA": "Flushing-Main St",
    "FLB": "Flatbush Av-Brooklyn College",
    "FLX": "Flushing-Main St",
    "FRP": "Far Rockaway",
    "GCS": "Grand Central",
    "GCT": "Grand Central",
    "HAR": "Harlem-125 St",
    "HRP": "Harlem-148 St",
    "HYS": "Hoyt-Schermerhorn",
    "INW": "Inwood-207 St",
    "JCT": "Jamaica Center",
    "KNG": "Kings Highway",
    "LEF": "Lefferts Blvd",
    "LCL": "Lexington Av/63 St",
    "MAR": "Marble Hill",
    "MET": "Middle Village-Metropolitan Av",
    "MST": "Main St-Flushing",
    "MYR": "Myrtle-Wyckoff Avs",
    "NLT": "New Lots Av",
    "NOR": "Norwood-205 St",
    "NWV": "Norwood-205 St",
    "OZN": "Ozone Park-Lefferts Blvd",
    "PAR": "Parkchester",
    "PEL": "Pelham Bay Park",
    "PPK": "Prospect Park",
    "P-A": "Parsons-Archer",
    "RPK": "Rockaway Park",
    "RPY": "Rockaway Parkway",
    "SFT": "South Ferry",
    "STG": "St George",
    "STC": "Times Sq",
    "STL": "Stillwell Av",
    "TNV": "Tompkinsville",
    "TSS": "Times Sq",
    "UTL": "Utica Av",
    "VCP": "Van Cortlandt Park",
    "WDL": "Woodlawn",
    "WHL": "Whitehall St",
    "WTC": "World Trade Center",
    "ZER": "Flatbush Av-Brooklyn College",
}

TRAIN_ID_PATTERN = re.compile(
    r"^(.)"  # trip type designator
    r"([A-Z0-9]+)"  # route / line (e.g. 1, L, SI, FS)
    r"\s+"
    r"(\d{4}\+?)"  # scheduled origin time
    r"\s*"
    r"([A-Z0-9-]{2,4})/"
    r"([A-Z0-9-]{2,4})"
    r"\s*$"
)


def parse_train_id(train_id: str | None) -> dict[str, str] | None:
    if not train_id:
        return None
    match = TRAIN_ID_PATTERN.match(train_id.strip())
    if not match:
        return None
    trip_type, line, origin_time, origin, destination = match.groups()
    return {
        "trip_type": trip_type,
        "line": line,
        "origin_time": origin_time.rstrip("+"),
        "origin": origin,
        "destination": destination,
    }


def format_train_display_name(train_id: str | None) -> str:
    if not train_id or train_id.startswith("trip:"):
        return "Special Train"

    parsed = parse_train_id(train_id)
    if parsed is None:
        return "Special Train"

    destination_name = LOCATION_NAMES.get(parsed["destination"])
    if not destination_name:
        return "Special Train"

    line = parsed["line"]
    return f"{destination_name} {line} Train {parsed['origin_time']}"
