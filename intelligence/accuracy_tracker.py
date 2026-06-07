from datetime import datetime, timedelta

from data_manager import get_config, load_x_accounts, load_x_posts, save_x_accounts, save_x_posts
from logger import log
from price_fetcher import get_prices


class AccuracyTracker:
    """Retroactive evaluation of X recommendations and dynamic trust_score updates."""

    def __init__(self, config: dict = None):
        self.config = config or get_config()
        accuracy_cfg = self.config.get("accuracy", {})
        self.buy_success_pct = accuracy_cfg.get("buy_success_pct", 3.0)
        self.sell_success_pct = accuracy_cfg.get("sell_success_pct", -2.0)
        self.trust_ema_alpha = accuracy_cfg.get("trust_ema_alpha", 0.3)
        self.evaluation_windows = {
            "outcome_4h": 4,
            "outcome_24h": 24,
            "outcome_7d": 168,
        }

    def _parse_ts(self, ts: str) -> datetime:
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00").replace("+00:00", ""))
        except Exception:
            return datetime.now() - timedelta(days=30)

    def _price_return_pct(self, signal_price: float, current_price: float) -> float:
        if not signal_price or signal_price <= 0 or not current_price:
            return 0.0
        return ((current_price / signal_price) - 1) * 100

    def _evaluate_post(self, post: dict, return_pct: float) -> bool:
        action = post.get("parsed_action") or post.get("action", "IGNORE")
        if action == "BUY":
            return return_pct >= self.buy_success_pct
        if action == "SELL":
            return return_pct <= self.sell_success_pct
        return False

    def update_outcomes(self) -> int:
        data = load_x_posts()
        posts = data.get("posts", [])
        updated = 0
        now = datetime.now()

        for post in posts:
            if post.get("coin") in (None, "", "UNKNOWN"):
                continue
            signal_price = post.get("signal_price")
            if not signal_price:
                continue

            post_time = self._parse_ts(post.get("timestamp", ""))
            age_hours = (now - post_time).total_seconds() / 3600
            symbol = f"{post['coin']}/USDT"
            current_price, _, _ = get_prices(symbol)
            if not current_price:
                continue

            for field, hours in self.evaluation_windows.items():
                if post.get(field) is not None:
                    continue
                if age_hours >= hours:
                    ret = self._price_return_pct(signal_price, current_price)
                    post[field] = round(ret, 2)
                    updated += 1

            if post.get("outcome_24h") is not None and post.get("was_correct") is None:
                post["was_correct"] = self._evaluate_post(post, post["outcome_24h"])
                usdt = self.config.get("max_usdt_per_trade", 150)
                if post.get("parsed_action") == "BUY" and signal_price:
                    amount = usdt / signal_price
                    post["pnl_if_followed"] = round((current_price - signal_price) * amount, 2)
                updated += 1

        if updated:
            save_x_posts(data)
        return updated

    def account_stats(self, account: str, limit: int = 30) -> dict:
        posts = [
            p for p in load_x_posts().get("posts", [])
            if p.get("account") == account and p.get("was_correct") is not None
        ][-limit:]

        if not posts:
            return {"account": account, "samples": 0, "hit_rate": 0.0, "avg_return_24h": 0.0}

        hits = sum(1 for p in posts if p.get("was_correct"))
        returns = [p.get("outcome_24h", 0) for p in posts if p.get("outcome_24h") is not None]
        return {
            "account": account,
            "samples": len(posts),
            "hit_rate": round(hits / len(posts), 3),
            "avg_return_24h": round(sum(returns) / len(returns), 2) if returns else 0.0,
        }

    def update_trust_scores(self) -> int:
        accounts = load_x_accounts()
        updated = 0
        for acc in accounts:
            handle = acc.get("handle", acc)
            stats = self.account_stats(handle)
            if stats["samples"] < 3:
                continue
            computed = stats["hit_rate"] * 100
            old_trust = acc.get("trust_score", 70)
            new_trust = round(
                self.trust_ema_alpha * computed + (1 - self.trust_ema_alpha) * old_trust,
                1,
            )
            new_trust = max(30, min(99, new_trust))
            if new_trust != old_trust:
                acc["trust_score"] = new_trust
                acc["accuracy_samples"] = stats["samples"]
                acc["hit_rate"] = stats["hit_rate"]
                updated += 1

        if updated:
            save_x_accounts(accounts)
            log(f"Updated trust scores for {updated} X accounts", "INFO")
        return updated

    def get_leaderboard(self) -> list:
        accounts = load_x_accounts()
        board = []
        for acc in accounts:
            handle = acc.get("handle", acc)
            stats = self.account_stats(handle)
            board.append({
                "handle": handle,
                "trust_score": acc.get("trust_score", 70),
                "hit_rate": stats["hit_rate"],
                "samples": stats["samples"],
                "avg_return_24h": stats["avg_return_24h"],
                "enabled": acc.get("enabled", True),
            })
        return sorted(board, key=lambda x: (x["trust_score"], x["hit_rate"]), reverse=True)