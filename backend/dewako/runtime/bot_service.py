"""Bot management: create / enable / disable / list. Stage 1 supports PAPER bots only; a LIVE bot cannot be created (the live lock is not touched).

Creating a bot validates everything that can be checked up front (strategy exists and is released, symbol and timeframe are supported
by the strategy, the exchange adapter exists and is a PAPER adapter) and initialises the durable risk scope for the bot. A bot is created
disabled; enabling is a separate, audited step.
"""
from __future__ import annotations

import re
import uuid
from typing import Optional

from dewako.persistence.db import now_iso
from dewako.risk.state import RiskStateStore
from dewako.strategies import base

_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_RELEASED = ("RELEASED",)


class BotError(ValueError):
    """A refusal that is safe to show to the caller."""


class BotService:
    def __init__(self, db, adapters: dict, risk_state: RiskStateStore, audit):
        self.db, self.adapters, self.risk_state, self.audit = db, adapters, risk_state, audit

    async def create(self, *, bot_id: str, strategy: str, version: str, symbol: str, timeframe: str, environment: str, exchange: str,
                     actor: Optional[dict] = None) -> dict:
        cid = uuid.uuid4().hex
        if not _ID.match(bot_id or ""):
            raise BotError("bot_id must be 1-40 characters of letters, digits, '_' or '-'")
        if environment != "PAPER":
            raise BotError("only PAPER bots can be created in this build; live trading remains locked")
        try:
            cls = base.get(strategy, version)
        except KeyError:
            raise BotError(f"unknown strategy {strategy} v{version}") from None
        if symbol not in cls.supported_markets:
            raise BotError(f"{strategy} v{version} does not support {symbol} (supported: {', '.join(cls.supported_markets)})")
        if timeframe not in cls.supported_timeframes:
            raise BotError(f"{strategy} v{version} does not support timeframe {timeframe} (supported: {', '.join(cls.supported_timeframes)})")
        adapter = self.adapters.get(exchange)
        if adapter is None:
            raise BotError(f"no adapter is configured for exchange {exchange!r}")
        if adapter.environment != environment:
            raise BotError(f"adapter {exchange} is a {adapter.environment} adapter, not {environment}")
        with self.db.read() as c:
            sv = c.execute("SELECT release_status, enabled FROM strategy_versions WHERE strategy_id=? AND strategy_version=?", (strategy, version)).fetchone()
            dup = c.execute("SELECT 1 FROM bots WHERE bot_id=?", (bot_id,)).fetchone()
        if sv is None or not sv["enabled"] or (sv["release_status"] not in _RELEASED and not cls.experimental):
            raise BotError(f"{strategy} v{version} is not a released, enabled strategy version")
        if dup:
            raise BotError(f"bot {bot_id} already exists")
        quote = symbol.split("/")[1]
        try:
            equity = float((await adapter.get_balance()).get(quote, 0.0))
        except Exception as e:
            raise BotError(f"account balance could not be read ({type(e).__name__}); risk scope not initialised") from None
        if equity <= 0:
            raise BotError(f"no {quote} balance available to size risk against")
        scope = environment
        self.risk_state.init_scope(scope, equity)
        ts = now_iso()
        with self.db.tx() as c:
            c.execute("INSERT INTO bots(bot_id,strategy_id,strategy_version,symbol,timeframe,environment,exchange,risk_scope,enabled,created_at,updated_at) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (bot_id, strategy, version, symbol, timeframe, environment, exchange, scope, 0, ts, ts))
            self.audit.record("BOT_CREATED", "OK", cid, actor=actor, resource=f"bot:{bot_id}",
                              metadata={"strategy": strategy, "version": version, "symbol": symbol, "timeframe": timeframe,
                                        "environment": environment, "exchange": exchange}, conn=c)
        return self.get(bot_id)

    def set_enabled(self, bot_id: str, enabled: bool, actor: Optional[dict] = None) -> dict:
        with self.db.tx() as c:
            n = c.execute("UPDATE bots SET enabled=?, updated_at=? WHERE bot_id=?", (1 if enabled else 0, now_iso(), bot_id)).rowcount
            if not n:
                raise BotError(f"bot {bot_id} not found")
            self.audit.record("BOT_ENABLED" if enabled else "BOT_DISABLED", "OK", uuid.uuid4().hex, actor=actor, resource=f"bot:{bot_id}", conn=c)
        return self.get(bot_id)

    def get(self, bot_id: str) -> dict:
        with self.db.read() as c:
            r = c.execute("SELECT * FROM bots WHERE bot_id=?", (bot_id,)).fetchone()
        if r is None:
            raise BotError(f"bot {bot_id} not found")
        return dict(r)

    def list(self) -> list[dict]:
        with self.db.read() as c:
            return [dict(r) for r in c.execute("SELECT * FROM bots ORDER BY bot_id")]
