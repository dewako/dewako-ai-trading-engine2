# Reconciliation

`reconciliation/service.py`. Read-only: it never cancels orders (exchange-side protective orders stay), never closes positions, never trades.

Each run: settles our own unresolved orders by asking for their client ids, then compares exchange vs database for open orders and positions. Findings: `missing_order`, `unexpected_order`, `quantity_mismatch`, `price_mismatch`, `position_mismatch`, `unresolved_order` (flagged stale after 5 min), `unresolved_position`, `duplicate_state`, `balance_invalid`, `exchange_unreachable`.

Any finding -> result MISMATCH/ERROR -> `RECONCILIATION_REQUIRED` and the live switch becomes BLOCKED. A later clean run with no unresolved orders clears the flag; live stays BLOCKED until an ADMIN re-enables it.

**Limitations:** there is no independent cash ledger for live accounts, so a balance *mismatch* cannot be computed (only sanity). Price mismatch only applies to non-market orders, which the engine does not place yet. Runs are not scheduled by default.

Startup (`runtime/startup.py`): database -> safety baseline -> strategy fingerprints -> recover unresolved orders -> reconcile each live exchange -> `safety.on_startup(ok)`. Any failure turns an ENABLED live switch into BLOCKED.
