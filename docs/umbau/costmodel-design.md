# CostModel — Design für `core/costs.py` (#301, Phase 1 §2.1)

**Stand:** 5. September 2026 · **Version:** `COST_MODEL_VERSION = "2026-09-v1"` · **Autor:** Claude (C-Design), Umsetzung Grok (G), Audit Claude

## 1. Warum ein Modell statt fünf Konstanten

| Stelle | heute | Richtung des Fehlers |
|---|---|---|
| `hermes/backtester.py:101,144,164` | Slippage 1,5 % **beidseitig** auf `balance`; `pnl = (price−entry)·qty` **brutto** | Balance 15× zu pessimistisch, `pnl` (→ Win-Rate, Sharpe in `hermes/metrics.py`) kostenfrei → **zu optimistisch** |
| `hermes/pipeline_backtest.py:74,88,142,167` | identisch | identisch |
| `intelligence/strategy_backtest.py:103,246,251` | Entry/Exit-Preis geslippt, `pnl` daraus → netto **ohne Fee** | konsistent, aber Fee fehlt |
| `services/portfolio_service.py:64-65,115-117` | Kauf kostenlos; Verkauf −1,5 % nur auf `received`; `pnl` brutto | P&L ≠ Cash bei jedem Verkauf (~67 $ Phantom pro 4.500-$-Ticket) |
| `execution/gate_adapter.py:308 _extract_fee` + `_sync_local_ledger` | echte Fee als Float, **Währung verworfen**; `pnl` aus `portfolio.execute_sell` ohne Fee | Live-Position um die Basis-Fee zu groß gebucht |
| `strategies/short_math.py:78`, `short_policy` | `fee_rate 0.001` Konstante | vierter Wert |
| `strategies/grid.py:120`, `grid_plan.py:466`, `grid_limits.py:47,205` | `assumed_fee_pct 0.1` | fünfter Wert, eigenes Mini-Modell |

**Gate Spot, verifiziert gegen ccxt 4.5.48:** VIP 0 = 0,2 % Maker / 0,2 % Taker (17-stufige Staffel bis 0,055/0,065 %). `feeSide: 'get'` — die Fee fällt in der Währung an, die man **bekommt**: **Kauf → Fee in Basis** (weniger Coins), **Verkauf → Fee in Quote** (weniger USDT). `calculate_fee` bestätigt: BUY 1000 BTC@1 → `fee cost=2.0 currency=BTC`; SELL → `currency=USDT`.

## 2. Schnittstelle

```python
# core/costs.py
from dataclasses import dataclass
from typing import Literal

COST_MODEL_VERSION = "2026-09-v1"
Side = Literal["buy", "sell"]
OrderType = Literal["market", "limit"]
FeeSide = Literal["base", "quote"]

@dataclass(frozen=True)
class CostParams:
    fee_maker_pct: float            # % vom Notional
    fee_taker_pct: float
    slippage_pct: float             # % vom Preis, immer adversarial
    fee_side_buy: FeeSide = "base"  # Gate spot 'get'
    fee_side_sell: FeeSide = "quote"
    funding_rate_8h: float | None = None   # nur swap
    source: str = "config"          # "config" | "exchange"

@dataclass(frozen=True)
class Fill:
    side: Side
    order_type: OrderType
    request_price: float
    fill_price: float       # nach Slippage
    qty_gross: float        # Basis, vor Fee
    qty_net: float          # Basis, tatsächlich gutgeschrieben (Kauf: − fee_base) / belastet (Verkauf: = qty_gross)
    quote_gross: float      # fill_price · qty_gross
    quote_net: float        # Kauf: tatsächlich belastet; Verkauf: tatsächlich gutgeschrieben (− fee_quote)
    fee_base: float
    fee_quote: float
    fee_usdt: float         # Fee in Quote ausgedrückt (fee_base · fill_price + fee_quote) — nur Reporting
    slippage_usdt: float    # |fill_price − request_price| · qty_gross
    cost_model_version: str = COST_MODEL_VERSION

class CostModel:
    def __init__(self, params: CostParams, *, exchange: str = "gate", market: str = "spot"): ...
    @classmethod
    def from_config(cls, config: dict | None, *, exchange="gate", market="spot", symbol: str | None = None) -> "CostModel":
        """Liest config["costs"][exchange][market]; fehlt der Block → VIP-0-Defaults + log WARNING einmalig.
        symbol → Slippage aus costs.slippage_by_tier über volatility tier, sonst slippage_pct des Markts."""
    def fee_pct(self, order_type: OrderType = "market") -> float          # market→taker, limit→maker
    def round_trip_pct(self, order_type: OrderType = "market") -> float   # 2·fee + 2·slippage, als %
    def simulate_buy(self, price: float, *, usdt: float | None = None, qty: float | None = None,
                     order_type: OrderType = "market") -> Fill
    def simulate_sell(self, price: float, qty: float, *, order_type: OrderType = "market") -> Fill
    def fill_from_exchange(self, raw: dict, *, side: Side, base: str, quote: str,
                           request_price: float, order_type: OrderType = "market") -> Fill
        """ccxt-Order-Dict → Fill. Liest raw['average'|'price'], raw['filled'], raw['fee']['cost'|'currency'].
        fee currency == base → fee_base, == quote → fee_quote, sonst ValueError (nie raten)."""
    @staticmethod
    def realized_pnl(*, qty_sold: float, avg_entry_net: float, sell: Fill) -> float
        """= sell.quote_net − qty_sold · avg_entry_net. Damit gilt konstruktiv: Σpnl == ΔCash − ΔKostenbasis."""
```

## 3. Semantik (das ist der Kern — bitte exakt so)

**Kauf um `usdt`** (Gate spot): `fill_price = price·(1+slip)` · `qty_gross = usdt / fill_price` · `fee_base = qty_gross·fee` · `qty_net = qty_gross − fee_base` · `quote_net = usdt` (exakt, das ist was vom Konto geht) · `fee_quote = 0`.

**Kauf um `qty` (netto gewünscht):** `qty_gross = qty / (1−fee)` · `usdt = qty_gross·fill_price` · Rest wie oben.

**Verkauf von `qty`:** `fill_price = price·(1−slip)` · `qty_gross = qty_net = qty` · `quote_gross = qty·fill_price` · `fee_quote = quote_gross·fee` · `quote_net = quote_gross − fee_quote` · `fee_base = 0`.

**Kostenbasis einer Position:** `avg_entry_net = Σ quote_net(Käufe) / Σ qty_net(Käufe)`. Liegt **über** `fill_price`, weil die Basis-Fee weniger Coins liefert. Das ist der Wert, gegen den `gain_pct`, Stop-Distanz und `sold_percent` gerechnet werden müssen — nicht `fill_price`.

**Realisierter P&L:** `pnl = sell.quote_net − qty_sold·avg_entry_net`. Kein separater Slippage-Abzug, keine separate Fee — beides steckt bereits in `quote_net` und `avg_entry_net`. **P&L und Cash können damit nicht mehr auseinanderlaufen.**

**`fee_side` konfigurierbar** (für Börsen mit Quote-Fee auf beiden Seiten). Bei `fee_side_buy="quote"`: `qty_net = qty_gross`, `quote_net = usdt + fee`, d. h. der Käufer zahlt Fee zusätzlich in USDT.

**Präzision:** v1 rechnet in Float ohne Börsen-Rundung; `amount_to_precision` bleibt Sache des Adapters. Folge-Task (Phase 2): Rundung ins Modell.

## 4. Config

```json
"costs": {
  "fee_source": "auto",
  "gate": {
    "spot": {"fee_maker_pct": 0.2, "fee_taker_pct": 0.2, "slippage_pct": 0.15,
             "fee_side_buy": "base", "fee_side_sell": "quote"},
    "swap": {"fee_maker_pct": 0.02, "fee_taker_pct": 0.05, "slippage_pct": 0.10,
             "fee_side_buy": "quote", "fee_side_sell": "quote", "funding_source": "exchange_history"}
  },
  "slippage_by_tier": {"volatile": 0.35, "mid": 0.20, "stable": 0.10}
}
```
- `fee_source: "auto"` — Live-Modus mit Credentials: `fetchTradingFees` beim Start, gecacht, `source="exchange"`; sonst Config, `source="config"`. Einmalige INFO-Zeile, welche Quelle gilt. `"config"` erzwingt Config.
- **0,2 % ist der ccxt-verifizierte VIP-0-Wert.** Die 0,1 % aus `phase1-kasse.md §2.1` waren ein unverifizierter Platzhalter. Vor Live gegen das Konto prüfen (VIP-Stufe, GT-Rabatt) — genau dafür `fee_source: auto`.
- `slippage_by_tier` nach `intelligence/volatility_classifier` (`volatile/mid/stable`). Startwerte geschätzt; **Kalibrierung aus `execution.venue` (Spread, Tiefe) ist #307 / Phase 2.**
- **Entfernt:** `slippage_percent` (config.json + `core/config.py:160` + `core/trading_profiles.py:147`), `shorts.fee_rate`, `grid.assumed_fee_pct`. `grid.fee_aware` bleibt als Schalter.

## 5. Migration der Konsumenten

| Stelle | Änderung |
|---|---|
| `hermes/backtester.py`, `hermes/pipeline_backtest.py` | `cm = CostModel.from_config(cfg, symbol=symbol)`. Kauf: `f = cm.simulate_buy(price, usdt=usdt_per_trade)` → `balance −= f.quote_net`, `position.qty += f.qty_net`, Kostenbasis mitführen. Verkauf: `f = cm.simulate_sell(price, qty)` → `balance += f.quote_net`, `pnl = CostModel.realized_pnl(...)`. **`pnl` wird netto** → `hermes/metrics.py` (Win-Rate, Sharpe) rechnet erstmals mit Kosten. |
| `intelligence/strategy_backtest.py` | dito; `trade_pnl` aus `realized_pnl`. |
| `services/portfolio_service.py` | `execute_buy`: `f = cm.simulate_buy(price, usdt=usdt)` → `update_position(..., price=f.quote_net / f.qty_net, amount=f.qty_net)` — der **Netto-Einstandspreis**, damit `average_entry` konstruktiv gleich der Kostenbasis ist (nicht `fill_price`, der liegt um die Basis-Fee zu niedrig); `fill_price` und Fee-Felder nur ins Trade-Record, Record bekommt `fee_base, fee_quote, fee_usdt, slippage_usdt, cost_model`. `execute_sell`: `f = cm.simulate_sell(price, amount)` → `received = f.quote_net`, `pnl = realized_pnl(qty_sold, pos.average_entry, f)`. **Neuer optionaler Parameter `fill: Fill | None`** — wenn der Adapter einen echten Fill liefert, wird der genommen statt simuliert. |
| `execution/gate_adapter.py` | `_extract_fee` **entfällt**. `_execute_buy/_execute_sell`: `f = cm.fill_from_exchange(raw, side=..., base=..., quote=..., request_price=order.price)` → `_sync_local_ledger(..., fill=f)` → `portfolio.execute_*(fill=f)`. `record_live_trade` bekommt die Fill-Felder. (Die Fill-Status-Logik — `filled` fehlt, teilgefüllt — ist #313; hier nur der Kostenpfad.) |
| `strategies/short_math.py`, `short_policy.py` | `fee_rate` → `CostModel.from_config(cfg, market="swap").fee_pct("taker")/100`. Funding bleibt `funding_rate_8h` aus `CostParams`. |
| `strategies/grid.py`, `grid_plan.py`, `grid_limits.py` | `fee_pct` → `cm.fee_pct("limit")` (Grid-Level sind Limit-Orders → Maker). `round_trip` → `cm.round_trip_pct("limit")`. |
| `core/config.py` | `slippage_percent`-Property **löschen**. `costs`-Property → `dict`. |
| Ledger-Records | jedes neue Trade-Record: `"cost_model": COST_MODEL_VERSION`. Alt-Records ohne Feld = legacy (#316 nutzt das). |

## 6. Tests (Abnahme — Grok schreibt sie, Claude prüft die Erwartungswerte)

`tests/unit/test_costs.py`:
1. `test_buy_fee_in_base` — price 100, usdt 1000, fee 0,2 %, slip 0 → `fill_price 100`, `qty_gross 10.0`, `fee_base 0.02`, `qty_net 9.98`, `quote_net 1000.0`, `fee_usdt 2.0`.
2. `test_buy_by_qty` — qty_net 9.98 gewünscht → `qty_gross 10.0`, `quote_net 1000.0`.
3. `test_sell_fee_in_quote` — qty 9.98 @100 → `quote_gross 998.0`, `fee_quote 1.996`, `quote_net 996.004`, `fee_base 0`.
4. `test_round_trip_pnl_equals_cash_delta` — Kauf 1000 → Verkauf alles zum selben Preis: `pnl == quote_net_sell − 1000 == −3.996`; `abs(pnl − (cash_after − cash_before)) < 1e-9`. **Der wichtigste Test.**
5. `test_slippage_adverse` — slip 0,15 % → Kauf `fill 100.15`, Verkauf `fill 99.85`; `slippage_usdt` korrekt.
6. `test_maker_taker` — `fee_pct("limit") == maker`, `("market") == taker`; `round_trip_pct` = 2·fee + 2·slip.
7. `test_from_config_missing_block_uses_vip0_defaults_and_warns`.
8. `test_from_config_reads_tier_slippage` — symbol mit tier `volatile` → 0,35.
9. `test_fill_from_exchange_base_fee` — raw `{"average":100,"filled":10,"fee":{"cost":0.02,"currency":"BTC"}}`, BTC/USDT buy → `fee_base 0.02`, `qty_net 9.98`.
10. `test_fill_from_exchange_quote_fee` — sell, `currency":"USDT"` → `fee_quote`.
11. `test_fill_from_exchange_unknown_currency_raises`.
12. `test_fee_side_quote_on_buy` — Börse mit Quote-Fee → `qty_net == qty_gross`, `quote_net == usdt + fee`.

`tests/test_costs_single_source.py` (aus der Spec): Grep über `core strategies services risk hermes intelligence execution` nach `slippage_percent|fee_rate\b|assumed_fee_pct` → leer außer `core/costs.py`.

`tests/unit/test_portfolio_service_costs.py`: Sequenz Buy→Sell im Sim → `Σ usdt_received − Σ usdt_spent == Σ pnl` (auf 1e-9). Record enthält `cost_model`.

`tests/unit/test_backtester_costs.py`: `CostModel` mit fee 0/slip 0 → Ergebnis identisch zur naiven Rechnung; fee 0,2 % → `pnl` sinkt um erwarteten Betrag; **kein Test ändert bestehende Backtest-Erwartungswerte** — neue Tests, alte bleiben (siehe CLAUDE.md „Tests are the spec"). Existierende Backtest-Tests, die auf Brutto-`pnl` bauen, gehen dadurch möglicherweise rot → **melden, nicht anpassen**; Claude entscheidet pro Test.

## 7. Nicht in diesem Ticket
Fill-Status / Teilfüllung / `filled` fehlt (#313) · Slippage-Kalibrierung aus Venue-Daten (#307) · Hermes-Neubewertung (#316) · Börsen-Rundung im Modell (Phase 2).
