"""A Telegram chat must route to one tenant — Henry 2026-08-15 collision."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from storage.mongo_client import TEST_DB_NAME, drop_database, get_database
from storage.tenant_registry import (
    TENANTS_COLLECTION,
    find_tenant_by_owner_chat_id,
    link_tenant_owner_chat,
)


class TestTenantChatExclusive(unittest.TestCase):
    CHAT = "6512212782"

    def setUp(self):
        os.environ["PYTEST_RUNNING"] = "1"
        os.environ["MONGODB_DB"] = TEST_DB_NAME
        os.environ["TENANT_SECRET_KEY"] = "MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDE="
        drop_database(test=True)
        self.coll = get_database(test=True)[TENANTS_COLLECTION]

    def tearDown(self):
        drop_database(test=True)

    def _insert(self, tenant_id: str, chat: str, *, created: str, updated: str):
        self.coll.insert_one(
            {
                "tenant_id": tenant_id,
                "status": "active",
                "telegram": {"owner_chat_id": chat},
                "created_at": created,
                "updated_at": updated,
            }
        )

    def test_collision_uses_oldest_created_not_last_updated(self):
        self._insert(
            "henry",
            self.CHAT,
            created="2026-07-14T18:53:01",
            updated="2026-08-04T22:04:56",
        )
        self._insert(
            "decisions",
            self.CHAT,
            created="2026-08-15T05:55:25",
            updated="2026-08-15T05:59:11",
        )
        hit = find_tenant_by_owner_chat_id(self.CHAT, test=True)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["tenant_id"], "henry")

    def test_link_refuses_chat_already_bound_to_other_tenant(self):
        self._insert(
            "henry",
            self.CHAT,
            created="2026-07-14T18:53:01",
            updated="2026-08-04T22:04:56",
        )
        self._insert(
            "decisions",
            "",
            created="2026-08-15T05:55:25",
            updated="2026-08-15T05:59:11",
        )
        ok, msg = link_tenant_owner_chat("decisions", self.CHAT, test=True)
        self.assertFalse(ok)
        self.assertIn("henry", msg)
        still = find_tenant_by_owner_chat_id(self.CHAT, test=True)
        self.assertEqual(still["tenant_id"], "henry")
        self.assertEqual(
            (self.coll.find_one({"tenant_id": "decisions"}) or {})
            .get("telegram", {})
            .get("owner_chat_id"),
            "",
        )


if __name__ == "__main__":
    unittest.main()
