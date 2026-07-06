"""Kalshi trade API v2 client (RSA-PSS signed) plus a deterministic mock for paper/pipeline testing.

All prices are normalized to dollars (0..1) at this boundary; Kalshi's API speaks cents.
"""

import base64
import hashlib
import json
import os
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

HOST = "https://api.elections.kalshi.com"
PATH_PREFIX = "/trade-api/v2"


class KalshiError(Exception):
    pass


def _cents_to_dollars(v):
    return None if v is None else round(v / 100.0, 2)


def _dollars(v):
    """Parse the current API's dollar-string fields ('0.5500' -> 0.55)."""
    if v in (None, ""):
        return None
    return round(float(v), 4)


class KalshiClient:
    def __init__(self, key_id: str = None, private_key_path: str = None):
        from cryptography.hazmat.primitives import serialization

        self.key_id = key_id or os.environ.get("KALSHI_API_KEY_ID")
        pk_path = private_key_path or os.environ.get("KALSHI_PRIVATE_KEY_PATH")
        if not self.key_id or not pk_path:
            raise KalshiError("KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH not configured")
        with open(pk_path, "rb") as f:
            self._key = serialization.load_pem_private_key(f.read(), password=None)
        self.session = requests.Session()

    # ---- signing / transport ------------------------------------------
    def _headers(self, method: str, path: str) -> dict:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        ts = str(int(time.time() * 1000))
        msg = ts + method.upper() + PATH_PREFIX + path.split("?")[0]
        sig = self._key.sign(
            msg.encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, params=None, body=None):
        url = HOST + PATH_PREFIX + path
        last_err = None
        for attempt in range(3):
            try:
                resp = self.session.request(
                    method, url, params=params,
                    json=body,
                    headers=self._headers(method, path),
                    timeout=20,
                )
                if resp.status_code >= 500:
                    raise KalshiError(f"{resp.status_code}: {resp.text[:200]}")
                if resp.status_code >= 400:
                    # client errors are not retryable
                    raise KalshiError(f"{resp.status_code}: {resp.text[:500]}")
                return resp.json()
            except (requests.ConnectionError, requests.Timeout, KalshiError) as e:
                last_err = e
                if isinstance(e, KalshiError) and str(e)[:1] == "4":
                    raise
                time.sleep(2 ** attempt)
        raise KalshiError(f"Kalshi API unreachable after retries: {last_err}")

    # ---- endpoints ------------------------------------------------------
    def exchange_ok(self) -> bool:
        try:
            st = self._request("GET", "/exchange/status")
            return bool(st.get("trading_active", st.get("exchange_active", False)))
        except Exception:
            return False

    def get_markets(self, max_close_ts: int = None, limit: int = 1000,
                    min_volume: int = None) -> list[dict]:
        out, cursor = [], None
        while len(out) < limit:
            params = {"status": "open", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            if max_close_ts:
                params["max_close_ts"] = max_close_ts
            if min_volume:
                params["min_volume"] = str(min_volume)
            data = self._request("GET", "/markets", params=params)
            for m in data.get("markets", []):
                out.append(self._normalize(m))
            cursor = data.get("cursor")
            if not cursor:
                break
        return out

    def get_market(self, ticker: str) -> dict:
        data = self._request("GET", f"/markets/{ticker}")
        return self._normalize(data["market"])

    def get_event_category(self, event_ticker: str) -> str:
        if not hasattr(self, "_event_cache"):
            self._event_cache = {}
        if event_ticker not in self._event_cache:
            data = self._request("GET", f"/events/{event_ticker}")
            self._event_cache[event_ticker] = (
                data.get("event", {}).get("category") or "uncategorized").lower()
        return self._event_cache[event_ticker]

    def get_orderbook(self, ticker: str, depth: int = 10) -> dict:
        data = self._request("GET", f"/markets/{ticker}/orderbook", params={"depth": depth})
        # Current API: {"orderbook_fp": {"yes_dollars": [[price, qty],...], "no_dollars": [...]}}
        # Legacy:      {"orderbook":    {"yes": [[price_cents, qty],...],  "no": [...]}}
        # Either way these are resting bids: a resting NO bid at q fills a YES buyer at 1-q.
        if "orderbook_fp" in data:
            ob = data["orderbook_fp"] or {}
            def levels(side):
                return [(_dollars(p), float(q)) for p, q in (ob.get(side) or [])]
            return {"yes_bids": levels("yes_dollars"), "no_bids": levels("no_dollars")}
        ob = data.get("orderbook") or {}
        def levels_cents(side):
            return [(_cents_to_dollars(p), q) for p, q in (ob.get(side) or [])]
        return {"yes_bids": levels_cents("yes"), "no_bids": levels_cents("no")}

    def get_balance(self) -> float:
        data = self._request("GET", "/portfolio/balance")
        if "balance_dollars" in data:
            return float(data["balance_dollars"])
        return data.get("balance", 0) / 100.0

    def create_order(self, ticker: str, side: str, count: int, price: float,
                     action: str = "buy", client_order_id: str = None) -> dict:
        body = {
            "ticker": ticker,
            "action": action,
            "side": side,                      # 'yes' | 'no'
            "count": count,
            "type": "limit",
            f"{side}_price": int(round(price * 100)),
            "client_order_id": client_order_id or f"oracle-{int(time.time()*1000)}",
        }
        return self._request("POST", "/portfolio/orders", body=body)

    @staticmethod
    def _normalize(m: dict) -> dict:
        if "yes_bid_dollars" in m:  # current API: dollar-string fields
            price = {k: _dollars(m.get(f"{k}_dollars")) for k in
                     ("yes_bid", "yes_ask", "no_bid", "no_ask", "last_price")}
            volume = float(m.get("volume_fp") or 0)
            open_interest = float(m.get("open_interest_fp") or 0)
        else:  # legacy: integer cents
            price = {k: _cents_to_dollars(m.get(k)) for k in
                     ("yes_bid", "yes_ask", "no_bid", "no_ask", "last_price")}
            volume = m.get("volume") or 0
            open_interest = m.get("open_interest") or 0
        return {
            "market_id": m.get("ticker"),
            "event_ticker": m.get("event_ticker"),
            "title": m.get("title") or m.get("yes_sub_title") or m.get("ticker"),
            "category": (m.get("category") or "").lower(),  # enriched from the event
            "mve": bool(m.get("mve_collection_ticker")),    # multi-leg parlay market
            **price,
            "volume": volume,
            "open_interest": open_interest,
            "close_time": m.get("close_time"),
            "status": m.get("status"),
            "result": m.get("result"),  # 'yes' | 'no' | '' when settled
            "rules": (m.get("rules_primary") or "")[:1500],
        }


class MockKalshiClient:
    """Deterministic fake exchange for Phase-0 pipeline testing.

    - Generates a fixed universe of markets with hidden 'true' probabilities.
    - Supports a fast-forward clock stored in state/mock_state.json so a whole
      round can be simulated in minutes.
    - Markets resolve after their close time using a seeded RNG vs the true prob.
    """

    CATEGORIES = ["economics", "weather", "sports", "politics", "science"]

    def __init__(self, state_path: Path):
        self.state_path = Path(state_path)
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text())
        else:
            self.state = {"offset_days": 0, "epoch": datetime.now(timezone.utc).isoformat()}
            self._save()

    def _save(self):
        self.state_path.write_text(json.dumps(self.state))

    def now(self) -> datetime:
        return datetime.now(timezone.utc) + timedelta(days=self.state["offset_days"])

    def fast_forward(self, days: int):
        self.state["offset_days"] += days
        self._save()

    def exchange_ok(self) -> bool:
        return True

    def _market(self, i: int, epoch: datetime) -> dict:
        rng = random.Random(f"market-{i}")
        true_p = round(rng.uniform(0.05, 0.95), 2)
        # market price = true prob + persistent bias, so real edges exist for agents to find
        bias = rng.uniform(-0.12, 0.12)
        mid = min(0.97, max(0.03, true_p + bias))
        spread = rng.choice([0.01, 0.02, 0.03])
        close = epoch + timedelta(days=rng.randint(1, 10), hours=rng.randint(0, 23))
        cat = self.CATEGORIES[i % len(self.CATEGORIES)]
        settled = close <= self.now()
        outcome = None
        if settled:
            outcome = "yes" if random.Random(f"outcome-{i}").random() < true_p else "no"
        return {
            "market_id": f"MOCK-{cat[:4].upper()}-{i:03d}",
            "title": f"Mock {cat} event #{i}: will threshold be exceeded by {close.date()}?",
            "category": cat,
            "yes_bid": round(mid - spread / 2, 2),
            "yes_ask": round(mid + spread / 2, 2),
            "no_bid": round(1 - mid - spread / 2, 2),
            "no_ask": round(1 - mid + spread / 2, 2),
            "last_price": round(mid, 2),
            "volume": random.Random(f"vol-{i}").randint(100, 20000),
            "open_interest": 5000,
            "close_time": close.isoformat(),
            "status": "settled" if settled else "active",
            "result": outcome or "",
            "rules": "Mock market resolves YES if the synthetic threshold is exceeded.",
            "_true_p": true_p,
        }

    def _universe(self) -> list[dict]:
        epoch = datetime.fromisoformat(self.state["epoch"])
        return [self._market(i, epoch) for i in range(60)]

    def get_markets(self, **_) -> list[dict]:
        return [m for m in self._universe() if m["status"] == "active"]

    def get_market(self, ticker: str) -> dict:
        for m in self._universe():
            if m["market_id"] == ticker:
                return m
        raise KalshiError(f"unknown mock market {ticker}")

    def get_orderbook(self, ticker: str, depth: int = 10) -> dict:
        m = self.get_market(ticker)
        # plenty of depth at the touch, a bit more 1-2 cents away
        return {
            "yes_bids": [(m["yes_bid"], 400), (round(m["yes_bid"] - 0.01, 2), 600)],
            "no_bids": [(m["no_bid"], 400), (round(m["no_bid"] - 0.01, 2), 600)],
        }

    def get_balance(self) -> float:
        return 200.0

    def create_order(self, ticker, side, count, price, action="buy", client_order_id=None):
        return {"order": {"order_id": f"mock-{ticker}-{side}-{count}", "status": "executed"}}
