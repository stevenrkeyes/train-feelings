from __future__ import annotations

import secrets
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


async def _migrate_observations_table(db: aiosqlite.Connection) -> None:
    cursor = await db.execute("PRAGMA table_info(observations)")
    columns = {row[1] for row in await cursor.fetchall()}
    additions = {
        "location_stop_id": "TEXT",
        "location_status": "TEXT",
        "scheduled_track": "TEXT",
        "actual_track": "TEXT",
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
                scheduled_track, actual_track, feed_id, feed_timestamp, collected_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        await db.commit()


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


async def get_feed_health() -> list[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT feed_id, last_poll_success_at, last_feed_timestamp, status, last_error FROM feed_health"
        )
        return [dict(row) for row in await cursor.fetchall()]
