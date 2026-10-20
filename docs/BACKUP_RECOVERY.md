# Backup, restore and recovery

DEWAKO contains **no backup system**. Backups are the operator's job, using the database's own tools. What DEWAKO provides is a schema that
can be restored and verified.

## What must be backed up
- The trading database (PostgreSQL in production, or the SQLite file named by `DEWAKO_TRADING_DB`).
- The market-data store (`DEWAKO_DB_PATH`) is reproducible from exchanges and is lower priority.
- The encryption key(s) (`ENCRYPTION_KEY` and earlier versions). **Store them separately from the database backup.** A database backup without the key cannot decrypt exchange credentials; a key stored beside the backup defeats the encryption.
- `.env` and the strategy lock file (`DEWAKO_STRATEGY_LOCK`).

## PostgreSQL
```bash
pg_dump --format=custom --file=dewako-$(date +%F).dump "$DATABASE_URL"          # backup
createdb dewako_restore && pg_restore --dbname=dewako_restore dewako-YYYY-MM-DD.dump   # restore into an empty database
```
Use WAL archiving / point-in-time recovery for anything beyond daily dumps. Test the restore regularly; an untested backup is not a backup.

## SQLite
Stop the process, or use `sqlite3 trading.db ".backup 'copy.db'"` (a consistent online copy). Do not copy the file while the process is writing.

## After restoring
1. Start the application. Startup applies pending migrations (advisory-locked on PostgreSQL), verifies the schema, loads the **durable kill switch and live switch as stored**, verifies strategy fingerprints, and recovers unresolved orders.
2. A restore is older than the exchange. Any order or fill after the backup point is unknown to DEWAKO. Startup reconciliation will report this as findings and **block live trading** (balance and position differences are human-only). Do not resolve them without comparing against the exchange.
3. Orders in SUBMITTING/UNKNOWN/RECONCILING are looked up on the exchange by client order id; nothing is resent. Orders the exchange does not know are only retried after the exchange confirms absence.
4. Review `/health/detailed`; live stays BLOCKED until reconciliation is HEALTHY and an ADMIN re-enables live.

## Migrations
Versions are recorded in `schema_version`. 1 `core`, 2 `bots_snapshots_reconciliation_health`, 3 `exchange_account_identity`, 4 `risk_reservations_and_incidents`, 5 `audit_hash_chain_and_worker_leases`. They only move forward; take a backup before upgrading. There is no automatic downgrade.

## Credential key rotation
`CredentialVault` supports key versions; rotate then re-encrypt (see `CREDENTIAL_SECURITY.md`). Keep old keys until every credential is re-encrypted.

## Audit log
The audit log is hash-chained and append-only (migration 5). After a restore run `SafetyController.verify_audit()` (startup does) and compare `audit_head` in `/health/detailed` with the value you stored off-host. Rows older than migration 5 are "legacy" and unchained. Copy the head hash to a place the database owner cannot edit, otherwise a full rewrite of the chain is undetectable.
## Execution lease
Only one process holds the `execution` lease. After a crash the lease expires after `lease_ttl` (default 30 s) and another process takes over automatically; a graceful shutdown releases it immediately.
