import json
import os
import re
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from openai import OpenAI

from intelligence.xai_auth import XaiEndpoint, fallback_endpoint_after, resolve_xai_endpoint
from logger import log

load_dotenv()

DEFAULT_MODEL = os.getenv("GROK_MODEL", "grok-4")


def _client(endpoint: XaiEndpoint | None = None) -> OpenAI:
    """OpenAI-compatible client for xAI: sidecar `/v1` + trigger token when
    `XAI_USE_SUBSCRIPTION` is on and configured, else api.x.ai + `XAI_API_KEY` (#397)."""
    ep = endpoint or resolve_xai_endpoint()
    return OpenAI(
        api_key=ep.api_key or None,
        base_url=ep.base_url,
    )


def _responses_create(endpoint: XaiEndpoint, **kwargs):
    """`responses.create` on `endpoint`; via the sidecar it is repeated once on
    `XAI_API_KEY` when the sidecar answers 401/403/503 or is unreachable."""
    try:
        return _client(endpoint).responses.create(**kwargs)
    except Exception as e:
        direct = fallback_endpoint_after(e, endpoint, context="grok_x_search")
        if direct is None:
            raise
        if not direct.api_key:
            raise
        return _client(direct).responses.create(**kwargs)


def _extract_response_text(response) -> str:
    for item in response.output:
        if getattr(item, "type", None) == "message":
            for part in item.content:
                text = getattr(part, "text", None)
                if text:
                    return text.strip()
    return ""


def _parse_json_posts(text: str) -> list[dict]:
    if not text:
        return []
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        data = json.loads(cleaned[start : end + 1])
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def fetch_posts_from_handle(
    handle: str,
    days: int = 60,
    max_posts: int = 50,
    model: str = None,
) -> list[dict]:
    """Fetch X posts via Grok x_search tool (no X API bearer token required)."""
    handle = handle.replace("@", "").strip()
    if not handle:
        return []

    endpoint = resolve_xai_endpoint()
    if not endpoint.api_key:
        # flag off (or sidecar not configured) and no metered key → nothing to call with
        log("XAI_API_KEY not set — cannot use Grok X Search", "WARNING")
        return []

    to_date = datetime.now(timezone.utc).date()
    from_date = to_date - timedelta(days=max(days, 1))
    model = model or DEFAULT_MODEL

    prompt = (
        f"Find up to {max_posts} original posts (no retweets, no replies-only) "
        f"from @{handle} between {from_date} and {to_date} that discuss crypto trades, "
        f"altcoins, buy/sell bias, long/short, take profit, or entries.\n\n"
        "Return ONLY a JSON array. Each item must have:\n"
        '- "post_id": tweet id string if known, else "grok_{index}"\n'
        '- "text": full post text\n'
        '- "created_at": ISO8601 timestamp (YYYY-MM-DDTHH:MM:SSZ)\n'
    )

    try:
        response = _responses_create(
            endpoint,
            model=model,
            input=[{"role": "user", "content": prompt}],
            tools=[{
                "type": "x_search",
                "allowed_x_handles": [handle],
                "from_date": str(from_date),
                "to_date": str(to_date),
            }],
        )
        posts = _parse_json_posts(_extract_response_text(response))
        log(f"Grok X Search returned {len(posts)} posts for @{handle}", "INFO")
        return posts[:max_posts]
    except Exception as e:
        log(f"Grok X Search failed for @{handle}: {e}", "WARNING")
        return []