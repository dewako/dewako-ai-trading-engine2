"""Paper futures account: long and short, selectable leverage, ISOLATED or CROSS margin, funding, liquidation.

PAPER ONLY. A deterministic accounting model with no network, no credentials and no exchange adapter. It is deliberately
conservative where it simplifies (each simplification is listed in docs/FUTURES_PAPER_STAGE.md):
  * one-way mode: one net position per symbol; an order may reduce or close a position but never flip it;
  * leverage and margin mode can only be changed while the symbol has no open position;
  * a liquidated ISOLATED position loses its whole isolated margin; a CROSS liquidation closes every cross position and loses
    the whole cross wallet (a real exchange may return a remainder; this model never does);
  * maintenance margin uses one rate for the whole position, chosen by its notional (real exchanges use per-symbol tables).
Money conservation is checkable at any time: initial + sum(ledger amounts) == wallet + sum(isolated margins) (see `check()`).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Optional

HARD_LEVERAGE_CEILING = 20          # no configuration can raise the cap above this


class MarginMode(str, Enum):
    ISOLATED = "ISOLATED"
    CROSS = "CROSS"


class FuturesError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class FuturesConfig:
    fee_rate: float = 0.001             # the owner's rule: 0.1% per side is always charged (Binance futures taker is 0.05%)
    slippage: float = 0.0005            # market fills at mark +/- 0.05%
    max_leverage: int = 3               # the owner's cap; leverage is selectable from 1 to this
    liquidation_fee_rate: float = 0.005
    min_notional: float = 5.0
    qty_step: float = 0.001
    # (notional up to, maintenance margin rate), ascending; the last bound must be infinity
    maintenance_tiers: tuple = ((50_000.0, 0.005), (250_000.0, 0.01), (1_000_000.0, 0.025), (math.inf, 0.05))

    def __post_init__(self):
        if isinstance(self.max_leverage, bool) or not isinstance(self.max_leverage, int) \
                or not 1 <= self.max_leverage <= HARD_LEVERAGE_CEILING:
            raise FuturesError("BAD_CONFIG", f"max_leverage must be a whole number from 1 to {HARD_LEVERAGE_CEILING}")
        for name in ("fee_rate", "slippage", "liquidation_fee_rate"):
            v = getattr(self, name)
            if not (isinstance(v, (int, float)) and math.isfinite(v) and 0 <= v < 0.1):
                raise FuturesError("BAD_CONFIG", f"{name} must be a rate from 0 to below 0.1")
        if not (self.min_notional > 0 and self.qty_step > 0):
            raise FuturesError("BAD_CONFIG", "min_notional and qty_step must be positive")
        t = self.maintenance_tiers
        if not t or t[-1][0] != math.inf or any(not 0 < r < 1 for _, r in t) or any(a[0] >= b[0] for a, b in zip(t, t[1:])):
            raise FuturesError("BAD_CONFIG", "maintenance_tiers must ascend, end at infinity, and use rates between 0 and 1")

    @staticmethod
    def from_env(env: Mapping[str, str]) -> "FuturesConfig":
        """DEWAKO_FUTURES_MAX_LEVERAGE (default 3). An invalid value refuses; it is never silently replaced by a default."""
        raw = (env.get("DEWAKO_FUTURES_MAX_LEVERAGE") or "").strip()
        if not raw:
            return FuturesConfig()
        if not raw.isdigit():
            raise FuturesError("BAD_CONFIG", f"DEWAKO_FUTURES_MAX_LEVERAGE must be a whole number from 1 to {HARD_LEVERAGE_CEILING}")
        return FuturesConfig(max_leverage=int(raw))

    def mmr(self, notional: float) -> float:
        for bound, rate in self.maintenance_tiers:
            if notional <= bound:
                return rate
        raise AssertionError("unreachable: the last tier is infinite")


@dataclass
class Position:
    symbol: str
    qty: float                 # signed: > 0 long, < 0 short
    entry: float
    mode: MarginMode
    leverage: int
    isolated_margin: float = 0.0     # ISOLATED only; money moved out of the wallet and held by this position

    @property
    def side(self) -> str:
        return "LONG" if self.qty > 0 else "SHORT"


@dataclass(frozen=True)
class Liquidation:
    symbol: str
    side: str
    mode: MarginMode
    qty: float
    entry: float
    mark: float
    loss: float                # what the account lost at the liquidation itself (isolated margin, or the cross wallet share)


@dataclass(frozen=True)
class Fill:
    symbol: str
    side: str                  # BUY / SELL
    qty: float
    price: float
    fee: float
    realized_pnl: float
    reduce: bool


@dataclass
class PaperFuturesAccount:
    initial: float = 10_000.0
    config: FuturesConfig = field(default_factory=FuturesConfig)

    def __post_init__(self):
        if not (isinstance(self.initial, (int, float)) and math.isfinite(self.initial) and self.initial > 0):
            raise FuturesError("BAD_CONFIG", "the starting balance must be positive")
        self.wallet = float(self.initial)        # cross wallet balance (isolated margins are held by their positions)
        self.marks: dict[str, float] = {}
        self.positions: dict[str, Position] = {}
        self.leverage: dict[str, int] = {}       # per symbol; default 1
        self.mode: dict[str, MarginMode] = {}    # per symbol; default ISOLATED
        self.ledger: list[tuple[str, str, float]] = []      # (kind, symbol, amount): every change to the account's money
        self.liquidations: list[Liquidation] = []

    # ------------------------------------------------------------------------------------------------ settings
    def set_leverage(self, symbol: str, leverage: int) -> None:
        if isinstance(leverage, bool) or not isinstance(leverage, int) or not 1 <= leverage <= self.config.max_leverage:
            raise FuturesError("LEVERAGE_NOT_ALLOWED", f"leverage must be a whole number from 1 to {self.config.max_leverage}")
        if symbol in self.positions:
            raise FuturesError("POSITION_OPEN", "close the position before changing its leverage")
        self.leverage[symbol] = leverage

    def set_margin_mode(self, symbol: str, mode: MarginMode) -> None:
        try:
            mode = MarginMode(mode)
        except ValueError:
            raise FuturesError("BAD_MODE", "margin mode must be ISOLATED or CROSS") from None
        if symbol in self.positions:
            raise FuturesError("POSITION_OPEN", "close the position before changing its margin mode")
        self.mode[symbol] = mode

    # ------------------------------------------------------------------------------------------------ read
    def _mark(self, symbol: str) -> float:
        if symbol not in self.marks:
            raise FuturesError("NO_PRICE", f"no mark price for {symbol}")
        return self.marks[symbol]

    def upnl(self, p: Position) -> float:
        return p.qty * (self._mark(p.symbol) - p.entry)

    def _maint(self, p: Position) -> float:
        n = abs(p.qty) * self._mark(p.symbol)
        return n * self.config.mmr(n)

    def _cross(self) -> list[Position]:
        return [p for p in self.positions.values() if p.mode is MarginMode.CROSS]

    def cross_equity(self) -> float:
        return self.wallet + sum(self.upnl(p) for p in self._cross())

    def available(self) -> float:
        """What a new position may use: the wallet, less losses and the initial margin of open cross positions. Unrealized
        profit is NOT spendable (more conservative than an exchange)."""
        cross = self._cross()
        used = sum(abs(p.qty) * self._mark(p.symbol) / p.leverage for p in cross)
        return self.wallet + min(0.0, sum(self.upnl(p) for p in cross)) - used

    def equity(self) -> float:
        return self.wallet + sum((p.isolated_margin if p.mode is MarginMode.ISOLATED else 0.0) + self.upnl(p)
                                 for p in self.positions.values())

    def liquidation_price(self, symbol: str) -> Optional[float]:
        """The mark price at which this position is liquidated, other prices unchanged. None = this symbol alone cannot do it."""
        p = self.positions.get(symbol)
        if p is None:
            return None
        mmr = self.config.mmr(abs(p.qty) * self._mark(symbol))
        if p.mode is MarginMode.ISOLATED:
            buffer = p.isolated_margin
        else:
            others = [o for o in self._cross() if o.symbol != symbol]
            buffer = self.wallet + sum(self.upnl(o) for o in others) - sum(self._maint(o) for o in others)
        price = (p.qty * p.entry - buffer) / (p.qty - mmr * abs(p.qty))
        return price if price > 0 else None

    def check(self) -> None:
        """Money conservation. Raises if the account's money is not exactly explained by the ledger."""
        have = self.wallet + sum(p.isolated_margin for p in self.positions.values() if p.mode is MarginMode.ISOLATED)
        want = self.initial + sum(a for _, _, a in self.ledger)
        if not math.isclose(have, want, rel_tol=1e-9, abs_tol=1e-6):
            raise AssertionError(f"futures ledger does not balance: account holds {have}, ledger explains {want}")

    # ------------------------------------------------------------------------------------------------ prices, funding
    def set_mark(self, symbol: str, price: float) -> list[Liquidation]:
        if not (isinstance(price, (int, float)) and math.isfinite(price) and price > 0):
            raise FuturesError("BAD_PRICE", "the mark price must be a positive number")
        self.marks[symbol] = float(price)
        return self._liquidate()

    def apply_funding(self, symbol: str, rate: float) -> list[Liquidation]:
        """One funding payment at the current mark. A positive rate: longs pay, shorts receive."""
        if not (isinstance(rate, (int, float)) and math.isfinite(rate) and abs(rate) < 0.05):
            raise FuturesError("BAD_FUNDING", "the funding rate must be a number between -5% and 5%")
        p = self.positions.get(symbol)
        if p is None:
            return []
        pay = p.qty * self._mark(symbol) * rate
        if p.mode is MarginMode.ISOLATED:
            p.isolated_margin -= pay
        else:
            self.wallet -= pay
        self.ledger.append(("FUNDING", symbol, -pay))
        return self._liquidate()

    def _liquidate(self) -> list[Liquidation]:
        out: list[Liquidation] = []
        for p in [p for p in self.positions.values() if p.mode is MarginMode.ISOLATED]:
            if p.symbol in self.marks and p.isolated_margin + self.upnl(p) <= self._maint(p):
                out.append(Liquidation(p.symbol, p.side, p.mode, abs(p.qty), p.entry, self.marks[p.symbol], p.isolated_margin))
                self.ledger.append(("LIQUIDATION", p.symbol, -p.isolated_margin))
                del self.positions[p.symbol]
        cross = self._cross()
        if cross and all(p.symbol in self.marks for p in cross) and self.cross_equity() <= sum(self._maint(p) for p in cross):
            total_n = sum(abs(p.qty) * self.marks[p.symbol] for p in cross)
            for p in cross:
                share = self.wallet * abs(p.qty) * self.marks[p.symbol] / total_n
                out.append(Liquidation(p.symbol, p.side, p.mode, abs(p.qty), p.entry, self.marks[p.symbol], share))
                del self.positions[p.symbol]
            self.ledger.append(("LIQUIDATION", "*CROSS*", -self.wallet))
            self.wallet = 0.0
        self.liquidations.extend(out)
        return out

    # ------------------------------------------------------------------------------------------------ orders
    def market_order(self, symbol: str, side: str, qty: float, reduce_only: bool = False) -> Fill:
        if side not in ("BUY", "SELL"):
            raise FuturesError("BAD_SIDE", "side must be BUY or SELL")
        if not (isinstance(qty, (int, float)) and math.isfinite(qty) and qty > 0):
            raise FuturesError("BAD_QTY", "quantity must be a positive number")
        step = self.config.qty_step
        qty = math.floor(qty / step + 1e-9) * step
        mark = self._mark(symbol)
        sign = 1.0 if side == "BUY" else -1.0
        price = mark * (1 + sign * self.config.slippage)
        p = self.positions.get(symbol)
        reducing = p is not None and (p.qty > 0) != (sign > 0)
        if reduce_only and not reducing:
            raise FuturesError("REDUCE_ONLY", "a reduce-only order needs an open position on the other side")
        if reducing:
            if qty > abs(p.qty) + step / 2:
                if not reduce_only:
                    raise FuturesError("WOULD_FLIP", "the order is larger than the position; close it first (positions never flip)")
                qty = abs(p.qty)
            qty = min(qty, abs(p.qty))
            if qty <= 0:
                raise FuturesError("BAD_QTY", f"quantity is below the step {step}")
            return self._reduce(p, side, qty, price)
        if qty <= 0:
            raise FuturesError("BAD_QTY", f"quantity is below the step {step}")
        return self._open(symbol, side, sign, qty, price, p)

    def _open(self, symbol, side, sign, qty, price, p: Optional[Position]) -> Fill:
        notional = qty * price
        if notional < self.config.min_notional:
            raise FuturesError("MIN_NOTIONAL", f"order value {notional:.2f} is below the minimum {self.config.min_notional}")
        lev = p.leverage if p else self.leverage.get(symbol, 1)
        mode = p.mode if p else self.mode.get(symbol, MarginMode.ISOLATED)
        margin, fee = notional / lev, notional * self.config.fee_rate
        if margin + fee > self.available() + 1e-9:
            raise FuturesError("INSUFFICIENT_MARGIN", f"needs {margin + fee:.2f} (margin {margin:.2f} + fee {fee:.2f}), "
                                                      f"available {self.available():.2f}")
        self.wallet -= fee
        self.ledger.append(("FEE", symbol, -fee))
        if p is None:
            p = self.positions[symbol] = Position(symbol, 0.0, price, mode, lev)
        p.entry = (abs(p.qty) * p.entry + qty * price) / (abs(p.qty) + qty)
        p.qty += sign * qty
        if mode is MarginMode.ISOLATED:
            self.wallet -= margin
            p.isolated_margin += margin
        self._liquidate()         # an over-leveraged entry can be liquidatable at once (fee and slippage included)
        return Fill(symbol, side, qty, price, fee, 0.0, False)

    def _reduce(self, p: Position, side, qty, price) -> Fill:
        direction = 1.0 if p.qty > 0 else -1.0
        pnl = direction * qty * (price - p.entry)
        fee = qty * price * self.config.fee_rate
        frac = qty / abs(p.qty)
        self.ledger += [("REALIZED_PNL", p.symbol, pnl), ("FEE", p.symbol, -fee)]
        if p.mode is MarginMode.ISOLATED:
            released = p.isolated_margin * frac
            p.isolated_margin -= released
            credit = released + pnl - fee
            if credit < 0:          # the loss can never exceed the isolated margin
                self.ledger.append(("SHORTFALL_ABSORBED", p.symbol, -credit))
                credit = 0.0
            self.wallet += credit
        else:
            self.wallet += pnl - fee
        p.qty -= direction * qty
        if abs(p.qty) < self.config.qty_step / 2:
            if p.mode is MarginMode.ISOLATED and p.isolated_margin:       # rounding dust
                self.wallet += p.isolated_margin
            del self.positions[p.symbol]
            if self.wallet < 0 and not self._cross():                     # nothing left to back a negative cross wallet
                self.ledger.append(("SHORTFALL_ABSORBED", p.symbol, -self.wallet))
                self.wallet = 0.0
        return Fill(p.symbol, side, qty, price, fee, pnl, True)

    def close(self, symbol: str) -> Fill:
        p = self.positions.get(symbol)
        if p is None:
            raise FuturesError("NO_POSITION", f"no open position in {symbol}")
        return self.market_order(symbol, "SELL" if p.qty > 0 else "BUY", abs(p.qty), reduce_only=True)


# ---------------------------------------------------------------------------------------------------- entry rules
def size_for_risk(equity: float, risk_pct: float, entry: float, stop: float) -> float:
    """Quantity that loses `risk_pct` of equity if the stop is hit (before fees). Leverage never changes this number: leverage
    decides how much margin the position ties up, not how much it may lose."""
    if not (equity > 0 and 0 < risk_pct <= 0.05 and entry > 0 and stop > 0 and entry != stop):
        raise FuturesError("BAD_RISK", "equity, entry and stop must be positive, entry != stop, and risk at most 5% of equity")
    return equity * risk_pct / abs(entry - stop)


def entry_guard(side: str, entry: float, stop: float, liquidation_price: Optional[float], leverage: int, max_leverage: int,
                min_liq_to_stop: float = 2.0) -> list[str]:
    """Reasons to refuse an entry ([] = allowed). The stop must sit on the losing side, and the liquidation price must be at
    least `min_liq_to_stop` times further from the entry than the stop, so the stop is reached long before liquidation."""
    why = []
    if side not in ("LONG", "SHORT"):
        return ["side must be LONG or SHORT"]
    if not 1 <= leverage <= max_leverage:
        why.append(f"leverage {leverage} is above the cap {max_leverage}")
    if not (entry > 0 and stop > 0) or (stop >= entry if side == "LONG" else stop <= entry):
        why.append("the stop is not on the losing side of the entry")
        return why
    if liquidation_price is not None:
        liq_dist = entry - liquidation_price if side == "LONG" else liquidation_price - entry
        if liq_dist < min_liq_to_stop * abs(entry - stop):
            why.append(f"liquidation is {liq_dist:.2f} from the entry, less than {min_liq_to_stop} x the stop distance "
                       f"{abs(entry - stop):.2f}")
    return why
