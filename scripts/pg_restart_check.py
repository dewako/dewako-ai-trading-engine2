#!/usr/bin/env python3
"""Manual PostgreSQL-restart recovery check (NOT part of CI: it restarts the server).
Usage: DEWAKO_TEST_PG_URL=... scripts/pg_restart_check.py "<command that restarts the server>"
Verifies: an open transaction at the moment of the restart is NOT committed; the next write after the restart either reconnects or
fails closed with DatabaseUnavailable (never a silent loss); afterwards the audit chain verifies and no row is duplicated."""
import os, subprocess, sys, uuid
sys.path[:0] = [os.path.join(os.path.dirname(__file__), "..", "backend")]
from dewako.persistence.audit import AuditLog
from dewako.persistence.db import Database, DatabaseUnavailable

url = os.environ["DEWAKO_TEST_PG_URL"] + f"?schema=r_{uuid.uuid4().hex[:12]}"
restart = sys.argv[1]
db = Database(url)
log = AuditLog(db)
log.record("PRE", "OK", "c-pre")
with db.tx() as c:
    c.execute("CREATE TABLE restart_probe(x INTEGER)")
cm = db.tx()
inflight = cm.__enter__()                                       # a transaction deliberately left open across the restart
inflight.execute("INSERT INTO restart_probe VALUES (1)")
subprocess.run(restart, shell=True, check=True, capture_output=True)
outcome = "reconnected"
try:
    log.record("POST", "OK", "c-post")
except DatabaseUnavailable:
    outcome = "failed closed (DatabaseUnavailable), then retried"
    log.record("POST", "OK", "c-post")
with db.read() as c:
    probe = c.execute("SELECT COUNT(*) FROM restart_probe").fetchone()[0]
rows = [r["action"] for r in log.recent(50)]
chain = log.verify_chain()
print({"first_write_after_restart": outcome, "actions": sorted(rows), "chain_ok": chain["ok"], "uncommitted_rows_visible_after_restart": probe, "server": db.read().__enter__().execute("SHOW server_version").fetchone()[0]})
assert probe == 0, "an uncommitted transaction survived the restart"
assert rows.count("POST") == 1 and rows.count("PRE") == 1 and chain["ok"]
print("PG RESTART CHECK: PASS")
