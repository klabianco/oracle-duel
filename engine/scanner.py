"""Market scanner: build the shared daily list of forecastable markets."""

from datetime import datetime, timedelta, timezone


def _parse_ts(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def scan(client, cfg: dict, now: datetime = None) -> list[dict]:
    sc = cfg["scanner"]
    now = now or getattr(client, "now", lambda: datetime.now(timezone.utc))()
    min_close = now + timedelta(hours=sc["min_horizon_hours"])
    max_close = now + timedelta(days=sc["max_horizon_days"])
    excludes = [p.lower() for p in sc["exclude_title_patterns"]]

    markets = client.get_markets(max_close_ts=int(max_close.timestamp()))
    picked = []
    for m in markets:
        if not m.get("close_time") or m.get("yes_ask") is None or m.get("yes_bid") is None:
            continue
        close = _parse_ts(m["close_time"])
        if not (min_close <= close <= max_close):
            continue
        title = (m.get("title") or "").lower()
        if any(p in title for p in excludes):
            continue
        if (m.get("volume") or 0) < sc["min_volume"]:
            continue
        if m["yes_ask"] <= 0.01 or m["yes_ask"] >= 0.99:
            continue  # near-certain markets carry no learning signal
        picked.append(m)

    picked.sort(key=lambda m: m.get("volume", 0), reverse=True)
    return picked[: sc["target_markets"]]
