from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.collector import collector_loop
from app.config import (
    ARRIVAL_WINDOW_MINUTES,
    CARTO_API_KEY,
    COLLECTOR_ENABLED,
    DATA_SOURCE,
    INTERNAL_API_TOKEN,
    REMOTE_API_URL,
    SESSION_COOKIE_NAME,
    STATIC_DIR,
)
from app import db
from app.interpolate import enrich_map_trains

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


def _should_run_collector() -> bool:
    return DATA_SOURCE == "local" and COLLECTOR_ENABLED


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    stop_event = asyncio.Event()
    collector_task = None

    if _should_run_collector():
        collector_task = asyncio.create_task(collector_loop(stop_event))
        logger.info("Local collector enabled")
    elif DATA_SOURCE == "remote":
        logger.info("Remote data source: %s", REMOTE_API_URL or "(not set)")
    else:
        logger.info("Collector disabled")

    yield

    if collector_task:
        stop_event.set()
        await collector_task


app = FastAPI(title="Train Feelings", lifespan=lifespan)


def _is_authorized(request: Request) -> bool:
    internal = request.headers.get("X-Internal-Token")
    if INTERNAL_API_TOKEN and internal == INTERNAL_API_TOKEN:
        return True
    return False


async def _require_session(request: Request) -> None:
    if _is_authorized(request):
        return
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not await db.validate_session(token):
        raise HTTPException(status_code=401, detail="Session required")


async def _fetch_remote(path: str) -> dict | list:
    if not REMOTE_API_URL:
        raise HTTPException(status_code=503, detail="REMOTE_API_URL is not configured")

    headers = {}
    if INTERNAL_API_TOKEN:
        headers["X-Internal-Token"] = INTERNAL_API_TOKEN

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(f"{REMOTE_API_URL}{path}", headers=headers)

    if response.status_code == 401:
        raise HTTPException(status_code=503, detail="Remote API rejected proxy request")
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Remote API error")

    return response.json()


TRAINS_PAGE_PATH = "/trains"
MAP_PAGE_PATH = "/"
SESSION_PAGES = {MAP_PAGE_PATH, TRAINS_PAGE_PATH}


@app.middleware("http")
async def session_middleware(request: Request, call_next):
    response = await call_next(request)

    if request.url.path in SESSION_PAGES and request.method == "GET":
        token = request.cookies.get(SESSION_COOKIE_NAME)
        if not await db.validate_session(token):
            new_token = await db.create_session()
            response.set_cookie(
                SESSION_COOKIE_NAME,
                new_token,
                httponly=True,
                samesite="lax",
                secure=request.url.scheme == "https",
                max_age=60 * 60 * 24 * 7,
            )

    return response


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "data_source": DATA_SOURCE,
        "collector_enabled": _should_run_collector(),
    }


@app.get("/api/trains")
async def list_trains(request: Request):
    await _require_session(request)

    if DATA_SOURCE == "remote":
        return await _fetch_remote("/api/trains")

    trains = await db.get_trains_with_arrivals()
    return {
        "window_minutes": ARRIVAL_WINDOW_MINUTES,
        "trains": trains,
    }


@app.get("/api/map/trains")
async def map_trains(request: Request):
    await _require_session(request)

    if DATA_SOURCE == "remote":
        return await _fetch_remote("/api/map/trains")

    started = time.monotonic()
    locations = await db.get_cached_train_locations()
    t_locations = time.monotonic()

    departed_from = {
        train["train_id"]: train["departed_from_stop_id"]
        for train in locations
        if train.get("departed_from_stop_id")
    }
    t_departed = time.monotonic()

    train_ids = [train["train_id"] for train in locations]
    day_punctuality = {
        train["train_id"]: db.punctuality_from_train_state(train)
        for train in locations
    }
    t_punctuality = time.monotonic()

    consist_ids, old_friends = await db.get_map_enrichment(train_ids)
    dwell_since = {
        train["train_id"]: train["dwell_since"]
        for train in locations
        if train.get("dwell_since")
    }
    groggy_until = {
        train["train_id"]: train["groggy_until"]
        for train in locations
        if train.get("groggy_until")
    }
    t_enrichment = time.monotonic()

    trains = enrich_map_trains(
        locations,
        departed_from,
        day_punctuality,
        consist_ids,
        old_friends,
        dwell_since,
        groggy_until,
    )
    elapsed = time.monotonic() - started
    if elapsed > 5:
        logger.warning(
            "map/trains slow (%.1fs, %d trains): locations=%.1fs departed=%.1fs "
            "punctuality=%.1fs enrichment=%.1fs enrich=%.1fs",
            elapsed,
            len(trains),
            t_locations - started,
            t_departed - t_locations,
            t_punctuality - t_departed,
            t_enrichment - t_punctuality,
            time.monotonic() - t_enrichment,
        )

    return {"trains": trains}


@app.get("/api/feeds")
async def feed_status(request: Request):
    await _require_session(request)

    if DATA_SOURCE == "remote":
        return await _fetch_remote("/api/feeds")

    return {"feeds": await db.get_feed_health()}


static_path = STATIC_DIR
if static_path.exists():
    app.mount("/static", StaticFiles(directory=static_path), name="static")


@app.get("/")
async def map_page():
    page_path = STATIC_DIR / "index.html"
    if not page_path.exists():
        return JSONResponse({"message": "Map not found"}, status_code=404)

    html = page_path.read_text(encoding="utf-8")
    config_script = (
        f"<script>window.CARTO_API_KEY = {json.dumps(CARTO_API_KEY)};</script>\n  "
    )
    html = html.replace('<script src="/static/map.js"></script>', f"{config_script}<script src=\"/static/map.js\"></script>")
    return HTMLResponse(html)


@app.get(TRAINS_PAGE_PATH)
async def trains_page():
    page_path = STATIC_DIR / "trains.html"
    if not page_path.exists():
        return JSONResponse({"message": "Frontend not found"}, status_code=404)
    return FileResponse(page_path)
