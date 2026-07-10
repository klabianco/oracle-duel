from datetime import datetime, timedelta, timezone

from engine import scanner

NOW = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)

CFG = {"scanner": {
    "max_horizon_days": 14,
    "min_horizon_hours": 6,
    "min_volume": 300,
    "target_markets": 40,
    "exclude_title_patterns": [],
    "exclude_categories": [],
    "max_per_event": 2,
}}


def _market(i, close, volume=1000):
    return {
        "market_id": f"M{i}", "event_ticker": f"EV{i}",
        "title": f"Market {i}", "close_time": close.isoformat(),
        "yes_ask": 0.50, "yes_bid": 0.48, "volume": volume,
    }


class PageCappedClient:
    """Mimics the Kalshi API: fills a capped page from the TOP of the
    close-time window (latest-closing first)."""

    PAGE_CAP = 200

    def __init__(self, markets):
        self.markets = markets

    def get_markets(self, max_close_ts=None, min_close_ts=None,
                    limit=1000, min_volume=None):
        ms = [m for m in self.markets
              if (max_close_ts is None or
                  datetime.fromisoformat(m["close_time"]).timestamp() <= max_close_ts)
              and (min_close_ts is None or
                   datetime.fromisoformat(m["close_time"]).timestamp() >= min_close_ts)]
        ms.sort(key=lambda m: m["close_time"], reverse=True)
        return ms[: self.PAGE_CAP]


def test_fast_closing_markets_survive_the_page_cap():
    # 500 markets closing 6-7 days out would fill any single wide fetch;
    # the 30 closing tomorrow must still be picked (soonest-first)
    far = [_market(i, NOW + timedelta(days=6, hours=i % 24)) for i in range(500)]
    near = [_market(1000 + i, NOW + timedelta(hours=18)) for i in range(30)]
    picked = scanner.scan(PageCappedClient(far + near), CFG, now=NOW)

    assert len(picked) == CFG["scanner"]["target_markets"]
    near_ids = {m["market_id"] for m in near}
    assert near_ids <= {m["market_id"] for m in picked}
    # soonest-closing first
    closes = [m["close_time"] for m in picked]
    assert closes == sorted(closes)


def test_per_category_cap_keeps_mix_broad():
    # crypto floods the fastest window; the cap should hold it to 10 and let
    # slower-closing categories fill the rest of the list
    crypto = [dict(_market(i, NOW + timedelta(hours=12 + i)), category="crypto")
              for i in range(30)]
    econ = [dict(_market(100 + i, NOW + timedelta(hours=30)), category="econ")
            for i in range(20)]
    sports = [dict(_market(200 + i, NOW + timedelta(days=4)), category="sports")
              for i in range(20)]
    cfg = {"scanner": {**CFG["scanner"], "max_per_category": 10}}
    picked = scanner.scan(PageCappedClient(crypto + econ + sports), cfg, now=NOW)
    cats = [m["category"] for m in picked]
    # the cap is symmetric and hard: 10 per category, even if the list runs
    # short of target_markets — a narrow day beats a crypto-flooded one
    assert cats.count("crypto") == 10
    assert cats.count("econ") == 10
    assert cats.count("sports") == 10
    assert len(picked) == 30
    # crypto's 10 slots all come from its soonest-closing day
    # (within a day the scanner prefers liquidity over hour-of-close, by design)
    crypto_days = {scanner._parse_ts(m["close_time"]).date()
                   for m in picked if m["category"] == "crypto"}
    assert crypto_days == {(NOW + timedelta(hours=12)).date()}


def test_per_series_cap_stops_relisted_ladders():
    # the same price ladder relists as a fresh event per close time; without a
    # series cap, SOL-today + SOL-tomorrow strikes stack past the event cap
    sol = []
    for day in range(3):                      # three close times of one ladder
        for strike in range(2):               # two strikes each (event cap ok)
            m = _market(day * 10 + strike, NOW + timedelta(hours=18 + day))
            m["event_ticker"] = f"KXSOL-26JUL{9 + day}17"
            sol.append(m)
    other = [_market(100 + i, NOW + timedelta(hours=30)) for i in range(10)]
    picked = scanner.scan(PageCappedClient(sol + other), CFG, now=NOW)
    n_sol = sum(1 for m in picked if m["event_ticker"].startswith("KXSOL"))
    assert n_sol == 3                         # default max_per_series
    assert len(picked) == 13


def test_horizon_bounds_still_respected():
    inside_6h = _market(1, NOW + timedelta(hours=3))          # too soon
    beyond_max = _market(2, NOW + timedelta(days=20))         # too far
    ok = _market(3, NOW + timedelta(days=2))
    picked = scanner.scan(PageCappedClient([inside_6h, beyond_max, ok]), CFG, now=NOW)
    assert [m["market_id"] for m in picked] == ["M3"]


def test_excludes_previously_forecast_and_past_expected_expiration():
    old = _market(1, NOW + timedelta(days=1))
    expired = _market(2, NOW + timedelta(days=1))
    expired["expected_expiration_time"] = (NOW - timedelta(minutes=1)).isoformat()
    ok = _market(3, NOW + timedelta(days=1))
    picked = scanner.scan(
        PageCappedClient([old, expired, ok]), CFG, now=NOW,
        exclude_market_ids={"M1"},
    )
    assert [m["market_id"] for m in picked] == ["M3"]


def test_event_metadata_supplies_real_series_key():
    class MetadataClient(PageCappedClient):
        def get_event_metadata(self, event_ticker):
            return {"category": "crypto", "series_ticker": "REAL-SERIES"}

    markets = [_market(i, NOW + timedelta(days=1)) for i in range(4)]
    cfg = {"scanner": {**CFG["scanner"], "max_per_series": 1}}
    picked = scanner.scan(MetadataClient(markets), cfg, now=NOW)
    assert len(picked) == 1
    assert picked[0]["series_ticker"] == "REAL-SERIES"


def test_mock_universe_survives_the_series_caps(tmp_path):
    # every mock market is its own event/series; per-event and per-series
    # caps of 1 must not collapse the mock universe to a single market
    from engine.kalshi_client import MockKalshiClient

    client = MockKalshiClient(tmp_path / "mock.json")
    cfg = {"scanner": {**CFG["scanner"], "max_per_event": 1, "max_per_series": 1}}
    picked = scanner.scan(client, cfg, now=client.now())
    assert len(picked) >= 30
