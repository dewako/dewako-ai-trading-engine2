"""Execution-worker process for the crash matrix (test_crash_matrix.py). Run as:
    python sim_worker.py <db_target> <sim_file> <exchange_id> <command> [args...]
with CRASH_AT=<boundary> in the environment to die (SIGKILL) at that boundary.
The fake exchange keeps its own order/fill state in <sim_file>, independent of the database, so it survives the worker's death.
Nothing here is imported by the application: the crash points are patched in by this test process only."""
import asyncio, json, os, signal, sys

from dewako.core.types import Order, OrderStatus
from dewako.exchanges.base import ExchangeError, InvalidOrder
from dewako.execution.engine import ExecutionEngine
from dewako.persistence.db import Database
from dewako.reconciliation.service import ReconciliationService
from dewako.risk.engine import RiskLimits
from dewako.safety.controller import SafetyController
from bounded import hard_kill_self
from hardening_helpers import FakeLive, TEST_QUOTE_GUARD, decision

CRASH = os.environ.get("CRASH_AT", "")


def die():
    hard_kill_self()       # uncatchable: no finally, no atexit, no rollback hooks (POSIX SIGKILL / Windows TerminateProcess)


class SimExchange(FakeLive):
    def __init__(self, path):
        d = json.load(open(path))
        super().__init__(mode=d["mode"])
        self.path = path
        self.store = {k: Order(**v) for k, v in d["orders"].items()}
        self.create_calls, self.duplicate_attempts = d["create_calls"], d["duplicate_attempts"]
        self.unreachable = d.get("unreachable", False)

    def save(self):
        d = {"mode": self.mode, "orders": {k: v.model_dump(mode="json") for k, v in self.store.items()}, "create_calls": self.create_calls,
             "duplicate_attempts": self.duplicate_attempts, "unreachable": self.unreachable}
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, self.path)

    async def create_order(self, req):
        if req.client_order_id in self.store:                     # a real exchange rejects a repeated client order id
            self.duplicate_attempts += 1; self.save()
            raise InvalidOrder("duplicate client order id")
        if CRASH == "during_submit":
            die()                                                 # the request never reached the exchange
        try:
            o = await FakeLive.create_order(self, req)
        finally:
            self.save()                                           # the exchange's own state persists even when the answer is lost
        if CRASH == "after_exchange_accept":
            die()                                                 # accepted by the exchange, answer never recorded locally
        return o

    async def cancel_order(self, symbol, order_id):
        o = await FakeLive.cancel_order(self, symbol, order_id)
        self.save()
        if CRASH == "during_cancel":
            die()                                                 # cancelled on the exchange, local state not updated
        return o

    async def get_order(self, symbol, order_id=None, client_order_id=None):
        if self.unreachable:
            raise ExchangeError("network timeout")
        return await FakeLive.get_order(self, symbol, order_id, client_order_id)

    async def get_account_snapshot(self):
        if self.unreachable:
            raise ExchangeError("network timeout")
        return await FakeLive.get_account_snapshot(self)

    async def get_positions(self):
        if self.unreachable:
            raise ExchangeError("network timeout")
        return await FakeLive.get_positions(self)


def build(target, sim_path, eid):
    db = Database(target)
    sim = SimExchange(sim_path)
    safety = SafetyController(db)
    lim = RiskLimits(max_position_pct=100, max_exposure_pct=100, max_open_positions=50)
    eng = ExecutionEngine(db, safety, {"fake": sim}, exchange_ids={"fake": eid}, limits=lim, quote_guard=TEST_QUOTE_GUARD)
    rec = ReconciliationService(db, safety, eng)
    if CRASH == "before_intent_commit":
        eng.orders.create = lambda **kw: die()                    # transaction open, nothing committed
    elif CRASH == "after_intent_commit":
        async def s(*a, **k): die()                               # intent + reservation committed, nothing sent
        eng._send = s
    elif CRASH == "after_partial_fill_commit":
        real = eng._apply_exchange_order
        def w(*a, **k):
            r = real(*a, **k); die()                              # the partial fill is committed locally, then the process dies
        eng._apply_exchange_order = w
    elif CRASH == "during_fill_tx":
        real_fill = eng._apply_fill
        def f(*a, **k):
            real_fill(*a, **k); die()                             # trade + position written inside the transaction, never committed
        eng._apply_fill = f
    elif CRASH == "during_reconcile":
        def fin(*a, **k): die()
        rec._finish = fin
    return db, sim, safety, eng, rec


def main():
    target, sim_path, eid, cmd, *args = sys.argv[1:]
    eid = int(eid)
    if cmd == "hang":
        import time; time.sleep(3600)
    if cmd == "startup_db_outage":
        from dewako.runtime.application import Application, Config
        app = Application(Config(db_target=target, workers_enabled=False), adapters={"fake": SimExchange(sim_path)})
        status = asyncio.run(app.startup())
        print("RESULT " + json.dumps({"status": status, "creates": SimExchange(sim_path).create_calls}))
        return
    db, sim, safety, eng, rec = build(target, sim_path, eid)
    if cmd == "exec":
        sid, qty, bot = args
        r = asyncio.run(eng.execute(decision(sid, qty=float(qty)), bot_id=bot, environment="LIVE", exchange="fake"))
        out = {"outcome": r.outcome, "order_id": r.order_id, "state": r.state}
    elif cmd == "recover":
        out = {"results": [(r.outcome, r.state) for r in asyncio.run(eng.recover(created_grace_seconds=0))]}
    elif cmd == "cancel":
        r = asyncio.run(eng.cancel(int(args[0]))); out = {"outcome": r.outcome, "state": r.state}
    elif cmd == "retry":
        r = asyncio.run(eng.retry(int(args[0]))); out = {"outcome": r.outcome, "state": r.state}
    elif cmd == "reconcile":
        r = asyncio.run(rec.run("fake")); out = {"result": r["result"], "state": r["state"], "findings": [f["type"] for f in r["findings"]]}
    else:
        raise SystemExit(f"unknown command {cmd}")
    print("RESULT " + json.dumps(out))


if __name__ == "__main__":
    main()
