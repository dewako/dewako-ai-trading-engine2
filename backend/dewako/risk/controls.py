"""What the risk layer actually enforces right now, and what it cannot. Reported by the API so a control that has no data source is never
presented as active.

A control is ENFORCED only when the input it depends on is being supplied by a running, healthy source. Otherwise it is NOT_ENFORCED with
the reason. No control silently falls back to a safe-looking default.
"""
from __future__ import annotations


def control_status(*, equity_feed_ok: bool, news_feed: bool = False, live_account_feed: bool = False) -> dict[str, dict]:
    def c(ok: bool, why_not: str) -> dict:
        return {"status": "ENFORCED" if ok else "NOT_ENFORCED", "reason": "" if ok else why_not}
    equity_why = "no healthy mark-to-market equity worker is running"
    return {
        "stop_loss_required": c(True, ""),
        "risk_per_trade": c(True, ""),
        "max_position_size": c(True, ""),
        "max_open_positions": c(True, ""),
        "max_exposure": c(True, ""),
        "max_correlated_risk": c(True, ""),     # computed from open positions' stop distance; one correlation group unless configured
        "max_consecutive_losses": c(True, ""),
        "cooldown_after_loss": c(True, ""),
        "min_confidence": c(True, ""),
        "max_daily_loss": c(equity_feed_ok, equity_why),
        "max_drawdown": c(equity_feed_ok, equity_why),
        "high_impact_news_blackout": c(news_feed, "no news/event calendar source is configured (Stage 4); this control does nothing today"),
        "live_realised_pnl": c(live_account_feed, "no LIVE account equity feed exists; live trading is locked"),
        "max_leverage": {"status": "NOT_APPLICABLE", "reason": "spot only; leverage and margin are not implemented"},
    }
