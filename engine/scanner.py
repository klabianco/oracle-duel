"""Market scanner: build the shared daily list of forecastable markets."""

from datetime import datetime, timedelta, timezone


def _parse_ts(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def scan(client, cfg: dict, now: datetime = None,
         exclude_market_ids: set[str] = None) -> list[dict]:
    sc = cfg["scanner"]
    now = now or getattr(client, "now", lambda: datetime.now(timezone.utc))()
    min_close = now + timedelta(hours=sc["min_horizon_hours"])
    max_close = now + timedelta(days=sc["max_horizon_days"])
    excludes = [p.lower() for p in sc["exclude_title_patterns"]]
    exclude_market_ids = exclude_market_ids or set()

    exclude_cats = [c.lower() for c in sc.get("exclude_categories", [])]

    # Fetch in ascending close-time windows. The API fills its page cap from the
    # TOP of the window (latest-closing first), so a single wide fetch returns
    # only far-dated markets and fast-resolving ones never reach the filters.
    edges = sorted({min(d, sc["max_horizon_days"]) for d in
                    (sc["min_horizon_hours"] / 24.0, 1, 2, 3, 7,
                     sc["max_horizon_days"])})
    seen_ids = set()
    markets = []
    for lo, hi in zip(edges, edges[1:]):
        batch = client.get_markets(
            min_close_ts=int((now + timedelta(days=lo)).timestamp()),
            max_close_ts=int((now + timedelta(days=hi)).timestamp()),
            min_volume=sc["min_volume"])
        for m in batch:
            if m["market_id"] not in seen_ids:
                seen_ids.add(m["market_id"])
                markets.append(m)
    picked = []
    for m in markets:
        if m["market_id"] in exclude_market_ids:
            continue
        if not m.get("close_time") or m.get("yes_ask") is None or m.get("yes_bid") is None:
            continue
        if m.get("mve"):
            continue  # multi-leg parlays are pure-noise combos, not forecastable events
        close = _parse_ts(m["close_time"])
        if not (min_close <= close <= max_close):
            continue
        expected = m.get("expected_expiration_time")
        if expected and _parse_ts(expected) <= now:
            continue  # do not forecast events whose expected outcome time has passed
        title = (m.get("title") or "").lower()
        if any(p in title for p in excludes):
            continue
        if (m.get("volume") or 0) < sc["min_volume"]:
            continue
        if m["yes_ask"] <= 0.01 or m["yes_ask"] >= 0.99:
            continue  # near-certain markets carry no learning signal
        picked.append(m)

    # sort for speed-to-verdict: soonest-resolving day first, most liquid within a day
    picked.sort(key=lambda m: (_parse_ts(m["close_time"]).date().toordinal(),
                               -m.get("volume", 0)))

    # Reduce ladder-heavy result sets before event enrichment. Keep generous
    # headroom because exact series metadata and category caps are applied below.
    per_event_cap = sc.get("max_per_event", 2)
    per_series_cap = sc.get("max_per_series", 3)
    rough_event, rough_series, diverse = {}, {}, []
    for m in picked:
        ev = m.get("event_ticker") or m["market_id"]
        series = m.get("series_ticker") or ev.split("-")[0]
        if rough_event.get(ev, 0) >= per_event_cap:
            continue
        if per_series_cap and rough_series.get(series, 0) >= per_series_cap:
            continue
        rough_event[ev] = rough_event.get(ev, 0) + 1
        rough_series[series] = rough_series.get(series, 0) + 1
        diverse.append(m)
        if len(diverse) >= sc["target_markets"] * 8:
            break
    picked = diverse

    # category lives on the event object in the current API; enrich the survivors only
    if picked and hasattr(client, "get_event_metadata"):
        for m in picked:
            try:
                meta = client.get_event_metadata(m["event_ticker"])
                if not m.get("category"):
                    m["category"] = meta["category"]
                if not m.get("series_ticker"):
                    m["series_ticker"] = meta.get("series_ticker")
            except Exception:
                if not m.get("category"):
                    m["category"] = "uncategorized"
    elif picked and not picked[0].get("category") and hasattr(client, "get_event_category"):
        for m in picked:
            try:
                m["category"] = client.get_event_category(m["event_ticker"])
            except Exception:
                m["category"] = "uncategorized"
    picked = [m for m in picked if m.get("category") not in exclude_cats]

    # diversity: at most N markets per underlying event (one concert setlist or
    # esports match can't consume the day's list), per series (the same price
    # ladder relists as a fresh event at every close time, so SOL-5pm-today +
    # SOL-5pm-tomorrow stack past the event cap), and per category (crypto
    # ladders dominate the fast-closing supply and are near-random-walk noise)
    per_cat_cap = sc.get("max_per_category")
    by_event, by_series, by_cat, final = {}, {}, {}, []
    for m in picked:
        ev = m.get("event_ticker") or m["market_id"]
        series = m.get("series_ticker") or ev.split("-")[0]
        cat = m.get("category") or "uncategorized"
        if by_event.get(ev, 0) >= per_event_cap:
            continue
        if per_series_cap and by_series.get(series, 0) >= per_series_cap:
            continue
        if per_cat_cap and by_cat.get(cat, 0) >= per_cat_cap:
            continue
        by_event[ev] = by_event.get(ev, 0) + 1
        by_series[series] = by_series.get(series, 0) + 1
        by_cat[cat] = by_cat.get(cat, 0) + 1
        final.append(m)
    return final[: sc["target_markets"]]
