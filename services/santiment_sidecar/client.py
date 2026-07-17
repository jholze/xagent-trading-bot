"""Santiment GraphQL client — realtime-first metrics + lag-aware meta."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

log = logging.getLogger("santiment_sidecar.client")

GRAPHQL_URL = "https://api.santiment.net/graphql"

# Last point must be within this many days of "now" to count as policy-fresh.
FRESH_MAX_AGE_DAYS = 2.5

# Realtime path (unrestricted / live on current plan).
REALTIME_QUERIES: list[tuple[str, str, str]] = [
    ("btc_daa", "bitcoin", "daily_active_addresses"),
    ("eth_daa", "ethereum", "daily_active_addresses"),
    ("btc_vol_1d", "bitcoin", "price_volatility_1d"),
    ("eth_vol_1d", "ethereum", "price_volatility_1d"),
    ("btc_dev_activity", "bitcoin", "dev_activity"),
    ("eth_dev_activity", "ethereum", "dev_activity"),
]

# Social: try recent window only; only policy-fresh if last point is fresh.
SOCIAL_QUERIES: list[tuple[str, str, str]] = [
    ("btc_social_volume", "bitcoin", "social_volume_total"),
    ("eth_social_volume", "ethereum", "social_volume_total"),
]

# Leverage (restricted on many plans): funding + OI. Live → policy; lag → research_only.
LEVERAGE_QUERIES: list[tuple[str, str, str, str]] = [
    # key, slug, metric, interval
    ("btc_funding_rate", "bitcoin", "total_funding_rates_aggregated_per_asset", "1h"),
    ("btc_open_interest", "bitcoin", "total_open_interest", "1h"),
]


@dataclass
class FeatureFetchResult:
    features: dict[str, float] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)


def _parse_dt(raw: Any) -> datetime | None:
    if raw is None:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    except Exception:
        return None


def series_lag_days(series: list[dict], *, now: datetime | None = None) -> float | None:
    """Days between now and last timeseries point."""
    if not series:
        return None
    now = now or datetime.now(timezone.utc)
    last = series[-1]
    ts = _parse_dt(last.get("datetime"))
    if ts is None:
        return None
    return max(0.0, (now - ts).total_seconds() / 86400.0)


def is_series_fresh(
    series: list[dict],
    *,
    now: datetime | None = None,
    max_age_days: float = FRESH_MAX_AGE_DAYS,
) -> bool:
    lag = series_lag_days(series, now=now)
    return lag is not None and lag <= max_age_days


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

    def _recent_window(self) -> tuple[str, str]:
        now = datetime.now(timezone.utc)
        a = now - timedelta(days=14)
        b = now - timedelta(hours=1)
        return (
            a.strftime("%Y-%m-%dT%H:%M:%SZ"),
            b.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

    def _apply_series(
        self,
        features: dict[str, float],
        key: str,
        series: list[dict],
    ) -> float | None:
        """Write last value + delta; return lag_days of last point."""
        last = series[-1]
        val = last.get("value")
        if val is None:
            return None
        features[key] = float(val)
        if len(series) >= 2 and series[-2].get("value") is not None:
            prev = float(series[-2]["value"])
            if prev != 0:
                features[f"{key}_delta_1d"] = (float(val) - prev) / abs(prev)
        return series_lag_days(series)

    def _fetch_one(
        self,
        key: str,
        slug: str,
        metric: str,
        from_iso: str,
        to_iso: str,
        *,
        interval: str = "1d",
    ) -> tuple[list[dict], Exception | None]:
        try:
            series = self.get_metric_timeseries(
                metric=metric,
                slug=slug,
                from_iso=from_iso,
                to_iso=to_iso,
                interval=interval,
            )
            return series, None
        except Exception as e:
            return [], e

    def _lagged_research_window(self) -> tuple[str, str]:
        """~30d lag window for restricted SanAPI tiers (research_only)."""
        now = datetime.now(timezone.utc)
        a = now - timedelta(days=34)
        b = now - timedelta(days=31)
        return (
            a.strftime("%Y-%m-%dT%H:%M:%SZ"),
            b.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

    def fetch_features(self) -> FeatureFetchResult:
        """Fetch realtime-first features + meta for health/policy."""
        features: dict[str, float] = {}
        metrics_ok: list[str] = []
        metrics_failed: list[str] = []
        lags: list[float] = []
        policy_inputs: list[str] = []
        social_fresh = False

        from_iso, to_iso = self._recent_window()

        for key, slug, metric in REALTIME_QUERIES:
            series, err = self._fetch_one(key, slug, metric, from_iso, to_iso)
            if not series or not is_series_fresh(series):
                metrics_failed.append(key)
                if err:
                    log.warning("metric %s/%s failed: %s", slug, metric, err)
                elif series:
                    log.warning(
                        "metric %s/%s stale lag=%.1fd",
                        slug,
                        metric,
                        series_lag_days(series) or -1,
                    )
                continue
            lag = self._apply_series(features, key, series)
            metrics_ok.append(key)
            if lag is not None:
                lags.append(lag)

        if any(k.endswith("_daa_delta_1d") or k.endswith("_daa") for k in features):
            if "btc_daa_delta_1d" in features or "eth_daa_delta_1d" in features:
                policy_inputs.append("daa")
        if "btc_vol_1d" in features or "eth_vol_1d" in features:
            policy_inputs.append("vol")
        if any(k.startswith("btc_dev") or k.startswith("eth_dev") for k in features):
            policy_inputs.append("dev")

        social_ok_keys: list[str] = []
        for key, slug, metric in SOCIAL_QUERIES:
            series, err = self._fetch_one(key, slug, metric, from_iso, to_iso)
            if not series:
                metrics_failed.append(key)
                if err:
                    log.warning("metric %s/%s failed: %s", slug, metric, err)
                continue
            if not is_series_fresh(series):
                metrics_failed.append(key)
                log.info(
                    "social %s/%s not policy-fresh lag=%.1fd — excluded from policy",
                    slug,
                    metric,
                    series_lag_days(series) or -1,
                )
                continue
            lag = self._apply_series(features, key, series)
            metrics_ok.append(key)
            social_ok_keys.append(key)
            if lag is not None:
                lags.append(lag)

        if social_ok_keys and (
            "btc_social_volume_delta_1d" in features or "eth_social_volume_delta_1d" in features
        ):
            social_fresh = True
            policy_inputs.append("social")

        leverage_fresh = False
        research_only: list[str] = []
        for key, slug, metric, interval in LEVERAGE_QUERIES:
            series, err = self._fetch_one(
                key, slug, metric, from_iso, to_iso, interval=interval
            )
            if series and is_series_fresh(series):
                lag = self._apply_series(features, key, series)
                metrics_ok.append(key)
                if lag is not None:
                    lags.append(lag)
                leverage_fresh = True
                continue
            # Restricted plans: optional lagged snapshot for research, not policy.
            lag_from, lag_to = self._lagged_research_window()
            series_lag, err_lag = self._fetch_one(
                key, slug, metric, lag_from, lag_to, interval=interval
            )
            if series_lag:
                # Store under research_ prefix so regime never picks them as live.
                rkey = f"research_{key}"
                self._apply_series(features, rkey, series_lag)
                metrics_ok.append(rkey)
                research_only.append(key)
                log.info(
                    "leverage %s/%s research_only (lagged, not policy)",
                    slug,
                    metric,
                )
            else:
                metrics_failed.append(key)
                if err or err_lag:
                    log.warning(
                        "metric %s/%s failed: %s",
                        slug,
                        metric,
                        err or err_lag,
                    )

        if leverage_fresh:
            policy_inputs.append("leverage")
            # Prefer live funding key name expected by score_leverage
            if "btc_funding_rate" in features:
                pass

        meta: dict[str, Any] = {
            "data_lag_days_max": round(max(lags), 3) if lags else None,
            "metrics_ok": metrics_ok,
            "metrics_failed": metrics_failed,
            "policy_inputs": policy_inputs,
            "social_fresh": social_fresh,
            "leverage_fresh": leverage_fresh,
            "research_only": research_only,
            "lagged_excluded_from_policy": (not social_fresh) or (not leverage_fresh and bool(research_only)),
            "fresh_max_age_days": FRESH_MAX_AGE_DAYS,
        }
        return FeatureFetchResult(features=features, meta=meta)

    def fetch_mvp_features(self) -> dict[str, float]:
        """Back-compat: features dict only."""
        return self.fetch_features().features
