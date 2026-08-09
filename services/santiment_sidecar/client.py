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

# Core policy metrics (lean profile — enough for onchain + vol regime).
LEAN_REALTIME_QUERIES: list[tuple[str, str, str]] = [
    ("btc_daa", "bitcoin", "daily_active_addresses"),
    ("eth_daa", "ethereum", "daily_active_addresses"),
    ("btc_vol_1d", "bitcoin", "price_volatility_1d"),
    ("eth_vol_1d", "ethereum", "price_volatility_1d"),
]

# Optional realtime extras (full profile).
DEV_QUERIES: list[tuple[str, str, str]] = [
    ("btc_dev_activity", "bitcoin", "dev_activity"),
    ("eth_dev_activity", "ethereum", "dev_activity"),
]

# Back-compat alias (tests / callers).
REALTIME_QUERIES: list[tuple[str, str, str]] = LEAN_REALTIME_QUERIES + DEV_QUERIES

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


class RateLimitError(RuntimeError):
    """Santiment HTTP 429 — stop remaining fetches in this cycle."""

    def __init__(self, message: str = "Santiment rate limited (429)", *, retry_after_sec: float | None = None):
        super().__init__(message)
        self.retry_after_sec = retry_after_sec


class SantimentClient:
    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = 30.0,
        inter_request_delay_sec: float = 0.35,
        abort_on_rate_limit: bool = True,
        fetch_social: bool = False,
        fetch_leverage: bool = False,
        fetch_dev: bool = False,
        leverage_research_fallback: bool = False,
    ):
        self.api_key = (api_key or "").strip()
        self.timeout = timeout
        self.inter_request_delay_sec = max(0.0, float(inter_request_delay_sec or 0))
        self.abort_on_rate_limit = bool(abort_on_rate_limit)
        self.fetch_social = bool(fetch_social)
        self.fetch_leverage = bool(fetch_leverage)
        self.fetch_dev = bool(fetch_dev)
        self.leverage_research_fallback = bool(leverage_research_fallback)
        self._session = requests.Session()
        self._last_request_at = 0.0
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

    def _throttle(self) -> None:
        if self.inter_request_delay_sec <= 0:
            return
        import time

        now = time.monotonic()
        wait = self.inter_request_delay_sec - (now - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _post(self, query: str, variables: dict | None = None) -> dict:
        if not self.api_key:
            raise RuntimeError("SANTIMENT_API_KEY not set")
        self._throttle()
        resp = self._session.post(
            GRAPHQL_URL,
            json={"query": query, "variables": variables or {}},
            timeout=self.timeout,
        )
        if resp.status_code == 429:
            retry = None
            try:
                ra = resp.headers.get("Retry-After")
                if ra is not None:
                    retry = float(ra)
            except Exception:
                retry = None
            # Body sometimes has human text "Try again in N seconds"
            try:
                text = resp.text or ""
                if "Try again in" in text and retry is None:
                    import re

                    m = re.search(r"Try again in\s+(\d+)\s+seconds", text, re.I)
                    if m:
                        retry = float(m.group(1))
            except Exception:
                pass
            raise RateLimitError(
                "Santiment rate limited (429)",
                retry_after_sec=retry,
            )
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
        except RateLimitError as e:
            # Propagate identity so fetch_features can abort remaining calls.
            return [], e
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

    def _consume_metric(
        self,
        *,
        key: str,
        slug: str,
        metric: str,
        from_iso: str,
        to_iso: str,
        interval: str = "1d",
        features: dict[str, float],
        metrics_ok: list[str],
        metrics_failed: list[str],
        lags: list[float],
        require_fresh: bool = True,
    ) -> None:
        series, err = self._fetch_one(
            key, slug, metric, from_iso, to_iso, interval=interval
        )
        if err and self.abort_on_rate_limit and isinstance(err, RateLimitError):
            metrics_failed.append(key)
            log.warning("metric %s/%s failed: %s — aborting remaining fetches", slug, metric, err)
            raise err
        if not series or (require_fresh and not is_series_fresh(series)):
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
            return
        lag = self._apply_series(features, key, series)
        metrics_ok.append(key)
        if lag is not None:
            lags.append(lag)

    def fetch_features(self) -> FeatureFetchResult:
        """Fetch thrifty realtime-first features + meta for health/policy.

        Lean default: 4 GraphQL calls (BTC/ETH DAA + vol). Optional social/dev/leverage
        only when enabled. Stops remaining calls on 429 when abort_on_rate_limit.
        """
        features: dict[str, float] = {}
        metrics_ok: list[str] = []
        metrics_failed: list[str] = []
        lags: list[float] = []
        policy_inputs: list[str] = []
        social_fresh = False
        leverage_fresh = False
        research_only: list[str] = []
        rate_limited = False
        rate_limit_retry_sec: float | None = None

        from_iso, to_iso = self._recent_window()

        realtime = list(LEAN_REALTIME_QUERIES)
        if self.fetch_dev:
            realtime = realtime + list(DEV_QUERIES)

        try:
            for key, slug, metric in realtime:
                self._consume_metric(
                    key=key,
                    slug=slug,
                    metric=metric,
                    from_iso=from_iso,
                    to_iso=to_iso,
                    features=features,
                    metrics_ok=metrics_ok,
                    metrics_failed=metrics_failed,
                    lags=lags,
                )

            if any(k.endswith("_daa_delta_1d") or k.endswith("_daa") for k in features):
                if "btc_daa_delta_1d" in features or "eth_daa_delta_1d" in features:
                    policy_inputs.append("daa")
            if "btc_vol_1d" in features or "eth_vol_1d" in features:
                policy_inputs.append("vol")
            if any(k.startswith("btc_dev") or k.startswith("eth_dev") for k in features):
                policy_inputs.append("dev")

            if self.fetch_social:
                for key, slug, metric in SOCIAL_QUERIES:
                    series, err = self._fetch_one(key, slug, metric, from_iso, to_iso)
                    if err and self.abort_on_rate_limit and isinstance(err, RateLimitError):
                        metrics_failed.append(key)
                        raise err
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
                    if lag is not None:
                        lags.append(lag)

                if "btc_social_volume_delta_1d" in features or "eth_social_volume_delta_1d" in features:
                    social_fresh = True
                    policy_inputs.append("social")

            if self.fetch_leverage:
                for key, slug, metric, interval in LEVERAGE_QUERIES:
                    series, err = self._fetch_one(
                        key, slug, metric, from_iso, to_iso, interval=interval
                    )
                    if err and self.abort_on_rate_limit and isinstance(err, RateLimitError):
                        metrics_failed.append(key)
                        raise err
                    if series and is_series_fresh(series):
                        lag = self._apply_series(features, key, series)
                        metrics_ok.append(key)
                        if lag is not None:
                            lags.append(lag)
                        leverage_fresh = True
                        continue
                    if not self.leverage_research_fallback:
                        metrics_failed.append(key)
                        if err:
                            log.warning("metric %s/%s failed: %s", slug, metric, err)
                        continue
                    # Restricted plans: optional lagged snapshot for research, not policy.
                    lag_from, lag_to = self._lagged_research_window()
                    series_lag, err_lag = self._fetch_one(
                        key, slug, metric, lag_from, lag_to, interval=interval
                    )
                    if err_lag and self.abort_on_rate_limit and isinstance(err_lag, RateLimitError):
                        metrics_failed.append(key)
                        raise err_lag
                    if series_lag:
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
        except RateLimitError as e:
            rate_limited = True
            rate_limit_retry_sec = e.retry_after_sec
            log.warning(
                "Santiment rate limit — stopping cycle early (ok=%s fail=%s retry=%s)",
                len(metrics_ok),
                len(metrics_failed),
                rate_limit_retry_sec,
            )

        meta: dict[str, Any] = {
            "data_lag_days_max": round(max(lags), 3) if lags else None,
            "metrics_ok": metrics_ok,
            "metrics_failed": metrics_failed,
            "policy_inputs": policy_inputs,
            "social_fresh": social_fresh,
            "leverage_fresh": leverage_fresh,
            "research_only": research_only,
            "lagged_excluded_from_policy": (not social_fresh)
            or (not leverage_fresh and bool(research_only)),
            "fresh_max_age_days": FRESH_MAX_AGE_DAYS,
            "rate_limited": rate_limited,
            "rate_limit_retry_sec": rate_limit_retry_sec,
            "metric_profile": (
                "full"
                if (self.fetch_dev or self.fetch_social or self.fetch_leverage)
                else "lean"
            ),
        }
        return FeatureFetchResult(features=features, meta=meta)

    def fetch_mvp_features(self) -> dict[str, float]:
        """Back-compat: features dict only."""
        return self.fetch_features().features

    # Per-asset metrics for recovery / DCA sniper.
    # Order: free/realtime-first (DAA, vol, dev) then restricted (social/flows/MVRV).
    # Sanbase Pro: restricted metrics often ~30d lag → stored as research_* only.
    # Keep list short — Pro rate limits (e.g. 5k calls/mo) and each metric = 1 call.
    ASSET_SIGNAL_METRICS: list[tuple[str, str, str]] = [
        # key, metric, interval
        ("daa", "daily_active_addresses", "1d"),
        ("vol_1d", "price_volatility_1d", "1d"),
        ("dev_activity", "dev_activity", "1d"),
        ("social_volume", "social_volume_total", "1d"),
        ("exchange_inflow", "exchange_inflow", "1d"),
        ("exchange_outflow", "exchange_outflow", "1d"),
        ("mvrv", "mvrv_usd", "1d"),
    ]

    # Subset when caller wants max rate-limit thrift (global snapshot already covers regime).
    ASSET_SIGNAL_METRICS_LEAN: list[tuple[str, str, str]] = [
        ("daa", "daily_active_addresses", "1d"),
        ("vol_1d", "price_volatility_1d", "1d"),
        ("social_volume", "social_volume_total", "1d"),
        ("exchange_inflow", "exchange_inflow", "1d"),
        ("exchange_outflow", "exchange_outflow", "1d"),
    ]

    def fetch_asset_signals(
        self,
        slug: str,
        *,
        metrics: list[tuple[str, str, str]] | None = None,
        lean: bool = False,
    ) -> dict[str, Any]:
        """Fetch per-asset signal bundle for sniper deep analysis.

        Returns {features, meta} with last values + 1d deltas where available.
        Fail-soft: missing metrics go to meta.metrics_failed.
        Stale/restricted series stored under research_* (not policy-fresh).
        """
        slug = str(slug or "").strip().lower()
        out_features: dict[str, float] = {}
        ok: list[str] = []
        failed: list[str] = []
        research_keys: list[str] = []
        lags: list[float] = []
        if not slug or not self.available():
            return {
                "slug": slug,
                "features": {},
                "meta": {
                    "metrics_ok": [],
                    "metrics_failed": ["no_slug_or_key"],
                    "research_only": [],
                    "fresh": False,
                },
            }
        from_iso, to_iso = self._recent_window()
        metric_list = metrics
        if metric_list is None:
            metric_list = (
                self.ASSET_SIGNAL_METRICS_LEAN if lean else self.ASSET_SIGNAL_METRICS
            )
        for key, metric, interval in metric_list:
            series, err = self._fetch_one(
                key, slug, metric, from_iso, to_iso, interval=interval
            )
            if not series:
                # Restricted tiers: try lagged research window once
                lag_from, lag_to = self._lagged_research_window()
                series_lag, err_lag = self._fetch_one(
                    key, slug, metric, lag_from, lag_to, interval=interval
                )
                if series_lag:
                    rkey = f"research_{key}"
                    self._apply_series(out_features, rkey, series_lag)
                    research_keys.append(key)
                    ok.append(rkey)
                    log.debug(
                        "asset %s %s research_only (lagged, not policy)",
                        slug,
                        metric,
                    )
                    continue
                failed.append(key)
                if err or err_lag:
                    log.debug("asset %s %s fail: %s", slug, metric, err or err_lag)
                continue
            if not is_series_fresh(series):
                failed.append(f"{key}_stale")
                rkey = f"research_{key}"
                lag = self._apply_series(out_features, rkey, series)
                research_keys.append(key)
                if lag is not None:
                    lags.append(lag)
                continue
            lag = self._apply_series(out_features, key, series)
            ok.append(key)
            if lag is not None:
                lags.append(lag)
        return {
            "slug": slug,
            "features": out_features,
            "meta": {
                "metrics_ok": ok,
                "metrics_failed": failed,
                "research_only": research_keys,
                "fresh": bool(ok) and any(not k.startswith("research_") for k in ok),
                "data_lag_days_max": round(max(lags), 3) if lags else None,
            },
        }
