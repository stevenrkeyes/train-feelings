from __future__ import annotations

import secrets
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from app.config import (
    ARRIVAL_WINDOW_MINUTES,
    DATABASE_PATH,
    DATA_RETENTION_HOURS,
    EVENT_FUTURE_MARGIN_SECONDS,
    HISTORY_LIMIT,
    SESSION_MAX_AGE_SECONDS,
)
from app.punctuality import summarize_day_punctuality, today_bounds_utc
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


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _event_time(row: dict) -> datetime | None:
    """When a stop visit completes; falls back to arrival for destination-only updates."""
    return _parse_iso(row.get("departure_time") or row.get("arrival_time"))


def _is_past_or_current(row: dict, now: datetime, margin: timedelta) -> bool:
    event_time = _event_time(row)
    if event_time is None:
        return False
    if event_time.tzinfo is None:
        event_time = event_time.replace(tzinfo=timezone.utc)
    return event_time <= now + margin


def _filter_recent_events(rows: list[dict], limit: int | None = None) -> list[dict]:
    now = _utc_now()
    margin = timedelta(seconds=EVENT_FUTURE_MARGIN_SECONDS)
    cap = limit or HISTORY_LIMIT

    past_or_current = [row for row in rows if _is_past_or_current(row, now, margin)]
    past_or_current.sort(
        key=lambda row: (
            _parse_iso(row.get("collected_at")) or datetime.min.replace(tzinfo=timezone.utc),
            _event_time(row) or datetime.min.replace(tzinfo=timezone.utc),
        ),
        reverse=True,
    )
    return past_or_current[:cap]


async def init_db(db_path: Path | None = None) -> None:
    path = db_path or DATABASE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(path) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                train_id TEXT NOT NULL,
                trip_id TEXT,
                route_id TEXT,
                stop_id TEXT NOT NULL,
                stop_name TEXT,
                arrival_time TEXT,
                departure_time TEXT,
                location_stop_id TEXT,
                location_status TEXT,
                scheduled_track TEXT,
                actual_track TEXT,
                arrival_delay INTEGER,
                departure_delay INTEGER,
                trip_arrival_delay INTEGER,
                trip_departure_delay INTEGER,
                last_position_update TEXT,
                next_stop_arrival_time TEXT,
                next_stop_departure_time TEXT,
                shape_id TEXT,
                direction TEXT,
                current_stop_sequence INTEGER,
                feed_id TEXT NOT NULL,
                feed_timestamp TEXT NOT NULL,
                collected_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_obs_train_collected
                ON observations(train_id, collected_at);
            CREATE INDEX IF NOT EXISTS idx_obs_collected
                ON observations(collected_at);

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
        await _migrate_observations_table(db)
        await _init_consist_tables(db)
        await _init_old_friend_tables(db)


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


async def _migrate_observations_table(db: aiosqlite.Connection) -> None:
    cursor = await db.execute("PRAGMA table_info(observations)")
    columns = {row[1] for row in await cursor.fetchall()}
    additions = {
        "location_stop_id": "TEXT",
        "location_status": "TEXT",
        "scheduled_track": "TEXT",
        "actual_track": "TEXT",
        "arrival_delay": "INTEGER",
        "departure_delay": "INTEGER",
        "trip_arrival_delay": "INTEGER",
        "trip_departure_delay": "INTEGER",
        "last_position_update": "TEXT",
        "next_stop_arrival_time": "TEXT",
        "next_stop_departure_time": "TEXT",
        "shape_id": "TEXT",
        "direction": "TEXT",
        "current_stop_sequence": "INTEGER",
    }
    for name, col_type in additions.items():
        if name not in columns:
            await db.execute(f"ALTER TABLE observations ADD COLUMN {name} {col_type}")
    await db.commit()


async def create_session() -> str:
    token = secrets.token_urlsafe(32)
    now = _utc_now()
    expires = now + timedelta(seconds=SESSION_MAX_AGE_SECONDS)

    async with aiosqlite.connect(DATABASE_PATH) as db:
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
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "SELECT 1 FROM sessions WHERE token = ? AND expires_at > ?",
            (token, now),
        )
        row = await cursor.fetchone()
    return row is not None


async def prune_expired_sessions() -> None:
    now = _iso(_utc_now())
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
        await db.commit()


async def record_observations(
    feed_id: str,
    feed_timestamp: datetime,
    rows: list[dict],
) -> None:
    if not rows:
        return

    collected_at = _iso(_utc_now())
    feed_ts = _iso(feed_timestamp)
    values = [
        (
            row["train_id"],
            row.get("trip_id"),
            row.get("route_id"),
            row["stop_id"],
            row.get("stop_name"),
            row.get("arrival_time"),
            row.get("departure_time"),
            row.get("location_stop_id"),
            row.get("location_status"),
            row.get("scheduled_track"),
            row.get("actual_track"),
            row.get("arrival_delay"),
            row.get("departure_delay"),
            row.get("trip_arrival_delay"),
            row.get("trip_departure_delay"),
            row.get("last_position_update"),
            row.get("next_stop_arrival_time"),
            row.get("next_stop_departure_time"),
            row.get("shape_id"),
            row.get("direction"),
            row.get("current_stop_sequence"),
            feed_id,
            feed_ts,
            collected_at,
        )
        for row in rows
    ]

    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.executemany(
            """
            INSERT INTO observations (
                train_id, trip_id, route_id, stop_id, stop_name,
                arrival_time, departure_time, location_stop_id, location_status,
                scheduled_track, actual_track, arrival_delay, departure_delay,
                trip_arrival_delay, trip_departure_delay, last_position_update,
                next_stop_arrival_time, next_stop_departure_time,
                shape_id, direction, current_stop_sequence,
                feed_id, feed_timestamp, collected_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        await db.commit()

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

    async with aiosqlite.connect(DATABASE_PATH) as db:
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

    by_stop = trains_at_station_by_consist(rows, consist_by_train)
    active_pairs = colocation_pairs(by_stop)
    active_keys = {(consist_a, consist_b, stop_id) for stop_id, consist_a, consist_b, _, _ in active_pairs}

    async with aiosqlite.connect(DATABASE_PATH) as db:
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
    async with aiosqlite.connect(DATABASE_PATH) as db:
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
    async with aiosqlite.connect(DATABASE_PATH) as db:
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
    async with aiosqlite.connect(DATABASE_PATH) as db:
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

    async with aiosqlite.connect(DATABASE_PATH) as db:
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


async def prune_old_data() -> None:
    cutoff = _iso(_utc_now() - timedelta(hours=DATA_RETENTION_HOURS))
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("DELETE FROM observations WHERE collected_at < ?", (cutoff,))
        await db.commit()


async def get_trains_with_arrivals(window_minutes: int | None = None) -> list[dict]:
    window = window_minutes or ARRIVAL_WINDOW_MINUTES
    cutoff = _iso(_utc_now() - timedelta(minutes=window))

    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT
                train_id,
                MAX(route_id) AS route_id,
                MAX(trip_id) AS trip_id,
                COUNT(*) AS observation_count
            FROM observations
            WHERE collected_at >= ?
            GROUP BY train_id
            ORDER BY train_id
            """,
            (cutoff,),
        )
        trains = [dict(row) for row in await cursor.fetchall()]

        result = []
        for train in trains:
            cursor = await db.execute(
                """
                SELECT
                    stop_id, stop_name, arrival_time, departure_time,
                    location_stop_id, location_status,
                    scheduled_track, actual_track, collected_at
                FROM observations
                WHERE train_id = ? AND collected_at >= ?
                ORDER BY collected_at DESC, arrival_time DESC
                LIMIT 300
                """,
                (train["train_id"], cutoff),
            )
            arrivals = _filter_recent_events([dict(row) for row in await cursor.fetchall()])
            if arrivals:
                result.append({**train, "arrivals": arrivals})

    return result


async def get_departed_from_stops(
    trains: list[dict],
    window_minutes: int | None = None,
) -> dict[str, str]:
    if not trains:
        return {}

    window = window_minutes or ARRIVAL_WINDOW_MINUTES
    cutoff = _iso(_utc_now() - timedelta(minutes=window))
    departed: dict[str, str] = {}

    async with aiosqlite.connect(DATABASE_PATH) as db:
        for train in trains:
            if train.get("location_status") != "IN_TRANSIT_TO":
                continue

            train_id = train["train_id"]
            current_stop = train.get("location_stop_id")
            if not current_stop:
                continue

            cursor = await db.execute(
                """
                SELECT location_stop_id
                FROM observations
                WHERE train_id = ?
                  AND collected_at >= ?
                  AND location_stop_id IS NOT NULL
                  AND location_stop_id != ?
                  AND location_status = 'STOPPED_AT'
                ORDER BY collected_at DESC
                LIMIT 1
                """,
                (train_id, cutoff, current_stop),
            )
            row = await cursor.fetchone()
            if row:
                departed[train_id] = row[0]

    return departed


async def get_train_locations(window_minutes: int | None = None) -> list[dict]:
    window = window_minutes or ARRIVAL_WINDOW_MINUTES
    cutoff = _iso(_utc_now() - timedelta(minutes=window))

    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT
                o.train_id,
                o.route_id,
                o.trip_id,
                o.location_stop_id,
                o.location_status,
                o.trip_arrival_delay,
                o.trip_departure_delay,
                o.last_position_update,
                o.next_stop_arrival_time,
                o.next_stop_departure_time,
                o.shape_id,
                o.direction,
                o.current_stop_sequence,
                o.collected_at
            FROM observations o
            INNER JOIN (
                SELECT train_id, MAX(id) AS max_id
                FROM observations
                WHERE collected_at >= ?
                  AND location_stop_id IS NOT NULL
                  AND location_stop_id != ''
                GROUP BY train_id
            ) latest ON o.id = latest.max_id
            ORDER BY o.train_id
            """,
            (cutoff,),
        )
        return [dict(row) for row in await cursor.fetchall()]


async def get_train_day_punctuality(train_ids: list[str]) -> dict[str, dict]:
    if not train_ids:
        return {}

    consist_by_train = await get_feelings_consist_ids(train_ids)
    unique_consist_ids = list(dict.fromkeys(consist_by_train.values()))
    members_by_consist = await get_train_ids_for_consists(unique_consist_ids)

    all_train_ids = list(
        dict.fromkeys(
            train_id
            for member_ids in members_by_consist.values()
            for train_id in member_ids
        )
    )
    if not all_train_ids:
        all_train_ids = train_ids

    day_start, day_end = today_bounds_utc()
    placeholders = ",".join("?" * len(all_train_ids))
    params = [*all_train_ids, _iso(day_start), _iso(day_end)]

    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""
            SELECT
                train_id,
                collected_at,
                MAX(trip_arrival_delay) AS trip_arrival_delay,
                MAX(trip_departure_delay) AS trip_departure_delay
            FROM observations
            WHERE train_id IN ({placeholders})
              AND collected_at >= ?
              AND collected_at < ?
            GROUP BY train_id, collected_at
            ORDER BY train_id, collected_at
            """,
            params,
        )
        rows = [dict(row) for row in await cursor.fetchall()]

    samples_by_train: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        samples_by_train[row["train_id"]].append(row)

    stats_by_consist: dict[int, dict] = {}
    for consist_id, member_ids in members_by_consist.items():
        merged: list[dict] = []
        for member_id in member_ids:
            merged.extend(samples_by_train.get(member_id, []))
        merged.sort(key=lambda sample: sample.get("collected_at") or "")
        stats_by_consist[consist_id] = summarize_day_punctuality(merged)

    result: dict[str, dict] = {}
    for train_id in train_ids:
        consist_id = consist_by_train.get(train_id)
        if consist_id and consist_id in stats_by_consist:
            result[train_id] = stats_by_consist[consist_id]
        else:
            result[train_id] = summarize_day_punctuality(samples_by_train.get(train_id, []))
    return result


async def get_feed_health() -> list[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT feed_id, last_poll_success_at, last_feed_timestamp, status, last_error FROM feed_health"
        )
        return [dict(row) for row in await cursor.fetchall()]
