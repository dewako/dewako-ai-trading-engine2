"""Durable safety state: kill switch, trading state, live-trading switch and the live-order gate.

Principles
- Everything is stored in the database, so a restart cannot clear it.
- Unknown means blocked. If the baseline records are missing or unreadable, trading is blocked.
- Live trading is DISABLED on a fresh install. Enabling needs an ADMIN. After a restart it comes back BLOCKED
  unless startup verification passes.
- Anyone (including automated code) may switch the kill switch ON. Only a human ADMIN can release it.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

from dewako.persistence.audit import AuditLog
from dewako.core import timesource as _timesource
from dewako.persistence.db import Database, now_iso
from dewako.market_data.freshness import CandleFreshnessError, validate_latest_candle_timestamp
from dewako.reconciliation.state import ReconState

log = logging.getLogger("dewako.safety")

NON_HUMAN = {"ai", "strategy", "bot", "system-ai", "selector", "system", "worker", "execution"}


class TradingState(str, Enum):
    SYSTEM_HEALTHY = "SYSTEM_HEALTHY"
    SYSTEM_DEGRADED = "SYSTEM_DEGRADED"
    TRADING_BLOCKED = "TRADING_BLOCKED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE"


class LiveSwitch(str, Enum):
    DISABLED = "DISABLED"
    ENABLED = "ENABLED"
    BLOCKED = "BLOCKED"


class NotAuthorized(Exception):
    pass


class ReadinessError(NotAuthorized):
    """Live trading cannot be switched on because mandatory readiness checks fail or could not be verified."""
    def __init__(self, reasons: list[str]):
        super().__init__("live trading cannot be enabled: " + "; ".join(reasons))
        self.reasons = reasons


@dataclass
class GateResult:
    allowed: bool
    reasons: list[str] = field(default_factory=list)


class SafetyController:
    BASELINE = {"initialized": "1", "live_trading": LiveSwitch.DISABLED.value,
                "trading_blocked": "0", "degraded": "0", "risk_healthy": "1"}

    def __init__(self, db: Database, audit: Optional[AuditLog] = None, reconciliation_max_age: timedelta = timedelta(minutes=10)):
        self.db, self.audit = db, audit or AuditLog(db)
        self.reconciliation_max_age = reconciliation_max_age
        self.alert_handlers: list = []
        self.live_adapters_ok = None  # callable() -> (bool, reason); the execution engine registers itself here
        self.workers_healthy = None   # optional callable() -> (bool, reason); wired by the application lifecycle
        self.vault = None             # optional CredentialVault; lets the live gate prove credentials still decrypt
        self._bootstrap()

    # ---- low level
    def _bootstrap(self) -> None:
        with self.db.tx() as c:
            if c.execute("SELECT 1 FROM safety_state WHERE key='initialized'").fetchone():
                return  # existing install: never overwrite stored safety state
            if c.execute("SELECT 1 FROM safety_state").fetchone():
                return  # partial state without marker: leave it, reads will report unknown and block
            ts = now_iso()
            for k, v in self.BASELINE.items():
                c.execute("INSERT INTO safety_state(key,value,updated_at,updated_by) VALUES (?,?,?,?)", (k, v, ts, "bootstrap"))

    def _get(self, key: str) -> Optional[str]:
        with self.db.read() as c:
            r = c.execute("SELECT value FROM safety_state WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def _set(self, key: str, value: str, by: str, conn=None) -> None:
        sql = ("INSERT INTO safety_state(key,value,updated_at,updated_by) VALUES (?,?,?,?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at, updated_by=excluded.updated_by")
        args = (key, value, now_iso(), by)
        if conn is not None:
            conn.execute(sql, args)
        else:
            with self.db.tx() as c:
                c.execute(sql, args)

    def baseline_intact(self) -> bool:
        try:
            return all(self._get(k) is not None for k in self.BASELINE)
        except Exception:
            return False

    # ---- kill switch
    def kill_switch_active(self) -> bool:
        """True when active OR when the state cannot be determined (fail closed)."""
        try:
            with self.db.read() as c:
                r = c.execute("SELECT enabled FROM kill_switch_events WHERE cleared_at IS NULL AND enabled=1 LIMIT 1").fetchone()
            return r is not None or not self.baseline_intact()
        except Exception:
            return True

    def activate_kill_switch(self, actor: dict, reason: str, correlation_id: str = "") -> None:
        if not reason.strip():
            raise ValueError("a reason is required")
        with self.db.tx() as c:
            if not c.execute("SELECT 1 FROM kill_switch_events WHERE cleared_at IS NULL AND enabled=1").fetchone():
                c.execute("INSERT INTO kill_switch_events(enabled,reason,triggered_by,triggered_at) VALUES (1,?,?,?)",
                          (reason, actor.get("username", "system"), now_iso()))
            self.audit.record("KILL_SWITCH_ENABLED", "OK", correlation_id, actor=actor, resource="kill_switch",
                              metadata={"reason": reason}, conn=c)

    def release_kill_switch(self, actor: dict, correlation_id: str = "") -> None:
        if actor.get("role") != "ADMIN" or str(actor.get("username", "")).lower() in NON_HUMAN:
            self.audit.record("KILL_SWITCH_DISABLED", "DENIED", correlation_id, actor=actor, resource="kill_switch")
            raise NotAuthorized("only a human ADMIN may release the kill switch")
        with self.db.tx() as c:
            n = c.execute("UPDATE kill_switch_events SET cleared_by=?, cleared_at=? WHERE cleared_at IS NULL AND enabled=1",
                          (actor["username"], now_iso())).rowcount
            self.audit.record("KILL_SWITCH_DISABLED", "OK" if n else "NOOP", correlation_id, actor=actor, resource="kill_switch", conn=c)

    def kill_switch_history(self) -> list[dict]:
        with self.db.read() as c:
            return [dict(r) for r in c.execute("SELECT * FROM kill_switch_events ORDER BY id DESC")]

    # ---- trading state
    def set_reconciliation_required(self, required: bool, reason: str = "", by: str = "reconciler", correlation_id: str = "",
                                    scope: str = "orders") -> None:
        """Flag (or, only through the legal state path, clear) a reconciliation scope. Clearing goes
        RECONCILIATION_REQUIRED -> RECONCILING -> HEALTHY; it is never a bare boolean flip."""
        rs = ReconState(self.db)
        cur = rs.current(scope)
        if required:
            if cur != "RECONCILIATION_REQUIRED":
                rs.transition(scope, "RECONCILIATION_REQUIRED", reason or "reconciliation required", by, correlation_id)
                self.audit.record("RECONCILIATION_FAILED", "OK", correlation_id, resource=f"scope:{scope}", metadata={"reason": reason})
                self.alert(f"Reconciliation required ({scope}): {reason}", correlation_id)
        elif cur not in (None, "HEALTHY"):
            if cur != "RECONCILING":
                rs.transition(scope, "RECONCILING", "verification started", by, correlation_id)
            rs.transition(scope, "HEALTHY", reason or "verified", by, correlation_id)
            self.audit.record("RECONCILIATION_RESOLVED", "OK", correlation_id, resource=f"scope:{scope}", metadata={"reason": reason})

    def alert(self, message: str, correlation_id: str = "") -> None:
        """Alert the user. This build records the alert in the audit log and the server log and calls any registered handler;
        e-mail/webhook delivery is NOT implemented."""
        log.error("ALERT: %s", message)
        self.audit.record_safely("ALERT", "RAISED", correlation_id, resource="alert", metadata={"message": message})
        for h in list(self.alert_handlers):
            try:
                h(message)
            except Exception:
                log.error("alert handler failed")

    def set_risk_healthy(self, healthy: bool, by: str = "risk") -> None:
        self._set("risk_healthy", "1" if healthy else "0", by)

    def set_degraded(self, degraded: bool, by: str = "system") -> None:
        self._set("degraded", "1" if degraded else "0", by)

    def trading_state(self) -> TradingState:
        try:
            if not self.baseline_intact():
                return TradingState.TRADING_BLOCKED
            if self.kill_switch_active():
                return TradingState.KILL_SWITCH_ACTIVE
            if ReconState(self.db).blocking():
                return TradingState.RECONCILIATION_REQUIRED
            if self._get("trading_blocked") != "0" or self._get("risk_healthy") != "1":
                return TradingState.TRADING_BLOCKED
            if self._get("degraded") != "0":
                return TradingState.SYSTEM_DEGRADED
            return TradingState.SYSTEM_HEALTHY
        except Exception:
            return TradingState.TRADING_BLOCKED

    # ---- live switch
    def live_switch(self) -> LiveSwitch:
        try:
            v = self._get("live_trading")
            return LiveSwitch(v) if v else LiveSwitch.BLOCKED
        except Exception:
            return LiveSwitch.BLOCKED

    def enable_live(self, actor: dict, correlation_id: str = "") -> None:
        if actor.get("role") != "ADMIN" or str(actor.get("username", "")).lower() in NON_HUMAN:
            self.audit.record("LIVE_ENABLED", "DENIED", correlation_id, actor=actor, resource="live_trading")
            raise NotAuthorized("only an ADMIN may enable live trading")
        ready = self.activation_readiness()
        if not ready.allowed:
            self.audit.record("LIVE_ENABLED", "REFUSED", correlation_id, actor=actor, resource="live_trading", metadata={"reasons": ready.reasons[:10]})
            raise ReadinessError(ready.reasons)
        self._set("live_trading", LiveSwitch.ENABLED.value, actor["username"])
        self.audit.record("LIVE_ENABLED", "OK", correlation_id, actor=actor, resource="live_trading")

    def resolve_incident(self, incident_id: int, actor: dict, resolution: str, correlation_id: str = "") -> None:
        """An incident is closed only by a human ADMIN with a written reason. Nothing is repaired automatically."""
        if actor.get("role") != "ADMIN" or str(actor.get("username", "")).lower() in NON_HUMAN:
            self.audit.record("INCIDENT_RESOLVED", "DENIED", correlation_id, actor=actor, resource=f"incident:{incident_id}")
            raise NotAuthorized("only an ADMIN may resolve incidents")
        if len(resolution.strip()) < 8:
            raise ValueError("a written resolution (at least 8 characters) is required")
        with self.db.tx() as c:
            n = c.execute("UPDATE incidents SET status='RESOLVED', resolved_by=?, resolved_at=?, resolution=? WHERE id=? AND status='OPEN'",
                          (actor["username"], now_iso(), resolution[:500], incident_id)).rowcount
            if n != 1:
                raise KeyError(f"no open incident {incident_id}")
            self.audit.record("INCIDENT_RESOLVED", "OK", correlation_id, actor=actor, resource=f"incident:{incident_id}",
                              metadata={"resolution": resolution[:200]}, conn=c)

    def disable_live(self, actor: dict, correlation_id: str = "") -> None:
        """Turning live OFF is always allowed to any caller. It is the safe direction."""
        self._set("live_trading", LiveSwitch.DISABLED.value, actor.get("username", "system"))
        self.audit.record("LIVE_DISABLED", "OK", correlation_id, actor=actor, resource="live_trading")

    def block_live(self, reason: str, by: str = "system") -> None:
        if self.live_switch() is LiveSwitch.ENABLED:
            self._set("live_trading", LiveSwitch.BLOCKED.value, by)
            self.audit.record("LIVE_DISABLED", "BLOCKED", "", resource="live_trading", metadata={"reason": reason})

    def on_startup(self, verification_ok: bool) -> LiveSwitch:
        """After a restart a previously ENABLED live switch only stays ENABLED if startup verification passed."""
        cur = self.live_switch()
        if cur is LiveSwitch.ENABLED and not verification_ok:
            self._set("live_trading", LiveSwitch.BLOCKED.value, "startup")
            self.audit.record("LIVE_DISABLED", "BLOCKED_AT_STARTUP", "", resource="live_trading")
            return LiveSwitch.BLOCKED
        return cur

    # ---- the gate every live order must pass
    # ---- audit integrity and market-data freshness (inputs to the live gate)
    def set_audit_integrity(self, ok: bool, detail: str = "", by: str = "audit_verifier") -> None:
        self._set("audit_integrity", "OK" if ok else "BROKEN", by)
        if not ok:
            self.alert(f"Audit log integrity check FAILED: {detail}")

    def verify_audit(self, by: str = "audit_verifier") -> dict:
        res = self.audit.verify_chain()
        self.set_audit_integrity(res["ok"], res.get("reason", ""), by)
        if not res["ok"]:
            with self.db.tx() as c:
                if not c.execute("SELECT 1 FROM incidents WHERE status='OPEN' AND kind='AUDIT_CHAIN_BROKEN'").fetchone():
                    c.execute("INSERT INTO incidents(kind,severity,scope,detail,status,created_at) VALUES (?,?,?,?,?,?)",
                              ("AUDIT_CHAIN_BROKEN", "CRITICAL", "audit_log", f"first bad row {res['first_bad_id']}: {res['reason']}"[:500], "OPEN", now_iso()))
        return res

    def mark_market_data_fresh(self, symbol: str, candle_timestamp: datetime, timeframe: str,
                               by: str = "market_data") -> None:
        validate_latest_candle_timestamp(candle_timestamp, timeframe, _timesource.now())
        self._set(f"market_data_candle_at:{symbol}", candle_timestamp.astimezone(timezone.utc).isoformat(), by)
        self._set(f"market_data_timeframe:{symbol}", timeframe, by)

    def _exchange_reasons(self, c, exchange_id: int) -> list[str]:
        """Everything about one exchange that must be true before a live order may go to it. Shared by the per-order gate and by activation."""
        why: list[str] = []
        ex = c.execute("SELECT * FROM exchanges WHERE id=?", (exchange_id,)).fetchone()
        if ex is None:
            why.append("exchange not found")
        else:
            perms = json.loads(ex["permissions"])
            if ex["environment"] != "LIVE":
                why.append("exchange is not a LIVE exchange")
            if not ex["enabled"]:
                why.append("exchange is not enabled")
            if not ex["verified_at"]:
                why.append("exchange key permissions were never verified")
            if perms.get("withdraw", True) or not perms.get("trade") or not perms.get("read"):
                why.append("key permissions must be read=yes, trade=yes, withdraw=no")
            cred = c.execute("SELECT withdrawal_permission FROM credentials WHERE exchange_id=? ORDER BY id DESC LIMIT 1", (exchange_id,)).fetchone()
            if cred is None or cred["withdrawal_permission"]:
                why.append("no verified credential without withdrawal permission")
            scope = f"exchange:{ex['exchange']}"
            st_row = c.execute("SELECT state FROM reconciliation_state WHERE scope=?", (scope,)).fetchone()
            rec = c.execute("SELECT result, finished_at FROM reconciliation_runs WHERE exchange=? ORDER BY id DESC LIMIT 1", (ex["exchange"],)).fetchone()
            if st_row is None or st_row["state"] != "HEALTHY":
                why.append(f"reconciliation state for {ex['exchange']} is {st_row['state'] if st_row else 'unknown'}, not HEALTHY")
            elif rec is None or rec["result"] != "OK":
                why.append("last reconciliation did not succeed")
            else:
                fin = rec["finished_at"]
                fin = datetime.fromisoformat(fin) if isinstance(fin, str) else fin
                if _timesource.now() - fin > self.reconciliation_max_age:
                    why.append("reconciliation is stale")
            if self.vault is not None:
                try:
                    from dewako.exchanges.persistent_manager import PersistentExchangeManager  # noqa: F401
                    blob = c.execute("SELECT encrypted_blob, encryption_key_version FROM credentials WHERE exchange_id=? ORDER BY id DESC LIMIT 1", (exchange_id,)).fetchone()
                    self.vault.decrypt(blob["encrypted_blob"], blob["encryption_key_version"])
                except Exception:
                    why.append("stored credentials cannot be decrypted with the configured keys")
        return why

    def activation_readiness(self) -> GateResult:
        """The authoritative answer to 'may live trading be switched ON right now?'. Evaluated from stored state and the execution engine,
        never from anything the caller supplies. Every control path that turns live on must go through this (enable_live does)."""
        why: list[str] = []
        try:
            st = self.trading_state()
            if st is not TradingState.SYSTEM_HEALTHY:
                why.append(f"trading state is {st.value}")
            ai = self._get("audit_integrity")
            if ai != "OK":
                why.append(f"audit log integrity is {ai or 'UNVERIFIED'}, not OK")
            with self.db.read() as c:
                inc = c.execute("SELECT COUNT(*) FROM incidents WHERE status='OPEN'").fetchone()[0]
                rows = [dict(r) for r in c.execute("SELECT id, exchange, enabled FROM exchanges WHERE environment='LIVE'")]
                if inc:
                    why.append(f"{inc} open incident(s) require investigation")
                enabled = [r for r in rows if r["enabled"]]
                if not enabled:
                    why.append("no LIVE exchange is configured, verified and enabled")
                for r in enabled:
                    why += [f"{r['exchange']}: {w}" for w in self._exchange_reasons(c, r["id"])]
            if not self._get("evidence:TESTNET_VERIFIED"):
                why.append("no operator-recorded TESTNET_VERIFIED evidence: live may not be enabled before testnet verification")
            if self.live_adapters_ok is None:
                why.append("execution engine is not attached: cannot confirm any adapter is verified for live execution")
            else:
                ok, reason = self.live_adapters_ok()
                if not ok:
                    why.append(f"live lock: {reason}")
            if self.workers_healthy is None:
                why.append("workers are not running: no heartbeat, reconciliation or lease renewal")
            else:
                ok, reason = self.workers_healthy()
                if not ok:
                    why.append(f"workers not healthy: {reason}")
        except Exception as e:  # any failure to evaluate is a refusal
            why.append(f"readiness could not be evaluated ({type(e).__name__})")
        return GateResult(not why, why)

    EVIDENCE_KINDS = ("PAPER_VERIFIED", "TESTNET_VERIFIED")

    def record_evidence(self, kind: str, actor: dict, note: str, correlation_id: str = "") -> None:
        """An ADMIN's written attestation that paper / testnet verification was done. It is an attestation, not proof, and nothing
        in the system ever records it automatically (CI passing does not). It never enables live trading by itself."""
        if kind not in self.EVIDENCE_KINDS:
            raise ValueError(f"unknown evidence kind {kind!r}")
        if actor.get("role") != "ADMIN" or str(actor.get("username", "")).lower() in NON_HUMAN:
            self.audit.record("EVIDENCE_RECORDED", "DENIED", correlation_id, actor=actor, resource=kind)
            raise NotAuthorized("only a human ADMIN may record verification evidence")
        if len((note or "").strip()) < 20:
            raise ValueError("evidence needs a written description of what was verified (at least 20 characters)")
        self._set(f"evidence:{kind}", note.strip()[:500], actor["username"])
        self.audit.record("EVIDENCE_RECORDED", "OK", correlation_id, actor=actor, resource=kind, metadata={"note": note.strip()[:200]})

    def readiness_state(self) -> dict:
        """LOCKED / NOT_READY / PAPER_VERIFIED / TESTNET_VERIFIED / LIVE_ELIGIBLE_PENDING_OPERATOR_AUTHORIZATION.
        There is deliberately no state that means 'live is enabled'; turning live on is a separate ADMIN action (enable_live) that
        re-runs activation_readiness() at that moment. Hard faults (kill switch, unhealthy trading state, open incident, audit not OK)
        always report NOT_READY regardless of recorded evidence."""
        try:
            paper, testnet = self._get("evidence:PAPER_VERIFIED"), self._get("evidence:TESTNET_VERIFIED")
            ready = self.activation_readiness()
            st = self.trading_state()
            with self.db.read() as c:
                inc = c.execute("SELECT COUNT(*) FROM incidents WHERE status='OPEN'").fetchone()[0]
            hard = st is not TradingState.SYSTEM_HEALTHY or inc > 0 or self._get("audit_integrity") != "OK"
            if hard:
                state = "NOT_READY"
            elif testnet and ready.allowed:
                state = "LIVE_ELIGIBLE_PENDING_OPERATOR_AUTHORIZATION"
            elif testnet:
                state = "TESTNET_VERIFIED"
            elif paper:
                state = "PAPER_VERIFIED"
            else:
                state = "LOCKED"
            return {"state": state, "evidence": {"PAPER_VERIFIED": paper, "TESTNET_VERIFIED": testnet},
                    "blocking_reasons": ready.reasons, "live_switch": self.live_switch().value}
        except Exception as e:
            return {"state": "NOT_READY", "evidence": {}, "blocking_reasons": [f"readiness could not be evaluated ({type(e).__name__})"],
                    "live_switch": LiveSwitch.BLOCKED.value}

    def live_gate(self, *, exchange_id: int, strategy_id: str, strategy_version: str, symbol: Optional[str] = None) -> GateResult:
        why: list[str] = []
        try:
            ai = self._get("audit_integrity")
            if ai != "OK":
                why.append(f"audit log integrity is {ai or 'UNVERIFIED'}, not OK")
            if symbol is not None:
                ts = self._get(f"market_data_candle_at:{symbol}")
                timeframe = self._get(f"market_data_timeframe:{symbol}")
                if ts is None or timeframe is None:
                    why.append(f"market data for {symbol} was never confirmed fresh")
                else:
                    try:
                        validate_latest_candle_timestamp(datetime.fromisoformat(ts), timeframe, _timesource.now())
                    except CandleFreshnessError as e:
                        label = "stale" if "stale" in str(e) else "invalid or unverified"
                        why.append(f"market data for {symbol} is {label}: {e}")
            if self.live_switch() is not LiveSwitch.ENABLED:
                why.append(f"live trading is {self.live_switch().value}")
            st = self.trading_state()
            if st is not TradingState.SYSTEM_HEALTHY:
                why.append(f"trading state is {st.value}")
            with self.db.read() as c:
                why += self._exchange_reasons(c, exchange_id)
                sv = c.execute("SELECT release_status, enabled FROM strategy_versions WHERE strategy_id=? AND strategy_version=?",
                               (strategy_id, strategy_version)).fetchone()
                if sv is None or sv["release_status"] != "RELEASED" or not sv["enabled"]:
                    why.append("strategy version is not a released, enabled version")
            with self.db.read() as c2:
                inc = c2.execute("SELECT COUNT(*) FROM incidents WHERE status='OPEN'").fetchone()[0]
            if inc:
                why.append(f"{inc} open incident(s) require investigation")
            if self.workers_healthy is None:
                why.append("workers not healthy: no worker supervisor is registered (fail closed)")
            else:
                ok, reason = self.workers_healthy()
                if not ok:
                    why.append(f"workers not healthy: {reason}")
        except Exception as e:  # any failure to verify is a block
            why.append(f"safety state could not be verified ({type(e).__name__})")
        return GateResult(not why, why)
