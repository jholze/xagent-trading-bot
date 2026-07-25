"""Optional Telethon E2E against a live local/demo bot.

Skipped unless TELEGRAM_E2E=1 and secrets are present.
Never places live orders — read-only commands only.

Required env:
  TELEGRAM_E2E=1
  TELEGRAM_E2E_API_ID
  TELEGRAM_E2E_API_HASH
  TELEGRAM_E2E_SESSION   # Telethon StringSession
  TELEGRAM_E2E_BOT       # bot username without @
"""

from __future__ import annotations

import asyncio
import os

import pytest

pytestmark = pytest.mark.telegram_e2e


def _e2e_enabled() -> bool:
    if os.environ.get("TELEGRAM_E2E", "").strip() not in ("1", "true", "yes"):
        return False
    need = (
        "TELEGRAM_E2E_API_ID",
        "TELEGRAM_E2E_API_HASH",
        "TELEGRAM_E2E_SESSION",
        "TELEGRAM_E2E_BOT",
    )
    return all((os.environ.get(k) or "").strip() for k in need)


@pytest.mark.skipif(not _e2e_enabled(), reason="TELEGRAM_E2E secrets not set")
def test_help_via_telethon():
    telethon = pytest.importorskip("telethon")
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    api_id = int(os.environ["TELEGRAM_E2E_API_ID"])
    api_hash = os.environ["TELEGRAM_E2E_API_HASH"]
    session = os.environ["TELEGRAM_E2E_SESSION"]
    bot = os.environ["TELEGRAM_E2E_BOT"].lstrip("@")

    async def _run():
        client = TelegramClient(StringSession(session), api_id, api_hash)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                pytest.fail("Telethon session not authorized")
            async with client.conversation(bot, timeout=30) as conv:
                await conv.send_message("/help")
                resp = await conv.get_response()
                assert resp and (resp.text or getattr(resp, "message", None))
                text = (resp.text or "").lower()
                assert any(k in text for k in ("help", "befehl", "command", "/"))
        finally:
            await client.disconnect()

    asyncio.run(_run())
