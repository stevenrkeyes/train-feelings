from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from app.config import (
    ARRIVAL_WINDOW_MINUTES,
    DATABASE_PATH,
    DATA_RETENTION_HOURS,
    HISTORY_LIMIT,
    SESSION_MAX_AGE_SECONDS,
)
from app.punctuality import (
    empty_day_punctuality,
    ny_calendar_date,
    punctuality_bucket,
    punctuality_from_counters,
    score_delay_sample,
)
from app.consist import (
    can_link_terminal_reversal,
    reversal_link_priority,
    snapshot_from_row,
    successor_origin_terminal,
)
from app.old_friends import (
    colocation_pairs,
    reunion_until,
    should_trigger_reunion,
    trains_at_station_by_consist,
)
from app.snoozing import AT_STATION_STATUSES, groggy_until_iso, is_groggy, is_snoozing_at_station

_db_lock = asyncio.Lock()

_locations_cache: list[dict] | None = None
_locations_cache_at: float | None = None
_locations_refresh_lock = asyncio.Lock()
LOCATIONS_CACHE_SECONDS = 15

_map_enrichment_cache: dict | None = None
_map_enrichment_cache_at: float | None = None
_map_enrichment_refresh_lock = asyncio.Lock()
MAP_ENRICHMENT_CACHE_SECONDS = 30


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _db_path(db_path: Path | None = None) -> Path:
    return db_path or DATABASE_PATH


def _db_connect(db_path: Path | None = None):
    return aiosqlite.connect(_db_path(db_path), timeout=30.0)


@asynccontextmanager
async def _db_write(db_path: Path | None = None):
    async with _db_lock:
        async with _db_connect(db_path) as db:
            yield db


async def init_db(db_path: Path | None = None) -> None:
    path = _db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    async with _db_connect(path) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS train_state (
                train_id TEXT PRIMARY KEY,
                trip_id TEXT,
                route_id TEXT,
                direction TEXT,
                shape_id TEXT,
                location_stop_id TEXT,
                location_status TEXT,
                current_stop_sequence INTEGER,
                trip_arrival_delay INTEGER,
                trip_departure_delay INTEGER,
                last_position_update TEXT,
                next_stop_arrival_time TEXT,
                next_stop_departure_time TEXT,
                feed_id TEXT NOT NULL,
                feed_timestamp TEXT NOT NULL,
                collected_at TEXT NOT NULL,
                last_stopped_at TEXT,
                departed_from_stop_id TEXT,
                dwell_since TEXT,
                groggy_until TEXT,
                day_bucket_date TEXT,
                day_first_seen_at TEXT,
                last_punctuality_bucket INTEGER,
                day_on_time_samples INTEGER NOT NULL DEFAULT 0,
                day_good_samples INTEGER NOT NULL DEFAULT 0,
                day_on_time_rate REAL,
                day_tracking_minutes REAL NOT NULL DEFAULT 0,
                consistent_day INTEGER NOT NULL DEFAULT 0,
                upcoming_stops_json TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_train_state_collected
                ON train_state(collected_at);

            CREATE TABLE IF NOT EXISTS feed_health (
                feed_id TEXT PRIMARY KEY,
                last_poll_success_at TEXT,
                last_feed_timestamp TEXT,
                status TEXT NOT NULL DEFAULT 'unknown',
                last_error TEXT
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            """
        )
        await db.commit()
        await _ensure_train_state_columns(db)
        await _init_consist_tables(db)
        await _init_old_friend_tables(db)


async def _ensure_train_state_columns(db: aiosqlite.Connection) -> None:
    cursor = await db.execute("PRAGMA table_info(train_state)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "groggy_until" not in columns:
        await db.execute("ALTER TABLE train_state ADD COLUMN groggy_until TEXT")
        await db.commit()


async def _init_consist_tables(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='feelings_consists'"
    )
    if await cursor.fetchone():
        cursor = await db.execute("PRAGMA table_info(feelings_consists)")
        columns = await cursor.fetchall()
        id_type = next((row[2] for row in columns if row[1] == "feelings_consist_id"), None)
        if id_type and id_type.upper() != "INTEGER":
            await db.executescript(
                """
                DROP TABLE IF EXISTS feelings_consist_trains;
                DROP TABLE IF EXISTS feelings_consists;
                """
            )

    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS feelings_consists (
            feelings_consist_id INTEGER PRIMARY KEY AUTOINCREMENT,
            route_id TEXT,
            started_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS feelings_consist_trains (
            train_id TEXT PRIMARY KEY,
            feelings_consist_id INTEGER NOT NULL,
            route_id TEXT,
            direction TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            last_end_terminal TEXT,
            predecessor_train_id TEXT,
            link_reason TEXT,
            FOREIGN KEY (feelings_consist_id) REFERENCES feelings_consists(feelings_consist_id)
        );

        CREATE INDEX IF NOT EXISTS idx_consist_trains_consist
            ON feelings_consist_trains(feelings_consist_id);
        CREATE INDEX IF NOT EXISTS idx_consist_trains_terminal
            ON feelings_consist_trains(route_id, last_end_terminal, last_seen_at);
        """
    )
    await db.commit()


async def _init_old_friend_tables(db: aiosqlite.Connection) -> None:
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS feelings_consist_colocations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            consist_a_id INTEGER NOT NULL,
            consist_b_id INTEGER NOT NULL,
            stop_id TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            CHECK (consist_a_id < consist_b_id)
        );

        CREATE INDEX IF NOT EXISTS idx_consist_colocations_pair_stop
            ON feelings_consist_colocations(consist_a_id, consist_b_id, stop_id);
        CREATE INDEX IF NOT EXISTS idx_consist_colocations_open
            ON feelings_consist_colocations(ended_at);

        CREATE TABLE IF NOT EXISTS feelings_consist_reunions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            consist_a_id INTEGER NOT NULL,
            consist_b_id INTEGER NOT NULL,
            stop_id TEXT NOT NULL,
            train_a_id TEXT NOT NULL,
            train_b_id TEXT NOT NULL,
            triggered_at TEXT NOT NULL,
            reunion_until TEXT NOT NULL,
            CHECK (consist_a_id < consist_b_id)
        );

        CREATE INDEX IF NOT EXISTS idx_consist_reunions_until
            ON feelings_consist_reunions(reunion_until);
        """
    )
    await db.commit()


async def create_session() -> str:
    token = secrets.token_urlsafe(32)
    now = _utc_now()
    expires = now + timedelta(seconds=SESSION_MAX_AGE_SECONDS)

    async with _db_write() as db:
        await db.execute(
            "INSERT INTO sessions (token, created_at, expires_at) VALUES (?, ?, ?)",
            (token, _iso(now), _iso(expires)),
        )
        await db.commit()

    return token


async def validate_session(token: str | None) -> bool:
    if not token:
        return False

    now = _iso(_utc_now())
    async with _db_connect() as db:
        cursor = await db.execute(
            "SELECT 1 FROM sessions WHERE token = ? AND expires_at > ?",
            (token, now),
        )
        row = await cursor.fetchone()
    return row is not None


async def prune_expired_sessions() -> None:
    now = _iso(_utc_now())
    async with _db_write() as db:
        await db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
        await db.commit()


def _derive_train_state(existing: dict | None, snapshot: dict, collected_at: str) -> dict:
    status = snapshot.get("location_status")
    stop_id = snapshot.get("location_stop_id")

    last_stopped_at = existing.get("last_stopped_at") if existing else None
    departed_from_stop_id = existing.get("departed_from_stop_id") if existing else None
    dwell_since = existing.get("dwell_since") if existing else None
    previous_dwell_since = dwell_since
    groggy_until = existing.get("groggy_until") if existing else None

    if status == "STOPPED_AT" and stop_id:
        last_stopped_at = stop_id
        departed_from_stop_id = None
    elif status == "IN_TRANSIT_TO":
        if last_stopped_at and last_stopped_at != stop_id:
            departed_from_stop_id = last_stopped_at
        else:
            departed_from_stop_id = None
    else:
        departed_from_stop_id = None

    if status in AT_STATION_STATUSES and stop_id:
        prev_status = existing.get("location_status") if existing else None
        prev_stop = existing.get("location_stop_id") if existing else None
        if (
            existing
            and dwell_since
            and prev_status in AT_STATION_STATUSES
            and prev_stop == stop_id
        ):
            pass
        else:
            dwell_since = collected_at
    else:
        dwell_since = None

    collected_dt = _parse_iso(collected_at) or _utc_now()
    was_snoozing = is_snoozing_at_station(previous_dwell_since, collected_dt)
    now_snoozing = is_snoozing_at_station(dwell_since, collected_dt) if dwell_since else False
    if was_snoozing and not now_snoozing:
        groggy_until = groggy_until_iso(collected_at, collected_dt)
    elif groggy_until and not is_groggy(groggy_until, collected_dt):
        groggy_until = None

    today = ny_calendar_date(collected_dt)
    bucket = punctuality_bucket(collected_dt)

    if existing and existing.get("day_bucket_date") == today:
        day_bucket_date = today
        day_first_seen_at = existing.get("day_first_seen_at") or collected_at
        last_bucket = existing.get("last_punctuality_bucket")
        day_on_time_samples = int(existing.get("day_on_time_samples") or 0)
        day_good_samples = int(existing.get("day_good_samples") or 0)
    else:
        day_bucket_date = today
        day_first_seen_at = collected_at
        last_bucket = None
        day_on_time_samples = 0
        day_good_samples = 0

    if last_bucket != bucket:
        scored = score_delay_sample(
            snapshot.get("trip_arrival_delay"),
            snapshot.get("trip_departure_delay"),
        )
        if scored is not None:
            day_on_time_samples += 1
            if scored:
                day_good_samples += 1
            last_bucket = bucket

    stats = punctuality_from_counters(
        day_first_seen_at=day_first_seen_at,
        collected_at=collected_at,
        day_on_time_samples=day_on_time_samples,
        day_good_samples=day_good_samples,
    )

    upcoming = snapshot.get("upcoming_stops") or []
    upcoming_stops_json = json.dumps(upcoming[:HISTORY_LIMIT])

    return {
        "train_id": snapshot["train_id"],
        "trip_id": snapshot.get("trip_id"),
        "route_id": snapshot.get("route_id"),
        "direction": snapshot.get("direction"),
        "shape_id": snapshot.get("shape_id"),
        "location_stop_id": stop_id,
        "location_status": status,
        "current_stop_sequence": snapshot.get("current_stop_sequence"),
        "trip_arrival_delay": snapshot.get("trip_arrival_delay"),
        "trip_departure_delay": snapshot.get("trip_departure_delay"),
        "last_position_update": snapshot.get("last_position_update"),
        "next_stop_arrival_time": snapshot.get("next_stop_arrival_time"),
        "next_stop_departure_time": snapshot.get("next_stop_departure_time"),
        "collected_at": collected_at,
        "last_stopped_at": last_stopped_at,
        "departed_from_stop_id": departed_from_stop_id,
        "dwell_since": dwell_since,
        "groggy_until": groggy_until,
        "day_bucket_date": day_bucket_date,
        "day_first_seen_at": day_first_seen_at,
        "last_punctuality_bucket": last_bucket,
        "day_on_time_samples": day_on_time_samples,
        "day_good_samples": day_good_samples,
        "day_on_time_rate": stats["day_on_time_rate"],
        "day_tracking_minutes": stats["day_tracking_minutes"],
        "consistent_day": 1 if stats["consistent_day"] else 0,
        "upcoming_stops_json": upcoming_stops_json,
    }


async def upsert_train_states(
    feed_id: str,
    feed_timestamp: datetime,
    snapshots: list[dict],
) -> None:
    if not snapshots:
        return

    collected_at = _iso(_utc_now())
    feed_ts = _iso(feed_timestamp)
    train_ids = [snapshot["train_id"] for snapshot in snapshots]

    async with _db_write() as db:
        db.row_factory = aiosqlite.Row
        existing_by_id: dict[str, dict] = {}
        if train_ids:
            placeholders = ",".join("?" * len(train_ids))
            cursor = await db.execute(
                f"SELECT * FROM train_state WHERE train_id IN ({placeholders})",
                train_ids,
            )
            existing_by_id = {row["train_id"]: dict(row) for row in await cursor.fetchall()}

        values = []
        for snapshot in snapshots:
            derived = _derive_train_state(
                existing_by_id.get(snapshot["train_id"]),
                snapshot,
                collected_at,
            )
            values.append(
                (
                    derived["train_id"],
                    derived["trip_id"],
                    derived["route_id"],
                    derived["direction"],
                    derived["shape_id"],
                    derived["location_stop_id"],
                    derived["location_status"],
                    derived["current_stop_sequence"],
                    derived["trip_arrival_delay"],
                    derived["trip_departure_delay"],
                    derived["last_position_update"],
                    derived["next_stop_arrival_time"],
                    derived["next_stop_departure_time"],
                    feed_id,
                    feed_ts,
                    derived["collected_at"],
                    derived["last_stopped_at"],
                    derived["departed_from_stop_id"],
                    derived["dwell_since"],
                    derived["groggy_until"],
                    derived["day_bucket_date"],
                    derived["day_first_seen_at"],
                    derived["last_punctuality_bucket"],
                    derived["day_on_time_samples"],
                    derived["day_good_samples"],
                    derived["day_on_time_rate"],
                    derived["day_tracking_minutes"],
                    derived["consistent_day"],
                    derived["upcoming_stops_json"],
                )
            )

        await db.executemany(
            """
            INSERT INTO train_state (
                train_id, trip_id, route_id, direction, shape_id,
                location_stop_id, location_status, current_stop_sequence,
                trip_arrival_delay, trip_departure_delay, last_position_update,
                next_stop_arrival_time, next_stop_departure_time,
                feed_id, feed_timestamp, collected_at,
                last_stopped_at, departed_from_stop_id, dwell_since, groggy_until,
                day_bucket_date, day_first_seen_at, last_punctuality_bucket,
                day_on_time_samples, day_good_samples, day_on_time_rate,
                day_tracking_minutes, consistent_day, upcoming_stops_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(train_id) DO UPDATE SET
                trip_id = excluded.trip_id,
                route_id = excluded.route_id,
                direction = excluded.direction,
                shape_id = excluded.shape_id,
                location_stop_id = excluded.location_stop_id,
                location_status = excluded.location_status,
                current_stop_sequence = excluded.current_stop_sequence,
                trip_arrival_delay = excluded.trip_arrival_delay,
                trip_departure_delay = excluded.trip_departure_delay,
                last_position_update = excluded.last_position_update,
                next_stop_arrival_time = excluded.next_stop_arrival_time,
                next_stop_departure_time = excluded.next_stop_departure_time,
                feed_id = excluded.feed_id,
                feed_timestamp = excluded.feed_timestamp,
                collected_at = excluded.collected_at,
                last_stopped_at = excluded.last_stopped_at,
                departed_from_stop_id = excluded.departed_from_stop_id,
                dwell_since = excluded.dwell_since,
                groggy_until = excluded.groggy_until,
                day_bucket_date = excluded.day_bucket_date,
                day_first_seen_at = excluded.day_first_seen_at,
                last_punctuality_bucket = excluded.last_punctuality_bucket,
                day_on_time_samples = excluded.day_on_time_samples,
                day_good_samples = excluded.day_good_samples,
                day_on_time_rate = excluded.day_on_time_rate,
                day_tracking_minutes = excluded.day_tracking_minutes,
                consistent_day = excluded.consistent_day,
                upcoming_stops_json = excluded.upcoming_stops_json
            """,
            values,
        )
        await db.commit()

    global _locations_cache, _locations_cache_at, _map_enrichment_cache, _map_enrichment_cache_at
    _locations_cache = None
    _locations_cache_at = None
    _map_enrichment_cache = None
    _map_enrichment_cache_at = None


async def finalize_poll_enrichment(rows: list[dict]) -> None:
    if not rows:
        return
    collected_at = _iso(_utc_now())
    await update_consists_from_poll(rows, collected_at)
    await update_colocations_from_poll(rows, collected_at)


def _snapshots_from_rows(rows: list[dict]) -> list[dict]:
    by_train: dict[str, dict] = {}
    for row in rows:
        train_id = row["train_id"]
        if train_id not in by_train:
            by_train[train_id] = row
    return [snapshot_from_row(row) for row in by_train.values()]


async def update_consists_from_poll(rows: list[dict], collected_at: str) -> None:
    snapshots = _snapshots_from_rows(rows)
    if not snapshots:
        return

    async with _db_write() as db:
        db.row_factory = aiosqlite.Row
        new_snapshots: list[dict] = []
        existing_snapshots: list[tuple[dict, aiosqlite.Row]] = []

        for snapshot in snapshots:
            train_id = snapshot["train_id"]
            cursor = await db.execute(
                "SELECT * FROM feelings_consist_trains WHERE train_id = ?",
                (train_id,),
            )
            existing = await cursor.fetchone()
            if existing is None:
                new_snapshots.append(snapshot)
            else:
                existing_snapshots.append((snapshot, existing))

        claimed_predecessors = await _predecessors_with_successors(db)
        link_assignments = await _assign_reversal_links(
            db,
            new_snapshots,
            collected_at,
            claimed_predecessors,
        )

        for snapshot in new_snapshots:
            train_id = snapshot["train_id"]
            consist_id, predecessor_id = link_assignments[train_id]
            await db.execute(
                """
                INSERT INTO feelings_consist_trains (
                    train_id, feelings_consist_id, route_id, direction,
                    first_seen_at, last_seen_at, last_end_terminal,
                    predecessor_train_id, link_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    train_id,
                    consist_id,
                    snapshot.get("route_id"),
                    snapshot.get("direction"),
                    collected_at,
                    collected_at,
                    snapshot.get("end_terminal"),
                    predecessor_id,
                    "terminal_reversal" if predecessor_id else None,
                ),
            )
            await db.execute(
                """
                UPDATE feelings_consists
                SET last_seen_at = ?
                WHERE feelings_consist_id = ?
                """,
                (collected_at, consist_id),
            )

        for snapshot, existing in existing_snapshots:
            train_id = snapshot["train_id"]
            end_terminal = snapshot.get("end_terminal") or existing["last_end_terminal"]
            await db.execute(
                """
                UPDATE feelings_consist_trains
                SET route_id = ?, direction = ?, last_seen_at = ?, last_end_terminal = ?
                WHERE train_id = ?
                """,
                (
                    snapshot.get("route_id"),
                    snapshot.get("direction"),
                    collected_at,
                    end_terminal,
                    train_id,
                ),
            )
            await db.execute(
                """
                UPDATE feelings_consists
                SET last_seen_at = ?
                WHERE feelings_consist_id = ?
                """,
                (collected_at, existing["feelings_consist_id"]),
            )

        await db.commit()


async def _create_feelings_consist(
    db: aiosqlite.Connection,
    route_id: str | None,
    collected_at: str,
) -> int:
    cursor = await db.execute(
        """
        INSERT INTO feelings_consists (route_id, started_at, last_seen_at)
        VALUES (?, ?, ?)
        """,
        (route_id, collected_at, collected_at),
    )
    return int(cursor.lastrowid)


async def _predecessors_with_successors(db: aiosqlite.Connection) -> set[str]:
    cursor = await db.execute(
        """
        SELECT DISTINCT predecessor_train_id
        FROM feelings_consist_trains
        WHERE predecessor_train_id IS NOT NULL
        """
    )
    return {row[0] for row in await cursor.fetchall()}


async def _find_reversal_predecessor_candidates(
    db: aiosqlite.Connection,
    *,
    direction: str | None,
    origin_terminal: str | None,
    successor_first_seen_at: str,
    exclude_train_id: str,
) -> list[dict]:
    if not direction or not origin_terminal:
        return []

    cursor = await db.execute(
        """
        SELECT train_id, feelings_consist_id, route_id, direction,
               last_seen_at, last_end_terminal
        FROM feelings_consist_trains
        WHERE direction != ?
          AND last_end_terminal = ?
          AND last_seen_at < ?
          AND train_id != ?
        ORDER BY last_seen_at DESC
        LIMIT 50
        """,
        (direction, origin_terminal, successor_first_seen_at, exclude_train_id),
    )
    return [dict(row) for row in await cursor.fetchall()]


async def _assign_reversal_links(
    db: aiosqlite.Connection,
    new_snapshots: list[dict],
    collected_at: str,
    claimed_predecessors: set[str],
) -> dict[str, tuple[int, str | None]]:
    """Assign at most one successor per predecessor; prefer same route, then shorter gap."""
    candidates: list[tuple[tuple[int, float], str, dict]] = []

    for snapshot in new_snapshots:
        origin_terminal = successor_origin_terminal(snapshot.get("parsed"))
        if not origin_terminal:
            continue

        for predecessor in await _find_reversal_predecessor_candidates(
            db,
            direction=snapshot.get("direction"),
            origin_terminal=origin_terminal,
            successor_first_seen_at=collected_at,
            exclude_train_id=snapshot["train_id"],
        ):
            if not can_link_terminal_reversal(
                predecessor_last_seen_at=predecessor["last_seen_at"],
                successor_first_seen_at=collected_at,
                predecessor_route_id=predecessor.get("route_id"),
                successor_route_id=snapshot.get("route_id"),
                predecessor_direction=predecessor.get("direction"),
                successor_direction=snapshot.get("direction"),
                predecessor_end_terminal=predecessor.get("last_end_terminal"),
                successor_origin_terminal=origin_terminal,
            ):
                continue

            priority = reversal_link_priority(
                predecessor_route_id=predecessor.get("route_id"),
                successor_route_id=snapshot.get("route_id"),
                predecessor_last_seen_at=predecessor["last_seen_at"],
                successor_first_seen_at=collected_at,
            )
            candidates.append((priority, snapshot["train_id"], predecessor))

    candidates.sort(key=lambda item: (item[0], item[1]))

    assignments: dict[str, tuple[int, str | None]] = {}
    assigned_successors: set[str] = set()

    for _priority, successor_id, predecessor in candidates:
        predecessor_id = predecessor["train_id"]
        if successor_id in assigned_successors:
            continue
        if predecessor_id in claimed_predecessors:
            continue

        assignments[successor_id] = (
            int(predecessor["feelings_consist_id"]),
            predecessor_id,
        )
        assigned_successors.add(successor_id)
        claimed_predecessors.add(predecessor_id)

    for snapshot in new_snapshots:
        train_id = snapshot["train_id"]
        if train_id in assignments:
            continue
        consist_id = await _create_feelings_consist(db, snapshot.get("route_id"), collected_at)
        assignments[train_id] = (consist_id, None)

    return assignments


async def update_colocations_from_poll(rows: list[dict], collected_at: str) -> None:
    train_ids = list({row["train_id"] for row in rows})
    consist_by_train = await get_feelings_consist_ids(train_ids) if train_ids else {}

    route_by_train = {row["train_id"]: row.get("route_id") for row in rows}
    by_stop = trains_at_station_by_consist(rows, consist_by_train)
    active_pairs = colocation_pairs(by_stop, route_by_train)
    active_keys = {(consist_a, consist_b, stop_id) for stop_id, consist_a, consist_b, _, _ in active_pairs}

    async with _db_write() as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            "DELETE FROM feelings_consist_reunions WHERE reunion_until <= ?",
            (collected_at,),
        )

        for stop_id, consist_a, consist_b, train_a_id, train_b_id in active_pairs:
            cursor = await db.execute(
                """
                SELECT id
                FROM feelings_consist_colocations
                WHERE consist_a_id = ?
                  AND consist_b_id = ?
                  AND stop_id = ?
                  AND ended_at IS NULL
                LIMIT 1
                """,
                (consist_a, consist_b, stop_id),
            )
            if await cursor.fetchone():
                continue

            cursor = await db.execute(
                """
                SELECT ended_at
                FROM feelings_consist_colocations
                WHERE consist_a_id = ?
                  AND consist_b_id = ?
                  AND stop_id = ?
                  AND ended_at IS NOT NULL
                ORDER BY ended_at DESC
                LIMIT 1
                """,
                (consist_a, consist_b, stop_id),
            )
            last_ended = await cursor.fetchone()
            if last_ended and should_trigger_reunion(last_ended["ended_at"], collected_at):
                await db.execute(
                    """
                    INSERT INTO feelings_consist_reunions (
                        consist_a_id, consist_b_id, stop_id,
                        train_a_id, train_b_id, triggered_at, reunion_until
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        consist_a,
                        consist_b,
                        stop_id,
                        train_a_id,
                        train_b_id,
                        collected_at,
                        reunion_until(collected_at),
                    ),
                )

            await db.execute(
                """
                INSERT INTO feelings_consist_colocations (
                    consist_a_id, consist_b_id, stop_id, started_at
                ) VALUES (?, ?, ?, ?)
                """,
                (consist_a, consist_b, stop_id, collected_at),
            )

        cursor = await db.execute(
            """
            SELECT id, consist_a_id, consist_b_id, stop_id
            FROM feelings_consist_colocations
            WHERE ended_at IS NULL
            """
        )
        for row in await cursor.fetchall():
            key = (row["consist_a_id"], row["consist_b_id"], row["stop_id"])
            if key not in active_keys:
                await db.execute(
                    """
                    UPDATE feelings_consist_colocations
                    SET ended_at = ?
                    WHERE id = ?
                    """,
                    (collected_at, row["id"]),
                )

        await db.commit()


async def get_active_old_friends(train_ids: list[str]) -> dict[str, dict[str, str]]:
    if not train_ids:
        return {}

    consist_by_train = await get_feelings_consist_ids(train_ids)
    if not consist_by_train:
        return {}

    now = _iso(_utc_now())
    async with _db_connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT consist_a_id, consist_b_id, train_a_id, train_b_id
            FROM feelings_consist_reunions
            WHERE reunion_until > ?
            """,
            (now,),
        )
        reunions = [dict(row) for row in await cursor.fetchall()]

    result: dict[str, dict[str, str]] = {}
    for train_id in train_ids:
        consist_id = consist_by_train.get(train_id)
        if consist_id is None:
            continue

        for reunion in reunions:
            if consist_id == reunion["consist_a_id"]:
                friend_train_id = reunion["train_b_id"]
            elif consist_id == reunion["consist_b_id"]:
                friend_train_id = reunion["train_a_id"]
            else:
                continue

            result[train_id] = {"old_friend_train_id": friend_train_id}
            break

    return result


async def get_feelings_consist_ids(train_ids: list[str]) -> dict[str, int]:
    if not train_ids:
        return {}

    placeholders = ",".join("?" * len(train_ids))
    async with _db_connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""
            SELECT train_id, feelings_consist_id
            FROM feelings_consist_trains
            WHERE train_id IN ({placeholders})
            """,
            train_ids,
        )
        return {row["train_id"]: int(row["feelings_consist_id"]) for row in await cursor.fetchall()}


async def get_train_ids_for_consists(consist_ids: list[int]) -> dict[int, list[str]]:
    if not consist_ids:
        return {}

    placeholders = ",".join("?" * len(consist_ids))
    async with _db_connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""
            SELECT feelings_consist_id, train_id
            FROM feelings_consist_trains
            WHERE feelings_consist_id IN ({placeholders})
            ORDER BY first_seen_at
            """,
            consist_ids,
        )
        grouped: dict[int, list[str]] = defaultdict(list)
        for row in await cursor.fetchall():
            grouped[int(row["feelings_consist_id"])].append(row["train_id"])
        return grouped


async def update_feed_health(
    feed_id: str,
    *,
    feed_timestamp: datetime | None,
    status: str,
    error: str | None = None,
) -> None:
    now = _iso(_utc_now())
    feed_ts = _iso(feed_timestamp) if feed_timestamp else None

    async with _db_write() as db:
        await db.execute(
            """
            INSERT INTO feed_health (
                feed_id, last_poll_success_at, last_feed_timestamp, status, last_error
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(feed_id) DO UPDATE SET
                last_poll_success_at = excluded.last_poll_success_at,
                last_feed_timestamp = excluded.last_feed_timestamp,
                status = excluded.status,
                last_error = excluded.last_error
            """,
            (feed_id, now, feed_ts, status, error),
        )
        await db.commit()


async def prune_stale_train_states() -> int:
    cutoff = _iso(_utc_now() - timedelta(hours=DATA_RETENTION_HOURS))
    async with _db_write() as db:
        cursor = await db.execute(
            "DELETE FROM train_state WHERE collected_at < ?",
            (cutoff,),
        )
        deleted = cursor.rowcount
        await db.commit()
    return deleted


async def reclaim_database_space() -> None:
    async with _db_write() as db:
        await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        await db.commit()


async def vacuum_database() -> None:
    async with _db_write() as db:
        await db.execute("VACUUM")
        await db.commit()


async def get_trains_with_arrivals(window_minutes: int | None = None) -> list[dict]:
    window = window_minutes or ARRIVAL_WINDOW_MINUTES
    cutoff = _iso(_utc_now() - timedelta(minutes=window))

    async with _db_connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT
                train_id, trip_id, route_id, direction, shape_id,
                location_stop_id, location_status, current_stop_sequence,
                trip_arrival_delay, trip_departure_delay,
                last_position_update, next_stop_arrival_time, next_stop_departure_time,
                feed_id, feed_timestamp, collected_at,
                last_stopped_at, departed_from_stop_id, dwell_since, groggy_until,
                day_on_time_rate, day_on_time_samples, day_tracking_minutes,
                consistent_day, upcoming_stops_json
            FROM train_state
            WHERE collected_at >= ?
              AND location_stop_id IS NOT NULL
              AND location_stop_id != ''
            ORDER BY route_id, train_id
            """,
            (cutoff,),
        )
        result = []
        for row in await cursor.fetchall():
            train = dict(row)
            try:
                upcoming = json.loads(train.pop("upcoming_stops_json") or "[]")
            except json.JSONDecodeError:
                upcoming = []
            train["consistent_day"] = bool(train.get("consistent_day"))
            train["upcoming_stops"] = upcoming[:HISTORY_LIMIT]
            train["observation_count"] = len(train["upcoming_stops"])
            # Keep legacy key used by older clients / remote proxy shape.
            train["arrivals"] = [
                {
                    **stop,
                    "location_stop_id": train["location_stop_id"],
                    "location_status": train["location_status"],
                    "collected_at": train["collected_at"],
                }
                for stop in train["upcoming_stops"]
            ]
            result.append(train)
        return result


async def get_train_locations(window_minutes: int | None = None) -> list[dict]:
    window = window_minutes or ARRIVAL_WINDOW_MINUTES
    cutoff = _iso(_utc_now() - timedelta(minutes=window))

    async with _db_connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT
                train_id, route_id, trip_id, location_stop_id, location_status,
                trip_arrival_delay, trip_departure_delay, last_position_update,
                next_stop_arrival_time, next_stop_departure_time,
                shape_id, direction, current_stop_sequence, collected_at,
                departed_from_stop_id, dwell_since, groggy_until,
                day_on_time_rate, day_on_time_samples, day_tracking_minutes,
                consistent_day
            FROM train_state
            WHERE collected_at >= ?
              AND location_stop_id IS NOT NULL
              AND location_stop_id != ''
            ORDER BY train_id
            """,
            (cutoff,),
        )
        return [dict(row) for row in await cursor.fetchall()]


async def get_cached_train_locations() -> list[dict]:
    global _locations_cache, _locations_cache_at

    now = time.monotonic()
    if (
        _locations_cache is not None
        and _locations_cache_at is not None
        and now - _locations_cache_at < LOCATIONS_CACHE_SECONDS
    ):
        return _locations_cache

    async with _locations_refresh_lock:
        now = time.monotonic()
        if (
            _locations_cache is not None
            and _locations_cache_at is not None
            and now - _locations_cache_at < LOCATIONS_CACHE_SECONDS
        ):
            return _locations_cache

        _locations_cache = await get_train_locations()
        _locations_cache_at = now
        return _locations_cache


async def get_map_enrichment(
    train_ids: list[str],
) -> tuple[dict[str, int], dict[str, dict[str, str]]]:
    global _map_enrichment_cache, _map_enrichment_cache_at

    now = time.monotonic()
    if (
        _map_enrichment_cache is not None
        and _map_enrichment_cache_at is not None
        and now - _map_enrichment_cache_at < MAP_ENRICHMENT_CACHE_SECONDS
    ):
        cached = _map_enrichment_cache
        return cached["consist_ids"], cached["old_friends"]

    async with _map_enrichment_refresh_lock:
        now = time.monotonic()
        if (
            _map_enrichment_cache is not None
            and _map_enrichment_cache_at is not None
            and now - _map_enrichment_cache_at < MAP_ENRICHMENT_CACHE_SECONDS
        ):
            cached = _map_enrichment_cache
            return cached["consist_ids"], cached["old_friends"]

        consist_ids = await get_feelings_consist_ids(train_ids)
        old_friends = await get_active_old_friends(train_ids)
        _map_enrichment_cache = {
            "consist_ids": consist_ids,
            "old_friends": old_friends,
        }
        _map_enrichment_cache_at = now
        return consist_ids, old_friends


def punctuality_from_train_state(train: dict) -> dict:
    return {
        "consistent_day": bool(train.get("consistent_day")),
        "day_on_time_rate": train.get("day_on_time_rate"),
        "day_on_time_samples": int(train.get("day_on_time_samples") or 0),
        "day_tracking_minutes": float(train.get("day_tracking_minutes") or 0.0),
    }


async def get_train_day_punctuality(train_ids: list[str]) -> dict[str, dict]:
    if not train_ids:
        return {}

    placeholders = ",".join("?" * len(train_ids))
    async with _db_connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""
            SELECT
                train_id, day_on_time_rate, day_on_time_samples,
                day_tracking_minutes, consistent_day
            FROM train_state
            WHERE train_id IN ({placeholders})
            """,
            train_ids,
        )
        rows = {
            row["train_id"]: punctuality_from_train_state(dict(row))
            for row in await cursor.fetchall()
        }

    return {
        train_id: rows.get(train_id, empty_day_punctuality())
        for train_id in train_ids
    }


async def get_feed_health() -> list[dict]:
    async with _db_connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT feed_id, last_poll_success_at, last_feed_timestamp, status, last_error FROM feed_health"
        )
        return [dict(row) for row in await cursor.fetchall()]
