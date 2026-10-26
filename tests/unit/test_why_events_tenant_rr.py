"""Tenant marker and pack filter for memory events (#708).

AC ids follow spec_708_v2 plus the annex (Lena PASS-with-caveats):
AC-001a writer tenant, AC-001b event id includes tenant, AC-002a/b/c pack
filter (ids, tenant field, no-symbol call), AC-003a/b global marker and
Mongo ``$in`` filter, AC-004 reflector tenant and id.

The symbol is synthetic. The issue's red sample used a production ticker;
these tests do not.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from core.tenant_context import tenant_context
from intelligence.memory.models import GLOBAL_TENANT_ID, CoinProfile, MarketEvent
from intelligence.memory.store import COL_EVENTS, InMemoryMemoryStore, MemoryStore
from services.mcp.explain import memory_pack
from strategies.dca_decision_event import persist_dca_decision_event
from strategies.dca_policy import DcaPolicyResult

SYM = "LAB/USDT"
FIXED_TS = "2026-01-02T03:04:05Z"


def _result() -> DcaPolicyResult:
    return DcaPolicyResult(size_mult=1.0, skip=True, reason_codes=("dca_guard_below_avg",))


def _persist(store, tenant: str) -> str:
    with patch("intelligence.memory.store.memory_enabled", return_value=True):
        with tenant_context(tenant, scope="paper"):
            eid = persist_dca_decision_event(
                symbol=SYM,
                result=_result(),
                store=store,
                index_rag=False,
            )
    assert eid, "event not written"
    return eid


def _event(event_id: str, tenant_id: str, *, symbol: str = SYM) -> MarketEvent:
    return MarketEvent(
        event_id=event_id,
        timestamp=FIXED_TS,
        event_type="dca_decision",
        symbols=[symbol],
        tenant_id=tenant_id,
    )


def test_dca_decision_event_written_in_henry_context_carries_henry():
    """AC-001a: the DCA writer stamps the cycle tenant, not the dataclass default."""
    store = InMemoryMemoryStore()
    eid = _persist(store, "henry")
    ev = store.get_event(eid)
    assert ev is not None
    assert ev.tenant_id == "henry", f"tenant_id={ev.tenant_id!r}"


def test_memory_pack_for_henry_excludes_default_tenant_event():
    """Issue control: a default-tenant row is not in henry's pack."""
    store = InMemoryMemoryStore()
    store.upsert_event(_event("ev_default_only", "default"))
    out = memory_pack("henry", SYM, store=store)
    leaked = [e for e in out["events"] if e.get("tenant_id") == "default"]
    assert leaked == [], f"henry pack returned default events: {[e['event_id'] for e in leaked]}"


def test_control_memory_pack_for_default_returns_default_event():
    store = InMemoryMemoryStore()
    store.upsert_event(_event("ev_default_only", "default"))
    out = memory_pack("default", SYM, store=store)
    assert [e["event_id"] for e in out["events"]] == ["ev_default_only"]


def test_ac_001b_708f_same_second_ids_differ_by_tenant(monkeypatch):
    """AC-001b / 708f: henry and default in one second are two rows."""
    monkeypatch.setattr(
        "strategies.dca_decision_event._utc_now_iso", lambda: FIXED_TS
    )
    store = InMemoryMemoryStore()
    henry = _persist(store, "henry")
    default = _persist(store, "default")
    assert henry != default
    assert store.get_event(henry).tenant_id == "henry"
    assert store.get_event(default).tenant_id == "default"


def _mixed_store() -> InMemoryMemoryStore:
    store = InMemoryMemoryStore()
    store.upsert_event(_event("ev_henry", "henry"))
    store.upsert_event(_event("ev_default", "default"))
    store.upsert_event(_event("ev_other", "other"))
    store.upsert_event(_event("ev_global", GLOBAL_TENANT_ID))
    return store


def test_ac_002a_708b_pack_ids_are_own_tenant_plus_global():
    """AC-002a / 708b: ids, not a stripped tenant field, decide membership."""
    store = _mixed_store()
    out = memory_pack("henry", SYM, store=store)
    assert sorted(e["event_id"] for e in out["events"]) == ["ev_global", "ev_henry"]


def test_ac_002b_708c_pack_keeps_tenant_id_on_returned_events():
    """AC-002b / 708c: hiding the field must not satisfy the pack filter."""
    store = _mixed_store()
    out = memory_pack("henry", SYM, store=store)
    by_id = {e["event_id"]: e for e in out["events"]}
    assert by_id["ev_henry"]["tenant_id"] == "henry"
    assert by_id["ev_global"]["tenant_id"] == GLOBAL_TENANT_ID


def test_ac_002c_n1_pack_without_symbol_filters_tenant():
    """AC-002c / n1: the no-symbol list_events call receives the tenant."""
    store = _mixed_store()
    out = memory_pack("henry", None, store=store)
    assert sorted(e["event_id"] for e in out["events"]) == ["ev_global", "ev_henry"]
    assert all(e.get("tenant_id") for e in out["events"])


def test_ac_003b_708h_in_memory_list_includes_global_only():
    """AC-003b / 708h: in-memory filter is own tenant plus global."""
    store = _mixed_store()
    ids = sorted(e.event_id for e in store.list_events(symbol=SYM, tenant_id="henry"))
    assert ids == ["ev_global", "ev_henry"]


class _MemCursor:
    def __init__(self, docs: list[dict]):
        self._docs = list(docs)

    def sort(self, key, direction):
        self._docs.sort(key=lambda d: d.get(key) or "", reverse=int(direction) < 0)
        return self

    def limit(self, n):
        self._docs = self._docs[: int(n)]
        return self

    def __iter__(self):
        return iter(self._docs)


def _mongo_match(doc: dict, query: dict) -> bool:
    for key, expected in query.items():
        val = doc.get(key)
        if isinstance(expected, dict) and "$in" in expected:
            allowed = set(expected["$in"])
            if isinstance(val, list):
                if not any(item in allowed for item in val):
                    return False
            elif val not in allowed:
                return False
            continue
        if isinstance(expected, dict) and "$gte" in expected:
            if (val or "") < expected["$gte"]:
                return False
            continue
        if val != expected:
            return False
    return True


class _MemCol:
    def __init__(self):
        self.docs: dict[str, dict] = {}

    def replace_one(self, _filt, doc, upsert=False):
        self.docs[doc["_id"]] = dict(doc)
        return SimpleNamespace(acknowledged=True)

    def find_one(self, query):
        if "_id" in query and len(query) == 1:
            return self.docs.get(query["_id"])
        for doc in self.docs.values():
            if _mongo_match(doc, query):
                return doc
        return None

    def find(self, query):
        return _MemCursor([d for d in self.docs.values() if _mongo_match(d, query)])


class _MemDB(dict):
    def __getitem__(self, name):
        if name not in self:
            dict.__setitem__(self, name, _MemCol())
        return dict.__getitem__(self, name)


def test_ac_003b_708e_n2_mongo_filter_includes_global_event():
    """AC-003b / 708e / n2: Mongo list uses ``$in [tenant, global]``."""
    store = MemoryStore(db=_MemDB())
    store.upsert_event(_event("ev_henry", "henry"))
    store.upsert_event(_event("ev_default", "default"))
    store.upsert_event(_event("ev_global", GLOBAL_TENANT_ID))
    ids = sorted(e.event_id for e in store.list_events(symbol=SYM, tenant_id="henry"))
    assert ids == ["ev_global", "ev_henry"]
    # The clause is the Mongo query, not an in-memory post-filter.
    query_docs = store.db[COL_EVENTS].find({"tenant_id": {"$in": ["henry", GLOBAL_TENANT_ID]}})
    assert sorted(d["event_id"] for d in query_docs) == ["ev_global", "ev_henry"]
    pack = memory_pack("henry", SYM, store=store)
    assert sorted(e["event_id"] for e in pack["events"]) == ["ev_global", "ev_henry"]


def test_ac_004_omitted_tenant_keeps_unfiltered_list():
    """Readers that do not pass tenant_id stay unfiltered (#721, not this fix)."""
    store = _mixed_store()
    ids = sorted(e.event_id for e in store.list_events(symbol=SYM))
    assert ids == ["ev_default", "ev_global", "ev_henry", "ev_other"]


def test_ac_003a_708g_w1_macro_writer_is_global():
    """AC-003a / w1: macro writer stamps global inside a tenant cycle."""
    from intelligence.macro.sync import _ingest_event

    store = InMemoryMemoryStore()
    with tenant_context("henry", scope="paper"):
        assert _ingest_event(
            store,
            source="calendar",
            event_type="macro",
            description="scheduled window",
            impact=0.1,
            symbols=[SYM],
            stable_key="lab-window",
        )
    ev = next(iter(store.list_events()))
    assert ev.tenant_id == GLOBAL_TENANT_ID


def test_ac_003a_w2_regime_writer_is_global():
    """AC-003a / w2: regime writer does not inherit the ambient tenant."""
    from intelligence.memory.event_ingest import ingest_regime_event

    store = InMemoryMemoryStore()
    with tenant_context("henry", scope="paper"):
        ev = ingest_regime_event(source="oracle", regime="NEUTRAL", store=store)
    assert ev is not None
    assert ev.tenant_id == GLOBAL_TENANT_ID


def test_ac_003a_w3_news_writer_is_global():
    """AC-003a / w3: news uses the global marker, not resolve_tenant_id()."""
    from intelligence.memory.event_ingest import ingest_news_item

    store = InMemoryMemoryStore()
    with tenant_context("henry", scope="paper"):
        ev = ingest_news_item(
            title="scheduled listing note",
            url="https://example.test/lab-note",
            source="desk",
            symbols=[SYM],
            store=store,
        )
    assert ev is not None
    assert ev.tenant_id == GLOBAL_TENANT_ID


def test_ac_003a_w4_social_writer_is_global():
    """AC-003a / w4."""
    from intelligence.memory.social_ingest import ingest_social_signal

    store = InMemoryMemoryStore()
    with tenant_context("henry", scope="paper"):
        ev = ingest_social_signal(
            source="feed",
            event_type="social",
            symbol=SYM,
            impact=0.2,
            description="feed row",
            stable_key="lab-social",
            store=store,
        )
    assert ev is not None
    assert ev.tenant_id == GLOBAL_TENANT_ID


def test_ac_003a_w5_move_writer_is_global():
    """AC-003a / w5."""
    from intelligence.memory.move_attribution import MoveSnap, build_attribution_event

    with tenant_context("henry", scope="paper"):
        ev = build_attribution_event(MoveSnap(symbol=SYM, chg_1h=1.5), [])
    assert ev.tenant_id == GLOBAL_TENANT_ID


def test_ac_003a_w6_henry_pack_sees_global_news_and_social():
    """AC-003a / w6: henry's pack includes global news and social rows."""
    from intelligence.memory.event_ingest import ingest_news_item
    from intelligence.memory.social_ingest import ingest_social_signal

    store = InMemoryMemoryStore()
    with tenant_context("henry", scope="paper"):
        news = ingest_news_item(
            title="scheduled listing note",
            url="https://example.test/lab-note-2",
            source="desk",
            symbols=[SYM],
            store=store,
        )
        social = ingest_social_signal(
            source="feed",
            event_type="social",
            symbol=SYM,
            impact=0.2,
            description="feed row two",
            stable_key="lab-social-2",
            store=store,
        )
    out = memory_pack("henry", SYM, store=store)
    ids = {e["event_id"] for e in out["events"]}
    assert news.event_id in ids
    assert social.event_id in ids
    assert all(e["tenant_id"] == GLOBAL_TENANT_ID for e in out["events"])


def test_ac_003a_coin_facts_writer_is_global():
    """AC-003a: coin-fact drafts are market-wide."""
    from intelligence.memory.coin_facts import CoinFactDraft
    from intelligence.memory.coin_facts_ingest import draft_to_event

    with tenant_context("henry", scope="paper"):
        ev = draft_to_event(
            CoinFactDraft(
                event_type="listing",
                impact_score=0.2,
                description="listed on the venue",
                source="cmc_ai_updates",
            ),
            symbol=SYM,
            slug="lab",
        )
    assert ev is not None
    assert ev.tenant_id == GLOBAL_TENANT_ID


def test_ac_q2_news_reupsert_keeps_existing_tenant():
    """Q-2: merging an existing news row does not relabel its tenant."""
    from intelligence.memory.event_ingest import ingest_news_item

    store = InMemoryMemoryStore()
    first = ingest_news_item(
        title="scheduled listing note",
        url="https://example.test/lab-keep",
        source="desk",
        symbols=[SYM],
        store=store,
    )
    assert first.tenant_id == GLOBAL_TENANT_ID
    first.tenant_id = "default"
    store.upsert_event(first)
    second = ingest_news_item(
        title="scheduled listing note",
        url="https://example.test/lab-keep",
        source="desk",
        symbols=[SYM, "LAB/USDT"],
        store=store,
    )
    assert second.tenant_id == "default"


def test_ac_004_r1_r2_reflector_event_is_tenant_scoped():
    """AC-004 / r1 / r2: soft_block carries the profile tenant and a distinct id."""
    from intelligence.memory.reflector import reflect

    def _one(tenant: str) -> MarketEvent:
        store = InMemoryMemoryStore()
        store.upsert_profile(
            CoinProfile(
                symbol=SYM,
                ledger_scope="paper",
                tenant_id=tenant,
                sells_30d=2,
                entry_bias="soft_block",
                size_bias=1.0,
                rationale="soft_block",
            )
        )
        reflect(store, tenant_id=tenant, ledger_scope="paper", min_samples=1)
        rows = [e for e in store.list_events() if e.event_type == "soft_block"]
        assert len(rows) == 1
        assert rows[0].tenant_id == tenant
        assert rows[0].tenant_id != GLOBAL_TENANT_ID
        return rows[0]

    henry = _one("henry")
    default = _one("default")
    assert henry.event_id != default.event_id


def test_p708_default_pack_excludes_henry_and_both_see_global():
    """H1: default pack has no henry row; both packs include the global row."""
    store = InMemoryMemoryStore()
    store.upsert_event(_event("h", "henry"))
    store.upsert_event(_event("d", "default"))
    store.upsert_event(_event("g", GLOBAL_TENANT_ID))
    default_ids = sorted(e["event_id"] for e in memory_pack("default", SYM, store=store)["events"])
    henry_ids = sorted(e["event_id"] for e in memory_pack("henry", SYM, store=store)["events"])
    assert default_ids == ["d", "g"], default_ids
    assert henry_ids == ["g", "h"], henry_ids


def test_h3_dca_writer_without_tenant_context_logs_and_does_not_write():
    """H3: no tenant context logs ERROR and does not store a default-tenant event."""
    store = InMemoryMemoryStore()
    with (
        patch("intelligence.memory.store.memory_enabled", return_value=True),
        patch("logger.log") as logged,
    ):
        eid = persist_dca_decision_event(
            symbol=SYM,
            result=_result(),
            store=store,
            index_rag=False,
        )
    assert eid == ""
    assert store.list_events() == []
    levels = [call.args[1] for call in logged.call_args_list if len(call.args) > 1]
    assert "ERROR" in levels
