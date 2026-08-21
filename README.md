# Train Feelings

Even a subway train has feelings.

## What it does

- Polls all 8 MTA subway GTFS-RT feeds every 30 seconds
- Logs the train state
- Shows how they feel

## Local development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
./run-dev.sh
```

Open http://localhost:8000/

### Modes

**Full local stack** (default):

```bash
DATA_SOURCE=local
COLLECTOR_ENABLED=true
```

**Local UI, production data**:

```bash
DATA_SOURCE=remote
COLLECTOR_ENABLED=false
REMOTE_API_URL=<production url>
INTERNAL_API_TOKEN=<shared secret>
```

## Production

```bash
fly apps create train-feelings   # once
fly volumes create train_feelings_data --region ewr --size 1
fly secrets set INTERNAL_API_TOKEN=...
fly deploy
```

## API (not public)

| Endpoint | Description |
|----------|-------------|
| `GET /` | Map (sets session cookie) |
| `GET /trains` | Train list UI |
| `GET /api/map/trains` | Current train locations for map |
| `GET /api/trains` | Current train state |
| `GET /api/feeds` | Feed health status |
| `GET /api/health` | Liveness check (no session) |
