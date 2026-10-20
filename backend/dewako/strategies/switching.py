"""Strategy switching. A change never silently alters how an open position is managed.

If a position is open (or we cannot tell), the caller must choose a policy:
  KEEP              the new strategy takes over managing the existing position
  CLOSE             request a close through the normal pipeline; no new entries until flat
  PREVIOUS_MANAGES  the old strategy keeps managing the position; the new one waits until flat
This module only records the decision. Closing a position still goes through the Risk Engine and order engine.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from dewako.strategies import base

NON_HUMAN_ACTORS = {"ai", "strategy", "bot", "system-ai", "selector"}


class PositionPolicy(str, Enum):
    KEEP = "KEEP"
    CLOSE = "CLOSE"
    PREVIOUS_MANAGES = "PREVIOUS_MANAGES"


class SwitchBlocked(Exception):
    pass


class PolicyRequired(SwitchBlocked):
    pass


@dataclass
class Binding:
    bot_id: str
    strategy: str
    version: str
    mode: str = "PAPER"                                # PAPER or LIVE
    previous: Optional[tuple[str, str]] = None         # (name, version) still managing an open position
    entries_blocked: bool = False                      # True until the old position is gone
    close_requested: bool = False


@dataclass
class SwitchResult:
    binding: Binding
    actions: list[str]


class StrategySwitcher:
    def __init__(self, has_position: Optional[Callable[[str], bool]] = None, audit: Optional[list] = None):
        """`has_position(bot_id)` reports whether the bot holds a position. If it is None, the answer is unknown and
        unknown is treated as "yes" (fail closed), so a policy is always required."""
        self._has_position = has_position
        self.audit = audit if audit is not None else []
        self.bindings: dict[str, Binding] = {}

    def assign(self, bot_id: str, name: str, version: Optional[str] = None, mode: str = "PAPER", actor: str = "admin",
               policy: Optional[PositionPolicy] = None) -> SwitchResult:
        try:
            cls = base.get(name, version) if version else base.latest(name)
        except KeyError:
            raise SwitchBlocked(f"unknown strategy {name} {version or ''}".strip()) from None
        if mode not in ("PAPER", "LIVE"):
            raise SwitchBlocked("mode must be PAPER or LIVE")
        if mode == "LIVE" and cls.experimental:
            raise SwitchBlocked(f"{cls.name} v{cls.version} is experimental and cannot run live")
        current = self.bindings.get(bot_id)
        if current and current.mode == "LIVE" and actor.lower() in NON_HUMAN_ACTORS:
            raise SwitchBlocked("only a human can change the strategy of a live bot")
        held = True if self._has_position is None else self._has_position(bot_id)
        if current and current.mode == "LIVE" and mode != "LIVE" and actor.lower() in NON_HUMAN_ACTORS:
            raise SwitchBlocked("only a human can move a live bot to paper")
        actions: list[str] = []
        b = Binding(bot_id, cls.name, cls.version, mode)
        if current and (current.strategy, current.version) != (cls.name, cls.version) and held:
            if policy is None:
                raise PolicyRequired("existing position detected. Choose: KEEP, CLOSE or PREVIOUS_MANAGES")
            b.entries_blocked = True
            if policy is PositionPolicy.CLOSE:
                b.close_requested = True
                actions.append("REQUEST_CLOSE")
            elif policy is PositionPolicy.PREVIOUS_MANAGES:
                b.previous = (current.strategy, current.version)
                actions.append("PREVIOUS_STRATEGY_MANAGES_POSITION")
            else:
                actions.append("NEW_STRATEGY_MANAGES_POSITION")
        elif current and current.previous and held:  # earlier switch still waiting for the old position to close
            b.previous, b.entries_blocked = current.previous, True
        self.bindings[bot_id] = b
        self.audit.append({"event": "strategy_switch", "bot": bot_id, "actor": actor, "to": f"{cls.name} v{cls.version}",
                           "mode": mode, "policy": policy.value if policy else None, "actions": actions})
        return SwitchResult(b, actions)

    def position_closed(self, bot_id: str) -> None:
        """Call when the bot is flat again: new entries are allowed and the old manager is released."""
        b = self.bindings.get(bot_id)
        if b:
            b.entries_blocked, b.previous, b.close_requested = False, None, False

    def managing_strategy(self, bot_id: str) -> tuple[str, str]:
        """Which strategy manages an open position for this bot right now."""
        b = self.bindings[bot_id]
        return b.previous or (b.strategy, b.version)
