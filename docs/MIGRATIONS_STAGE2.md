# Stage 2 database migration notes

Only one schema change in Stage 2.

## Migration 10 `position_exits` (additive)
```
position_exits(id, order_id UNIQUE -> orders, position_id -> positions, bot_id, exchange, environment, symbol,
               reason, requested_quantity, filled_quantity, remaining_quantity,
               status CHECK IN (PENDING, PARTIAL, COMPLETE, CANCELLED, FAILED, UNKNOWN),
               reconciliation_status CHECK IN (CLEAR, REQUIRED),
               created_at, updated_at, last_progress_at)
INDEX position_exits_position(position_id, status)
```
- Runs on SQLite and PostgreSQL 16 through the existing migration runner, inside the migration transaction.
- Backward compatibility: nothing existing is modified. Orders that already exist are not back-filled (rows appear for new EXIT orders). An exit that was in flight before the upgrade has no row; the position then behaves as before (status OPEN), and a REDUCING position without a tracked exit is reported as unverified and reconciled.
- Rollback: the table can be dropped; the order and position tables stay valid. Older code ignores it.
- Rows are derived from `orders` in the same transaction as every order change, so they cannot drift from the order.

## Non-schema persisted settings
- `safety_state` key `time_stop_config` (WP5): stored time-stop setting; refused changes while a position is open.

## Position status values in use
`OPEN`, `REDUCING` (new use, a partly sold position with an active exit), `CLOSED`, `RECONCILING`, `UNKNOWN`. `CLOSING` remains allowed by the schema and is not set by Stage 2 code.
