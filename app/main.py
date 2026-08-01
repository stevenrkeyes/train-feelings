from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.collector import collector_loop
from app.config import (
    ARRIVAL_WINDOW_MINUTES,
    COLLECTOR_ENABLED,
    DATA_SOURCE,
    INTERNAL_API_TOKEN,
    REMOTE_API_URL,
    SESSION_COOKIE_NAME,
    STATIC_DIR,
)
from app import db

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


@app.middleware("http")
async def session_middleware(request: Request, call_next):
    response = await call_next(request)

    if request.url.path == TRAINS_PAGE_PATH and request.method == "GET":
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
async def root():
    return RedirectResponse(url=TRAINS_PAGE_PATH, status_code=302)


@app.get(TRAINS_PAGE_PATH)
async def trains_page():
    page_path = STATIC_DIR / "trains.html"
    if not page_path.exists():
        return JSONResponse({"message": "Frontend not found"}, status_code=404)
    return FileResponse(page_path)
