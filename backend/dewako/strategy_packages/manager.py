"""Package validation, lifecycle and registry binding.

Authority: a package can only become a registered (name, version) rule strategy. Its signals travel the same route as every other strategy
(signal engine -> risk -> execution authorization -> engine). It has no handle on credentials, adapters or the engine, because it is
data. It is always `experimental` and never gets a RELEASED row in strategy_versions, so the live gate refuses it. TESTNET_ELIGIBLE is
a label: this build cannot place testnet orders.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from pydantic import ValidationError

from dewako.auth.service import PERMISSIONS
from dewako.persistence.db import Database, IntegrityError, now_iso
from dewako.strategies import base as sbase
from dewako.strategies import library  # noqa: F401  (registers the built-ins so a package cannot reuse their names)
from dewako.backtesting import gate as backtest_gate
from dewako.backtesting.records import RunStore
from dewako.strategies.rule_builder import RuleSpec, build_strategy_class
from dewako.strategies.versions import fingerprint
from dewako.strategy_packages.archive import PackageRejected, open_package, parse_json
from dewako.strategy_packages.schema import ALLOWED_PARAMETER_KEYS, FUTURE_RUNTIMES, SUPPORTED_RUNTIMES, Manifest

STATES = ("IMPORTED", "VALIDATION_FAILED", "VALIDATED", "BACKTEST_ELIGIBLE", "PAPER_ELIGIBLE", "TESTNET_ELIGIBLE", "DISABLED", "QUARANTINED")
LADDER = ("VALIDATED", "BACKTEST_ELIGIBLE", "PAPER_ELIGIBLE", "TESTNET_ELIGIBLE")
REGISTERED_STATES = {"BACKTEST_ELIGIBLE", "PAPER_ELIGIBLE", "TESTNET_ELIGIBLE"}
NEEDS = {"BACKTEST_ELIGIBLE": "backtests", "PAPER_ELIGIBLE": "trade_actions", "TESTNET_ELIGIBLE": "trade_actions", "DISABLED": "trade_actions",
         "VALIDATED": "trade_actions", "QUARANTINED": "kill_switch_trigger"}
RELEASE_NEEDS = "user_management"


class TransitionRefused(Exception):
    pass


def _has(actor: dict, perm: str) -> bool:
    return perm in PERMISSIONS.get(actor.get("role", ""), set())


class PackageManager:
    def __init__(self, db: Database, audit):
        self.db, self.audit = db, audit
        self.runs = RunStore(db, audit)
        self._registered: dict[int, tuple[str, str]] = {}

    # ------------------------------------------------------------------ validation (pure)
    @staticmethod
    def validate(data: bytes) -> dict[str, Any]:
        """Return {sha256, manifest, spec, fingerprint, files}. Raises PackageRejected. Never executes or writes anything."""
        pkg = open_package(data)
        raw_manifest = parse_json(pkg.files["manifest.json"], "manifest.json")
        if isinstance(raw_manifest, dict) and isinstance(raw_manifest.get("runtime"), str):
            rt = raw_manifest["runtime"]
            if rt not in SUPPORTED_RUNTIMES:
                raise PackageRejected("UNSUPPORTED_RUNTIME", f"{rt}: {FUTURE_RUNTIMES.get(rt, 'unknown runtime')}")
        try:
            m = Manifest.model_validate(raw_manifest)
        except ValidationError as e:
            raise PackageRejected("MALFORMED_MANIFEST", "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())[:400])
        others = {p for p in pkg.files if p != "manifest.json"}
        if set(m.files) != others:
            raise PackageRejected("FILE_LIST_MISMATCH", f"unlisted={sorted(others - set(m.files))} absent={sorted(set(m.files) - others)}")
        for p, h in m.files.items():
            if hashlib.sha256(pkg.files[p]).hexdigest() != h:
                raise PackageRejected("HASH_MISMATCH", p)
        rules = parse_json(pkg.files["strategy/rules.json"], "strategy/rules.json")
        params = parse_json(pkg.files["parameters.json"], "parameters.json")
        meta = parse_json(pkg.files["metadata.json"], "metadata.json")
        if not isinstance(rules, dict) or not isinstance(params, dict) or not isinstance(meta, dict):
            raise PackageRejected("MALFORMED_JSON", "rules, parameters and metadata must be objects")
        bad = set(params) - set(ALLOWED_PARAMETER_KEYS)
        if bad:
            raise PackageRejected("UNSUPPORTED_PARAMETER", str(sorted(bad)))
        if len(meta) > 20 or any(not isinstance(k, str) or not isinstance(v, str) or len(v) > 500 for k, v in meta.items()):
            raise PackageRejected("MALFORMED_METADATA", "up to 20 string values of at most 500 characters")
        for p in others:
            if p.startswith("tests/"):
                parse_json(pkg.files[p], p)               # must parse; never executed
        if rules.get("name", m.name) != m.name or rules.get("version", m.version) != m.version:
            raise PackageRejected("IDENTITY_MISMATCH", "rules.json name/version differ from the manifest")
        merged = {**rules, **params, "name": m.name, "version": m.version}
        merged.setdefault("description", m.description)
        try:
            spec = RuleSpec.model_validate(merged)
        except ValidationError as e:
            raise PackageRejected("INVALID_RULES", "; ".join(x["msg"] for x in e.errors())[:400])
        if m.name in {c.name for c in sbase.all_strategies() if getattr(c, "spec", None) is None}:
            raise PackageRejected("NAME_COLLIDES_WITH_BUILTIN", m.name)
        cls = build_strategy_class(spec, do_register=False)
        cls.supported_markets, cls.supported_timeframes, cls.experimental = list(m.instruments), list(m.timeframes), True
        return {"sha256": pkg.sha256, "manifest": m, "spec": spec, "fingerprint": fingerprint(cls), "files": sorted(pkg.files)}

    # ------------------------------------------------------------------ store
    def _row(self, pid: int) -> dict:
        with self.db.read() as c:
            r = c.execute("SELECT * FROM strategy_packages WHERE id=?", (pid,)).fetchone()
        if r is None:
            raise KeyError(pid)
        return dict(r)

    @staticmethod
    def public(r: dict) -> dict:
        return {k: r[k] for k in ("id", "name", "version", "state", "state_reason", "package_sha256", "fingerprint", "imported_by", "created_at", "updated_at")} | {
            "findings": json.loads(r["findings"] or "[]")}

    def list(self) -> list[dict]:
        with self.db.read() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM strategy_packages ORDER BY id DESC").fetchall()]
        return [self.public(r) for r in rows]

    def get(self, pid: int) -> dict:
        r = self._row(pid)
        out = self.public(r)
        try:
            m = Manifest.model_validate_json(r["manifest"]) if r["manifest"] else None
        except Exception:
            m = None
        out["summary"] = None if m is None else {"runtime": m.runtime, "instruments": m.instruments, "timeframes": m.timeframes, "description": m.description,
                                                 "author": m.author, "file_count": len(m.files) + 1, "schema_version": m.schema_version}
        return out

    def import_package(self, data: bytes, actor: dict, correlation_id: str = "") -> dict:
        if not _has(actor, "strategies"):
            raise TransitionRefused("not permitted to import strategy packages")
        sha = hashlib.sha256(data).hexdigest() if isinstance(data, (bytes, bytearray)) else ""
        with self.db.read() as c:
            same = c.execute("SELECT id FROM strategy_packages WHERE package_sha256=? ORDER BY id DESC", (sha,)).fetchone()
        if same:
            self.audit.record("STRATEGY_PACKAGE_IMPORT", "DUPLICATE", correlation_id, actor=actor, resource=f"package:{same['id']}", metadata={"sha256": sha})
            raise PackageRejected("DUPLICATE_PACKAGE", f"already imported as package {same['id']}")
        ts = now_iso()
        try:
            v = self.validate(data)
        except PackageRejected as e:
            with self.db.tx() as c:
                pid = c.execute("INSERT INTO strategy_packages(package_sha256,state,state_reason,findings,imported_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                                (sha, "VALIDATION_FAILED", e.code, json.dumps([{"code": e.code, "detail": e.detail[:300]}]), actor.get("username", ""), ts, ts)).lastrowid
                self.audit.record("STRATEGY_PACKAGE_IMPORT", "REJECTED", correlation_id, actor=actor, resource=f"package:{pid}", metadata={"code": e.code, "sha256": sha}, conn=c)
            raise
        m = v["manifest"]
        with self.db.read() as c:
            clash = c.execute("SELECT id FROM strategy_packages WHERE name=? AND version=? AND state<>'VALIDATION_FAILED'", (m.name, m.version)).fetchone()
        if clash or (m.name, m.version) in sbase._REGISTRY:
            self.audit.record("STRATEGY_PACKAGE_IMPORT", "VERSION_CONFLICT", correlation_id, actor=actor, metadata={"name": m.name, "version": m.version})
            raise PackageRejected("VERSION_CONFLICT", f"{m.name} v{m.version} already exists; versions are immutable, publish a new version number")
        try:
            with self.db.tx() as c:          # one transaction: the row, its audit records and its VALIDATED state commit together or not at all
                pid = c.execute("INSERT INTO strategy_packages(name,version,package_sha256,fingerprint,state,state_reason,manifest,spec,findings,imported_by,created_at,updated_at) "
                                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                (m.name, m.version, v["sha256"], v["fingerprint"], "IMPORTED", "", m.model_dump_json(), v["spec"].model_dump_json(), "[]",
                                 actor.get("username", ""), ts, ts)).lastrowid
                self.audit.record("STRATEGY_PACKAGE_IMPORT", "OK", correlation_id, actor=actor, resource=f"package:{pid}",
                                  metadata={"name": m.name, "version": m.version, "sha256": v["sha256"], "fingerprint": v["fingerprint"]}, conn=c)
                self._set_state(pid, "VALIDATED", "validated: manifest, hashes, rules", actor, correlation_id, conn=c)
        except IntegrityError:
            raise PackageRejected("VERSION_CONFLICT", f"{m.name} v{m.version}")
        return self.get(pid)

    # ------------------------------------------------------------------ lifecycle
    def _set_state(self, pid: int, to: str, reason: str, actor: dict, cid: str, frm: Optional[str] = None, conn=None) -> None:
        def go(c):
            row = c.execute("SELECT state, name, version, fingerprint FROM strategy_packages WHERE id=?", (pid,)).fetchone()
            cur = row["state"]
            c.execute("UPDATE strategy_packages SET state=?, state_reason=?, updated_at=? WHERE id=?", (to, reason[:300], now_iso(), pid))
            self.audit.record("STRATEGY_PACKAGE_STATE", "OK", cid, actor=actor, resource=f"package:{pid}",
                              metadata={"from": cur, "to": to, "reason": reason[:300], "name": row["name"], "version": row["version"], "fingerprint": row["fingerprint"]}, conn=c)
        if conn is not None:
            go(conn)
        else:
            with self.db.tx() as c:
                go(c)

    def _verify_integrity(self, r: dict) -> Optional[str]:
        """Recompute the fingerprint from the stored spec; None if intact."""
        try:
            m = Manifest.model_validate_json(r["manifest"])
            spec = RuleSpec.model_validate_json(r["spec"])
            cls = build_strategy_class(spec, do_register=False)
            cls.supported_markets, cls.supported_timeframes, cls.experimental = list(m.instruments), list(m.timeframes), True
            return None if fingerprint(cls) == r["fingerprint"] else "fingerprint mismatch: stored spec changed after import"
        except Exception as e:
            return f"stored package unreadable ({type(e).__name__})"

    def transition(self, pid: int, to: str, actor: dict, reason: str = "", correlation_id: str = "") -> dict:
        if to not in STATES or to in ("IMPORTED", "VALIDATION_FAILED"):
            raise TransitionRefused(f"cannot move to {to}")
        r = self._row(pid)
        cur = r["state"]
        if cur in ("VALIDATION_FAILED", "IMPORTED"):
            raise TransitionRefused(f"a {cur} package cannot be promoted; fix it and import a new version")
        need = RELEASE_NEEDS if cur == "QUARANTINED" else NEEDS[to]
        if not _has(actor, need):
            self.audit.record("STRATEGY_PACKAGE_STATE", "DENIED", correlation_id, actor=actor, resource=f"package:{pid}", metadata={"from": cur, "to": to})
            raise TransitionRefused(f"requires permission {need}")
        if cur == "QUARANTINED" and to != "DISABLED":
            raise TransitionRefused("a quarantined package is released to DISABLED first, then re-validated")
        if to == "QUARANTINED":
            pass
        elif to == "DISABLED":
            pass
        else:
            if cur in ("DISABLED",) and to != "VALIDATED":
                raise TransitionRefused("a disabled package is re-validated first")
            if to in LADDER and cur in LADDER and LADDER.index(to) > LADDER.index(cur) + 1:
                raise TransitionRefused("states are promoted one step at a time")
            if cur not in LADDER and cur != "DISABLED":
                raise TransitionRefused(f"cannot move from {cur} to {to}")
            bad = self._verify_integrity(r)
            if bad:
                self._quarantine(pid, bad, actor, correlation_id)
                raise TransitionRefused(f"quarantined: {bad}")
            if to in ("PAPER_ELIGIBLE", "TESTNET_ELIGIBLE") and cur in LADDER and LADDER.index(to) > LADDER.index(cur):
                g = backtest_gate.check(self.runs, r["name"], r["version"], r["fingerprint"])        # demotions are never gated
                if not g.ok:
                    self.audit.record("STRATEGY_PACKAGE_STATE", "DENIED", correlation_id, actor=actor, resource=f"package:{pid}",
                                      metadata={"from": cur, "to": to, "why": "no passing backtest evidence", "reasons": g.reasons, "run_id": g.run_id})
                    raise TransitionRefused("promotion needs passing backtest evidence: " + "; ".join(g.reasons))
        self._set_state(pid, to, reason or f"{cur} -> {to}", actor, correlation_id)
        self._sync_one(self._row(pid))
        return self.get(pid)

    def review_existing(self, actor: dict, correlation_id: str = "") -> list[dict]:
        """Apply the evidence gate to packages that were promoted BEFORE it existed. A package at PAPER_ELIGIBLE or TESTNET_ELIGIBLE
        without passing evidence is demoted to BACKTEST_ELIGIBLE (never anywhere else), each one audited with the reasons.
        Human-triggered only: it needs the same permission as a promotion and acts on nothing by itself."""
        if not _has(actor, NEEDS["PAPER_ELIGIBLE"]):
            raise TransitionRefused(f"requires permission {NEEDS['PAPER_ELIGIBLE']}")
        out = []
        for p in self.list():
            if p["state"] not in ("PAPER_ELIGIBLE", "TESTNET_ELIGIBLE"):
                continue
            g = backtest_gate.check(self.runs, p["name"], p["version"], p["fingerprint"])
            if g.ok:
                continue
            self._set_state(p["id"], "BACKTEST_ELIGIBLE", "no passing backtest evidence: " + "; ".join(g.reasons)[:200], actor, correlation_id)
            self._sync_one(self._row(p["id"]))
            out.append({"id": p["id"], "name": p["name"], "version": p["version"], "from": p["state"], "reasons": g.reasons})
        return out

    def _quarantine(self, pid: int, why: str, actor: dict, cid: str) -> None:
        self._set_state(pid, "QUARANTINED", why, actor, cid)
        r = self._row(pid)
        self.audit.record("STRATEGY_PACKAGE_QUARANTINE", "QUARANTINED", cid, actor=actor, resource=f"package:{pid}",
                          metadata={"why": why, "name": r["name"], "version": r["version"]})
        self._unregister(pid)

    # ------------------------------------------------------------------ registry binding
    def _unregister(self, pid: int) -> None:
        key = self._registered.pop(pid, None)
        if key:
            sbase._REGISTRY.pop(key, None)

    def _sync_one(self, r: dict) -> Optional[str]:
        pid = r["id"]
        if r["state"] not in REGISTERED_STATES:
            self._unregister(pid)
            return None
        if pid in self._registered:
            return None
        bad = self._verify_integrity(r)
        if bad:
            self._quarantine(pid, bad, {"username": "system", "role": "SYSTEM"}, "")
            return bad
        m = Manifest.model_validate_json(r["manifest"])
        spec = RuleSpec.model_validate_json(r["spec"])
        if (r["name"], r["version"]) in sbase._REGISTRY:
            self._quarantine(pid, "name/version already registered by other code", {"username": "system", "role": "SYSTEM"}, "")
            return "conflict"
        cls = build_strategy_class(spec, do_register=False)
        cls.supported_markets, cls.supported_timeframes, cls.experimental = list(m.instruments), list(m.timeframes), True
        sbase.register(cls)
        self._registered[pid] = (r["name"], r["version"])
        return None

    def sync_registry(self) -> list[str]:
        """Startup/restart: re-register eligible packages after verifying fingerprints. Returns problems (tampered ones are quarantined)."""
        with self.db.read() as c:
            rows = [dict(x) for x in c.execute("SELECT * FROM strategy_packages ORDER BY id").fetchall()]
        return [f"package {r['id']}: {p}" for r in rows if (p := self._sync_one(r))]
