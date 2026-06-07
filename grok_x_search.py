import json
import os
import re
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from openai import OpenAI

from logger import log

load_dotenv()

DEFAULT_MODEL = os.getenv("GROK_MODEL", "grok-4")


def _client() -> OpenAI:
    return OpenAI(
        api_key=os.getenv("XAI_API_KEY"),
        base_url="https://api.x.ai/v1",
    )


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

    api_key = os.getenv("XAI_API_KEY")
    if not api_key:
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
        response = _client().responses.create(
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