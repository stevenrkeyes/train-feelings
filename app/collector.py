from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from nyct_gtfs import NYCTFeed

from app.config import FEED_STALE_THRESHOLD_SECONDS, POLL_INTERVAL_SECONDS
from app.db import prune_expired_sessions, prune_old_data, record_observations, update_feed_health
from app.feeds import FEEDS

logger = logging.getLogger(__name__)
NY_TZ = ZoneInfo("America/New_York")


def _train_key(trip) -> str:
    if trip.nyc_train_id:
        return trip.nyc_train_id.strip()
    return f"trip:{trip.trip_id}"


def _normalize_feed_timestamp(feed_timestamp: datetime | None) -> datetime | None:
    if feed_timestamp is None:
        return None
    if feed_timestamp.tzinfo is None:
        feed_timestamp = feed_timestamp.replace(tzinfo=NY_TZ)
    return feed_timestamp.astimezone(timezone.utc)


def _iso_or_none(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=NY_TZ)
    return dt.astimezone(timezone.utc).isoformat()


def _delays_from_stop_update(stop_update) -> tuple[int | None, int | None]:
    raw = stop_update._stop_time_update
    arrival_delay = None
    departure_delay = None
    if raw.HasField("arrival") and raw.arrival.HasField("delay"):
        arrival_delay = int(raw.arrival.delay)
    if raw.HasField("departure") and raw.departure.HasField("delay"):
        departure_delay = int(raw.departure.delay)
    return arrival_delay, departure_delay


def _extract_rows(feed_id: str, feed: NYCTFeed) -> list[dict]:
    rows: list[dict] = []
    for trip in feed.trips:
        train_id = _train_key(trip)
        location_stop_id = trip.location
        location_status = trip.location_status
        stop_updates = trip.stop_time_updates
        trip_arrival_delay = None
        trip_departure_delay = None
        if stop_updates:
            trip_arrival_delay, trip_departure_delay = _delays_from_stop_update(stop_updates[0])

        last_position_update = _iso_or_none(trip.last_position_update) if trip.underway else None
        next_stop_arrival_time = _iso_or_none(stop_updates[0].arrival) if stop_updates else None
        next_stop_departure_time = _iso_or_none(stop_updates[0].departure) if stop_updates else None
        current_stop_sequence = trip.current_stop_sequence_index if trip.underway else None
        shape_id = trip.shape_id
        direction = trip.direction

        for stop_update in stop_updates:
            arrival_delay, departure_delay = _delays_from_stop_update(stop_update)
            rows.append(
                {
                    "train_id": train_id,
                    "trip_id": trip.trip_id,
                    "route_id": trip.route_id,
                    "stop_id": stop_update.stop_id,
                    "stop_name": stop_update.stop_name,
                    "arrival_time": _iso_or_none(stop_update.arrival),
                    "departure_time": _iso_or_none(stop_update.departure),
                    "arrival_delay": arrival_delay,
                    "departure_delay": departure_delay,
                    "trip_arrival_delay": trip_arrival_delay,
                    "trip_departure_delay": trip_departure_delay,
                    "last_position_update": last_position_update,
                    "next_stop_arrival_time": next_stop_arrival_time,
                    "next_stop_departure_time": next_stop_departure_time,
                    "location_stop_id": location_stop_id,
                    "location_status": location_status,
                    "shape_id": shape_id,
                    "direction": direction,
                    "current_stop_sequence": current_stop_sequence,
                    "scheduled_track": stop_update.scheduled_track,
                    "actual_track": stop_update.actual_track,
                }
            )
    return rows


def _poll_feed_sync(feed_id: str, url: str) -> tuple[str, list[dict], datetime | None, str | None]:
    try:
        feed = NYCTFeed(url)
        feed_timestamp = _normalize_feed_timestamp(feed.last_generated)

        now = datetime.now(timezone.utc)
        if feed_timestamp:
            staleness = (now - feed_timestamp).total_seconds()
            status = "healthy" if staleness <= FEED_STALE_THRESHOLD_SECONDS else "unhealthy"
            if status == "unhealthy":
                error = f"Feed stale by {int(staleness)}s"
            else:
                error = None
        else:
            status = "unhealthy"
            error = "Missing feed timestamp"

        rows = _extract_rows(feed_id, feed)
        return status, rows, feed_timestamp, error
    except Exception as exc:
        logger.exception("Failed to poll feed %s", feed_id)
        return "unhealthy", [], None, str(exc)


async def poll_feed(feed_id: str, url: str) -> None:
    loop = asyncio.get_running_loop()
    status, rows, feed_timestamp, error = await loop.run_in_executor(
        None, _poll_feed_sync, feed_id, url
    )

    if feed_timestamp or status == "healthy":
        await update_feed_health(
            feed_id,
            feed_timestamp=feed_timestamp,
            status=status,
            error=error,
        )
    else:
        await update_feed_health(feed_id, feed_timestamp=None, status="unhealthy", error=error)

    if rows and feed_timestamp and status == "healthy":
        await record_observations(feed_id, feed_timestamp, rows)
        logger.info("Recorded %d observations from feed %s", len(rows), feed_id)


async def poll_all_feeds() -> None:
    await asyncio.gather(*(poll_feed(feed_id, url) for feed_id, url in FEEDS.items()))


async def collector_loop(stop_event: asyncio.Event) -> None:
    logger.info("Collector started (interval=%ss)", POLL_INTERVAL_SECONDS)
    while not stop_event.is_set():
        try:
            await poll_all_feeds()
            await prune_old_data()
            await prune_expired_sessions()
        except Exception:
            logger.exception("Collector loop error")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=POLL_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            continue

    logger.info("Collector stopped")
