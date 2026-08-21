# Train Feelings

Even a subway train has feelings.

## What it does

- Polls all 8 MTA subway GTFS-RT feeds every 30 seconds
- Upserts one current row per train (`train_state`) with map-ready derived fields
- Serves a same-origin web UI (session cookie required for API access)
- Lists currently tracked trains with their upcoming stop predictions

## Local development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
./run-dev.sh
```

Open http://localhost:8000/ for the map, or http://localhost:8000/trains for the train list.

The first page load sets an HttpOnly session cookie. API routes reject requests without it.

### Modes

**Full local stack** (default):

```bash
DATA_SOURCE=local
COLLECTOR_ENABLED=true
```

**Local UI, production data** (proxy through your local server):

```bash
DATA_SOURCE=remote
COLLECTOR_ENABLED=false
REMOTE_API_URL=https://your-production-host
INTERNAL_API_TOKEN=your-shared-secret
```

Set the same `INTERNAL_API_TOKEN` on production so your local server can proxy API calls.

## Production (Fly.io)

```bash
fly apps create train-feelings   # once
fly volumes create train_feelings_data --region ewr --size 1
fly secrets set SESSION_SECRET=... INTERNAL_API_TOKEN=...
fly deploy
```

Point `trains.oscilloscopi.st` (or your subdomain) at the Fly app via DNS.

## API (not public)

| Endpoint | Description |
|----------|-------------|
| `GET /` | Map (sets session cookie) |
| `GET /trains` | Train list UI |
| `GET /api/map/trains` | Current train locations for map |
| `GET /api/trains` | Current train state |
| `GET /api/feeds` | Feed health status |
| `GET /api/health` | Liveness check (no session) |

## Mobile testing

Use browser DevTools device mode (`Ctrl+Shift+M`) or open `http://<your-lan-ip>:8000/trains` on your phone.

## Subway line overlay

The map draws route shapes from MTA GTFS `shapes.txt` with official `route_color` values from `routes.txt`.

To regenerate after updating GTFS:

```bash
curl -fsSL -o data/gtfs/google_transit.zip \
  http://web.mta.info/developers/data/nyct/subway/google_transit.zip
python scripts/build_shapes_geojson.py
```
