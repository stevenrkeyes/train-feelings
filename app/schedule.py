from __future__ import annotations

from app.config import LATE_THRESHOLD_SECONDS


def is_train_late(
    arrival_delay: int | None,
    departure_delay: int | None,
    *,
    threshold: int | None = None,
) -> bool:
    """True when the train is more than `threshold` seconds behind schedule."""
    limit = LATE_THRESHOLD_SECONDS if threshold is None else threshold
    delays = [delay for delay in (arrival_delay, departure_delay) if delay is not None]
    if not delays:
        return False
    return max(delays) > limit
