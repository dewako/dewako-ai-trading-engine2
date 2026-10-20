"""Process used by test_crash_and_recon.py. Sends one LIVE order through a file-backed fake exchange and is SIGKILLed by the
exchange stub right after the exchange has accepted the order and before DEWAKO can record the answer.
Usage: python crash_worker.py <db_target> <exchange_file> <exchange_id>"""
import json, os, signal, sys

from dewako.core.types import Order
from bounded import hard_kill_self
from hardening_helpers import FakeLive, decision, engine_for, run
from dewako.persistence.db import Database


class FileLive(FakeLive):
    """FakeLive whose exchange-side state lives in a JSON file, so it survives the death of this process."""
    def __init__(self, path):
        super().__init__(); self.path = path
        if os.path.exists(path):
            d = json.load(open(path))
            self.store = {k: Order(**v) for k, v in d["orders"].items()}; self.create_calls = d["create_calls"]

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"orders": {k: v.model_dump(mode="json") for k, v in self.store.items()}, "create_calls": self.create_calls}, f); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, self.path)

    async def create_order(self, req):
        o = await super().create_order(req)
        self._save()
        if os.environ.get("DEWAKO_CRASH") == "1":
            hard_kill_self()          # a real, uncatchable kill: no finally blocks, no atexit, no rollback hooks
        return o


if __name__ == "__main__":
    target, path, eid = sys.argv[1], sys.argv[2], int(sys.argv[3])
    db = Database(target)
    ad = FileLive(path)
    eng, _ = engine_for(db, ad)
    eng.exchange_ids["fake"] = eid
    run(eng.execute(decision("crash1"), bot_id="b", environment="LIVE", exchange="fake"))
    print("survived")      # must not be reached when DEWAKO_CRASH=1
