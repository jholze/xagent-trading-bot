"""Minimal Santiment GraphQL client (getMetric)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

log = logging.getLogger("santiment_sidecar.client")

GRAPHQL_URL = "https://api.santiment.net/graphql"

# MVP metrics — global / BTC-ETH focused (low cost).
DEFAULT_QUERIES: list[tuple[str, str, str]] = [
    # (feature_key, slug, metric)
    ("btc_social_volume", "bitcoin", "social_volume_total"),
    ("eth_social_volume", "ethereum", "social_volume_total"),
    ("btc_dev_activity", "bitcoin", "dev_activity"),
    ("eth_dev_activity", "ethereum", "dev_activity"),
]


class SantimentClient:
    def __init__(self, api_key: str, *, timeout: float = 30.0):
        self.api_key = (api_key or "").strip()
        self.timeout = timeout
        self._session = requests.Session()
        if self.api_key:
            self._session.headers.update(
                {
                    "Authorization": f"Apikey {self.api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                }
            )

    def available(self) -> bool:
        return bool(self.api_key)

    def _post(self, query: str, variables: dict | None = None) -> dict:
        if not self.api_key:
            raise RuntimeError("SANTIMENT_API_KEY not set")
        resp = self._session.post(
            GRAPHQL_URL,
            json={"query": query, "variables": variables or {}},
            timeout=self.timeout,
        )
        if resp.status_code == 429:
            raise RuntimeError("Santiment rate limited (429)")
        if resp.status_code != 200:
            raise RuntimeError(f"Santiment HTTP {resp.status_code}: {resp.text[:200]}")
        body = resp.json()
        if body.get("errors"):
            raise RuntimeError(f"Santiment GraphQL errors: {body['errors'][:2]}")
        return body.get("data") or {}

    def get_metric_timeseries(
        self,
        *,
        metric: str,
        slug: str,
        from_iso: str,
        to_iso: str,
        interval: str = "1d",
    ) -> list[dict[str, Any]]:
        query = """
        query($metric: String!, $slug: String!, $from: DateTime!, $to: DateTime!, $interval: interval!) {
          getMetric(metric: $metric) {
            timeseriesData(
              slug: $slug
              from: $from
              to: $to
              interval: $interval
            ) {
              datetime
              value
            }
          }
        }
        """
        data = self._post(
            query,
            {
                "metric": metric,
                "slug": slug,
                "from": from_iso,
                "to": to_iso,
                "interval": interval,
            },
        )
        series = ((data.get("getMetric") or {}).get("timeseriesData")) or []
        return list(series)

    def _window_pairs(self) -> list[tuple[str, str]]:
        """Santiment plans often lag social metrics (~30d); try recent then lagged."""
        now = datetime.now(timezone.utc)
        windows: list[tuple[datetime, datetime]] = [
            (now - timedelta(days=14), now - timedelta(hours=1)),
            (now - timedelta(days=45), now - timedelta(days=31)),
            (now - timedelta(days=90), now - timedelta(days=60)),
        ]
        out = []
        for a, b in windows:
            out.append(
                (
                    a.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    b.strftime("%Y-%m-%dT%H:%M:%SZ"),
                )
            )
        return out

    def fetch_mvp_features(self) -> dict[str, float]:
        """Fetch last values for MVP metrics; missing metrics skipped with log."""
        features: dict[str, float] = {}
        windows = self._window_pairs()
        for key, slug, metric in DEFAULT_QUERIES:
            series: list = []
            last_err: Exception | None = None
            for from_iso, to_iso in windows:
                try:
                    series = self.get_metric_timeseries(
                        metric=metric,
                        slug=slug,
                        from_iso=from_iso,
                        to_iso=to_iso,
                        interval="1d",
                    )
                    if series:
                        break
                except Exception as e:
                    last_err = e
                    series = []
            if not series:
                if last_err:
                    log.warning("metric %s/%s failed: %s", slug, metric, last_err)
                continue
            last = series[-1]
            val = last.get("value")
            if val is None:
                continue
            features[key] = float(val)
            if len(series) >= 2 and series[-2].get("value") is not None:
                prev = float(series[-2]["value"])
                if prev != 0:
                    features[f"{key}_delta_1d"] = (float(val) - prev) / abs(prev)
        return features
