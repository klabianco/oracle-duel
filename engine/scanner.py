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

    exclude_cats = [c.lower() for c in sc.get("exclude_categories", [])]

    markets = client.get_markets(max_close_ts=int(max_close.timestamp()),
                                 min_volume=sc["min_volume"])
    picked = []
    for m in markets:
        if not m.get("close_time") or m.get("yes_ask") is None or m.get("yes_bid") is None:
            continue
        if m.get("mve"):
            continue  # multi-leg parlays are pure-noise combos, not forecastable events
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
    picked = picked[: sc["target_markets"] * 4]  # headroom for category/event dedup below

    # category lives on the event object in the current API; enrich the survivors only
    if picked and not picked[0].get("category") and hasattr(client, "get_event_category"):
        for m in picked:
            try:
                m["category"] = client.get_event_category(m["event_ticker"])
            except Exception:
                m["category"] = "uncategorized"
    picked = [m for m in picked if m.get("category") not in exclude_cats]

    # diversity: at most N markets per underlying event, so one concert setlist or
    # esports match can't consume the whole day's forecast list
    per_event_cap = sc.get("max_per_event", 2)
    by_event, final = {}, []
    for m in picked:
        ev = m.get("event_ticker") or m["market_id"]
        if by_event.get(ev, 0) >= per_event_cap:
            continue
        by_event[ev] = by_event.get(ev, 0) + 1
        final.append(m)
    return final[: sc["target_markets"]]
