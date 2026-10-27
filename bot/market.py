"""Revolut X public market data. No API keys needed."""
import time

import requests


class Market:
    def __init__(self, base_url, region="EEA", session=None):
        self.base_url = base_url.rstrip("/")
        self.region = region
        self.http = session or requests.Session()
        self._last_call = 0.0

    def _get(self, path, params=None):
        # Public endpoints allow ~1 request/second.
        wait = 1.1 - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        for attempt in range(3):
            self._last_call = time.time()
            r = self.http.get(f"{self.base_url}{path}", params=params, timeout=20)
            if r.status_code == 429:
                retry_ms = int(r.headers.get("Retry-After", "2000"))
                time.sleep(retry_ms / 1000)
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"rate limited on {path}")

    def candles(self, symbol, interval_min):
        """Closed candles only, oldest first, as dicts of floats."""
        data = self._get(f"/public/candles/{symbol}",
                         {"interval": interval_min, "region": self.region})
        rows = [{
            "start": int(c["start"]),
            "open": float(c["open"]), "high": float(c["high"]),
            "low": float(c["low"]), "close": float(c["close"]),
            "volume": float(c["volume"]),
        } for c in data.get("data", [])]
        rows.sort(key=lambda c: c["start"])
        now_ms = int(time.time() * 1000)
        span = interval_min * 60_000
        return [c for c in rows if c["start"] + span <= now_ms]

    def last_price(self, symbol):
        """Close of the most recent 1-minute candle (including the forming one)."""
        data = self._get(f"/public/candles/{symbol}",
                         {"interval": 1, "region": self.region})
        rows = sorted(data.get("data", []), key=lambda c: int(c["start"]))
        if not rows:
            raise RuntimeError(f"no price for {symbol}")
        last = rows[-1]
        return float(last["close"]), float(last["high"]), float(last["low"])
