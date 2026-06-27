"""Keep Telegram webhook aligned with the local ngrok tunnel."""

import os
import time

import requests

from logger import log

_NGROK_API = "http://127.0.0.1:4040/api/tunnels"
_CHECK_INTERVAL_SEC = 300


def get_ngrok_public_url() -> str | None:
    try:
        response = requests.get(_NGROK_API, timeout=3)
        if response.status_code != 200:
            return None
        tunnels = response.json().get("tunnels", [])
        for tunnel in tunnels:
            url = tunnel.get("public_url", "")
            if url.startswith("https://"):
                return url.rstrip("/")
    except Exception:
        return None
    return None


def probe_webhook_url(public_url: str) -> bool:
    """POST like Telegram does; confirms ngrok → Flask path is alive."""
    try:
        response = requests.post(
            f"{public_url.rstrip('/')}/",
            json={"update_id": 0},
            headers={
                "Content-Type": "application/json",
                "ngrok-skip-browser-warning": "true",
            },
            timeout=8,
        )
        return response.status_code == 200
    except Exception:
        return False


def ensure_webhook_registered() -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        return False

    public_url = get_ngrok_public_url()
    if not public_url:
        return False

    webhook_url = f"{public_url}/"
    api_base = f"https://api.telegram.org/bot{token}"

    try:
        info_resp = requests.get(f"{api_base}/getWebhookInfo", timeout=10)
        if info_resp.status_code != 200:
            return False
        info = info_resp.json().get("result", {})
        registered = (info.get("url") or "").rstrip("/")
        expected = public_url.rstrip("/")
        last_error = info.get("last_error_message") or ""
        probe_ok = probe_webhook_url(public_url)

        if registered == expected and probe_ok:
            return True

        reason = last_error or f"url mismatch ({registered} != {webhook_url})"
        if registered == expected and not probe_ok:
            reason = "tunnel probe failed"
        log(f"Webhook watchdog re-registering: {reason}", "WARNING")
        set_resp = requests.post(
            f"{api_base}/setWebhook",
            data={
                "url": webhook_url,
                "drop_pending_updates": "false",
                "allowed_updates": '["message","callback_query"]',
            },
            timeout=10,
        )
        if set_resp.status_code == 200 and set_resp.json().get("ok"):
            log(f"Webhook restored: {webhook_url}", "INFO")
            return probe_webhook_url(public_url)
        log(f"Webhook re-register failed: {set_resp.text[:200]}", "WARNING")
    except Exception as e:
        log(f"Webhook watchdog error: {e}", "WARNING")
    return False


def start_webhook_watchdog(interval_sec: int = _CHECK_INTERVAL_SEC):
    """Daemon thread: re-register Telegram webhook when ngrok URL drifts or errors."""

    def _loop():
        while True:
            try:
                ensure_webhook_registered()
            except Exception as e:
                log(f"Webhook watchdog loop error: {e}", "WARNING")
            time.sleep(interval_sec)

    import threading

    thread = threading.Thread(target=_loop, daemon=True, name="webhook-watchdog")
    thread.start()
    return thread