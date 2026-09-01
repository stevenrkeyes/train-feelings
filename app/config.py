import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data")))

DATA_SOURCE = os.environ.get("DATA_SOURCE", "local")  # local | remote
COLLECTOR_ENABLED = os.environ.get("COLLECTOR_ENABLED", "true").lower() == "true"
REMOTE_API_URL = os.environ.get("REMOTE_API_URL", "").rstrip("/")
INTERNAL_API_TOKEN = os.environ.get("INTERNAL_API_TOKEN", "")

DATABASE_PATH = Path(os.environ.get("DATABASE_PATH", str(DATA_DIR / "trains.db")))
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "30"))
ARRIVAL_WINDOW_MINUTES = int(os.environ.get("ARRIVAL_WINDOW_MINUTES", "10"))
DATA_RETENTION_HOURS = int(os.environ.get("DATA_RETENTION_HOURS", "2"))

SESSION_COOKIE_NAME = "tf_session"
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 7

FEED_STALE_THRESHOLD_SECONDS = int(os.environ.get("FEED_STALE_THRESHOLD_SECONDS", "90"))
EVENT_FUTURE_MARGIN_SECONDS = int(os.environ.get("EVENT_FUTURE_MARGIN_SECONDS", "30"))
HISTORY_LIMIT = int(os.environ.get("HISTORY_LIMIT", "30"))
LATE_THRESHOLD_SECONDS = int(os.environ.get("LATE_THRESHOLD_SECONDS", "60"))
EARLY_THRESHOLD_SECONDS = int(os.environ.get("EARLY_THRESHOLD_SECONDS", "60"))
TRAIN_IN_FRONT_MAX_STOPS_AHEAD = int(os.environ.get("TRAIN_IN_FRONT_MAX_STOPS_AHEAD", "2"))
PUNCTUALITY_DAY_THRESHOLD = float(os.environ.get("PUNCTUALITY_DAY_THRESHOLD", "0.99"))
PUNCTUALITY_MIN_TRACKING_MINUTES = int(os.environ.get("PUNCTUALITY_MIN_TRACKING_MINUTES", "60"))
CONSIST_REVERSAL_MAX_GAP_MINUTES = int(os.environ.get("CONSIST_REVERSAL_MAX_GAP_MINUTES", "20"))
OLD_FRIEND_MIN_GAP_MINUTES = int(os.environ.get("OLD_FRIEND_MIN_GAP_MINUTES", "60"))
OLD_FRIEND_DURATION_MINUTES = int(os.environ.get("OLD_FRIEND_DURATION_MINUTES", "2"))
SNORING_STATION_MINUTES = int(os.environ.get("SNORING_STATION_MINUTES", "20"))
GROGGY_DURATION_MINUTES = int(os.environ.get("GROGGY_DURATION_MINUTES", "2"))
CARTO_API_KEY = os.environ.get("CARTO_API_KEY", "")
