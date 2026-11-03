"""Ordered schema migrations. Never edit a released migration: add a new one.

The SQL is kept to the portable subset (TEXT/INTEGER/REAL, CHECK, UNIQUE, FOREIGN KEY) so it can run on SQLite
and PostgreSQL. Only SQLite is exercised in tests; see docs for what is untested on PostgreSQL.
"""

MIGRATIONS: list[tuple[int, str, str]] = [
    (1, "core", """
CREATE TABLE users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('ADMIN','TRADER','ANALYST','VIEWER')),
  status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','DISABLED')),
  failed_logins INTEGER NOT NULL DEFAULT 0,
  locked_until TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  token_hash TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL, revoked_at TEXT
);
CREATE TABLE exchanges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER REFERENCES users(id),
  exchange TEXT NOT NULL,
  label TEXT NOT NULL,
  environment TEXT NOT NULL CHECK (environment IN ('PAPER','LIVE')),
  enabled INTEGER NOT NULL DEFAULT 0,
  permissions TEXT NOT NULL,
  verified_at TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE credentials (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  exchange_id INTEGER NOT NULL REFERENCES exchanges(id),
  encrypted_blob BLOB NOT NULL,
  encryption_key_version INTEGER NOT NULL,
  last4 TEXT NOT NULL,
  withdrawal_permission INTEGER NOT NULL CHECK (withdrawal_permission IN (0,1)),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, rotated_at TEXT
);
CREATE TABLE strategy_versions (
  strategy_id TEXT NOT NULL,
  strategy_version TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  release_status TEXT NOT NULL DEFAULT 'RELEASED' CHECK (release_status IN ('DRAFT','RELEASED','INVALID')),
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL, released_at TEXT,
  PRIMARY KEY (strategy_id, strategy_version)
);
CREATE TABLE signals (
  signal_id TEXT PRIMARY KEY,
  user_id INTEGER, bot_id TEXT NOT NULL,
  strategy_id TEXT NOT NULL, strategy_version TEXT NOT NULL,
  symbol TEXT NOT NULL, timeframe TEXT NOT NULL DEFAULT '',
  side TEXT NOT NULL, signal_type TEXT NOT NULL DEFAULT 'ENTRY',
  price REAL, confidence REAL NOT NULL,
  timestamp TEXT NOT NULL, market_state_hash TEXT,
  status TEXT NOT NULL CHECK (status IN ('NEW','REJECTED','EXECUTING','EXECUTED','FAILED')),
  rejection_reason TEXT, created_at TEXT NOT NULL
);
CREATE TABLE execution_intents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bot_id TEXT NOT NULL, signal_id TEXT NOT NULL, intent TEXT NOT NULL,
  environment TEXT NOT NULL CHECK (environment IN ('PAPER','LIVE')),
  correlation_id TEXT NOT NULL,
  risk_decision_id TEXT,
  created_at TEXT NOT NULL,
  UNIQUE (bot_id, signal_id, intent)
);
CREATE TABLE orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  intent_id INTEGER NOT NULL UNIQUE REFERENCES execution_intents(id),
  client_order_id TEXT NOT NULL UNIQUE,
  exchange_order_id TEXT,
  exchange TEXT NOT NULL, environment TEXT NOT NULL CHECK (environment IN ('PAPER','LIVE')),
  market_type TEXT NOT NULL DEFAULT 'SPOT' CHECK (market_type IN ('SPOT','PERPETUAL','FUTURES')),
  bot_id TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL, order_type TEXT NOT NULL,
  quantity REAL NOT NULL, price REAL, filled REAL NOT NULL DEFAULT 0, avg_fill_price REAL,
  state TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 1,
  correlation_id TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX orders_state ON orders(state);
CREATE TABLE order_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER NOT NULL REFERENCES orders(id),
  event_type TEXT NOT NULL,
  previous_state TEXT, new_state TEXT NOT NULL,
  exchange_order_id TEXT, exchange_response TEXT, error TEXT,
  actor TEXT NOT NULL, correlation_id TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER NOT NULL REFERENCES orders(id),
  environment TEXT NOT NULL CHECK (environment IN ('PAPER','LIVE')),
  symbol TEXT NOT NULL, side TEXT NOT NULL, quantity REAL NOT NULL, price REAL NOT NULL, fee REAL NOT NULL DEFAULT 0,
  exchange_trade_id TEXT, correlation_id TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE (order_id, exchange_trade_id)
);
CREATE TABLE positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bot_id TEXT NOT NULL, exchange TEXT NOT NULL, account TEXT NOT NULL DEFAULT '',
  environment TEXT NOT NULL CHECK (environment IN ('PAPER','LIVE')),
  market_type TEXT NOT NULL DEFAULT 'SPOT' CHECK (market_type IN ('SPOT','PERPETUAL','FUTURES')),
  symbol TEXT NOT NULL, side TEXT NOT NULL,
  quantity REAL NOT NULL, entry_price REAL, average_price REAL,
  realized_pnl REAL NOT NULL DEFAULT 0, unrealized_pnl REAL NOT NULL DEFAULT 0,
  leverage REAL NOT NULL DEFAULT 1, stop_loss REAL, take_profit REAL,
  exchange_position_id TEXT,
  status TEXT NOT NULL CHECK (status IN ('OPENING','OPEN','REDUCING','CLOSING','CLOSED','UNKNOWN','RECONCILING')),
  opened_at TEXT, closed_at TEXT, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX one_active_position ON positions(bot_id, exchange, environment, symbol)
  WHERE status <> 'CLOSED';
CREATE TABLE safety_state (
  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL, updated_by TEXT NOT NULL
);
CREATE TABLE kill_switch_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  enabled INTEGER NOT NULL, reason TEXT NOT NULL,
  triggered_by TEXT, triggered_at TEXT,
  cleared_by TEXT, cleared_at TEXT
);
CREATE TABLE risk_state (
  scope TEXT PRIMARY KEY,            -- 'global' or a bot id
  day TEXT NOT NULL,                 -- UTC date the daily counters belong to
  day_start_equity REAL NOT NULL,
  peak_equity REAL NOT NULL,
  current_equity REAL NOT NULL,
  daily_realized_pnl REAL NOT NULL DEFAULT 0,
  consecutive_losses INTEGER NOT NULL DEFAULT 0,
  last_loss_at TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE paper_accounts (
  bot_id TEXT PRIMARY KEY, cash REAL NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE reconciliation_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  exchange TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
  result TEXT NOT NULL CHECK (result IN ('OK','MISMATCH','ERROR')),
  findings TEXT NOT NULL
);
CREATE TABLE audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  user_id INTEGER, username TEXT, role TEXT,
  action TEXT NOT NULL, resource TEXT, result TEXT NOT NULL,
  correlation_id TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}'
);
"""),

    (2, "bots_snapshots_reconciliation_health", """
CREATE TABLE strategies (
  strategy_id TEXT PRIMARY KEY,
  description TEXT NOT NULL DEFAULT '',
  experimental INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX strategy_fingerprint_uq ON strategy_versions(fingerprint);
CREATE TABLE bots (
  bot_id TEXT PRIMARY KEY,
  strategy_id TEXT NOT NULL, strategy_version TEXT NOT NULL,
  symbol TEXT NOT NULL, timeframe TEXT NOT NULL,
  environment TEXT NOT NULL CHECK (environment IN ('PAPER','LIVE')),
  exchange TEXT NOT NULL,
  risk_scope TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0,1)),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  FOREIGN KEY (strategy_id, strategy_version) REFERENCES strategy_versions(strategy_id, strategy_version)
);
CREATE TABLE risk_decisions (
  risk_decision_id TEXT PRIMARY KEY,
  signal_id TEXT NOT NULL REFERENCES signals(signal_id),
  bot_id TEXT NOT NULL,
  approved INTEGER NOT NULL CHECK (approved IN (0,1)),
  quantity REAL NOT NULL,
  reasons TEXT NOT NULL,
  decided_at TEXT NOT NULL
);
ALTER TABLE trades ADD COLUMN position_id INTEGER REFERENCES positions(id);
CREATE TABLE position_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  position_id INTEGER NOT NULL REFERENCES positions(id),
  event_type TEXT NOT NULL,
  previous_status TEXT, new_status TEXT NOT NULL,
  quantity REAL, price REAL,
  order_id INTEGER REFERENCES orders(id),
  correlation_id TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE account_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  exchange TEXT NOT NULL,
  environment TEXT NOT NULL CHECK (environment IN ('PAPER','LIVE')),
  account_type TEXT NOT NULL CHECK (account_type IN ('SPOT','MARGIN','PERPETUAL','FUTURES')),
  asset TEXT NOT NULL,
  total REAL NOT NULL, available REAL NOT NULL, locked REAL NOT NULL,
  margin_balance REAL, available_margin REAL, unrealized_pnl REAL, position_margin REAL,
  source TEXT NOT NULL CHECK (source IN ('EXCHANGE','DEWAKO')),
  taken_at TEXT NOT NULL
);
CREATE INDEX account_snapshots_lookup ON account_snapshots(exchange, environment, asset, taken_at);
CREATE TABLE reconciliation_state (
  scope TEXT PRIMARY KEY,
  state TEXT NOT NULL CHECK (state IN ('HEALTHY','RECONCILING','RECONCILIATION_REQUIRED','RESOLVED','FAILED')),
  reason TEXT NOT NULL DEFAULT '',
  last_run_id INTEGER REFERENCES reconciliation_runs(id),
  updated_at TEXT NOT NULL
);
CREATE TABLE reconciliation_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope TEXT NOT NULL,
  previous_state TEXT, new_state TEXT NOT NULL,
  reason TEXT NOT NULL, actor TEXT NOT NULL, correlation_id TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE reconciliation_findings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES reconciliation_runs(id),
  scope TEXT NOT NULL,
  finding_type TEXT NOT NULL,
  detail TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','RESOLVED')),
  resolution TEXT, resolved_by TEXT, resolved_at TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX reconciliation_findings_open ON reconciliation_findings(scope, status);
CREATE TABLE worker_health (
  name TEXT PRIMARY KEY,
  critical INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL CHECK (status IN ('STARTING','HEALTHY','DEGRADED','FAILED','STOPPED')),
  last_heartbeat TEXT, last_success TEXT, last_error TEXT,
  restart_count INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);
"""),
    (3, "exchange_account_identity", """
ALTER TABLE exchanges ADD COLUMN account_ref TEXT;
CREATE UNIQUE INDEX exchanges_identity_uq ON exchanges(exchange, label, environment);
"""),
    (4, "risk_reservations_and_incidents", """
CREATE TABLE risk_scopes (
  scope TEXT PRIMARY KEY,                    -- '<ENV>:<exchange>'. The row is the lock that serialises limit checks (SELECT ... FOR UPDATE)
  created_at TEXT NOT NULL
);
CREATE TABLE risk_reservations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER NOT NULL UNIQUE REFERENCES orders(id),
  scope TEXT NOT NULL REFERENCES risk_scopes(scope),
  bot_id TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
  quantity REAL NOT NULL CHECK (quantity > 0),
  notional REAL NOT NULL CHECK (notional > 0),
  opens_position INTEGER NOT NULL DEFAULT 0 CHECK (opens_position IN (0,1)),
  status TEXT NOT NULL CHECK (status IN ('ACTIVE','CONSUMED','RELEASED')),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX risk_reservations_active_idx ON risk_reservations(scope, status);
CREATE TABLE incidents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL, severity TEXT NOT NULL CHECK (severity IN ('HIGH','CRITICAL')),
  scope TEXT NOT NULL DEFAULT '', order_id INTEGER REFERENCES orders(id),
  detail TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','RESOLVED')),
  created_at TEXT NOT NULL, resolved_by TEXT, resolved_at TEXT, resolution TEXT
);
CREATE INDEX incidents_open_idx ON incidents(status);
"""),
    (5, "audit_hash_chain_and_worker_leases", """
ALTER TABLE audit_log ADD COLUMN prev_hash TEXT;
ALTER TABLE audit_log ADD COLUMN row_hash TEXT;
CREATE UNIQUE INDEX audit_log_prev_hash_uq ON audit_log(prev_hash) WHERE prev_hash IS NOT NULL;
CREATE TABLE worker_leases (
  name TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  epoch INTEGER NOT NULL,                    -- fencing token: increases every time the lease changes hands
  expires_at TEXT NOT NULL,
  acquired_at TEXT NOT NULL, renewed_at TEXT NOT NULL
);
"""),
    (6, "strategy_packages", """
CREATE TABLE strategy_packages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT, version TEXT,
  package_sha256 TEXT NOT NULL,
  fingerprint TEXT,
  state TEXT NOT NULL CHECK (state IN ('IMPORTED','VALIDATION_FAILED','VALIDATED','BACKTEST_ELIGIBLE','PAPER_ELIGIBLE','TESTNET_ELIGIBLE','DISABLED','QUARANTINED')),
  state_reason TEXT NOT NULL DEFAULT '',
  manifest TEXT, spec TEXT,
  findings TEXT NOT NULL DEFAULT '[]',
  imported_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX strategy_packages_name_version_uq ON strategy_packages(name, version) WHERE state <> 'VALIDATION_FAILED';
CREATE INDEX strategy_packages_sha_idx ON strategy_packages(package_sha256);
"""),
    (7, "ai_settings", """
CREATE TABLE ai_settings (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  config TEXT NOT NULL, providers TEXT NOT NULL,
  updated_by TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
);
CREATE TABLE ai_credentials (
  provider TEXT PRIMARY KEY,
  encrypted_blob BLOB NOT NULL,
  encryption_key_version INTEGER NOT NULL,
  last4 TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""),
    (8, "signal_protection_levels", """
ALTER TABLE signals ADD COLUMN stop_price REAL;
ALTER TABLE signals ADD COLUMN take_profit_price REAL;
"""),
    (9, "risk_limits_versions", """
CREATE TABLE risk_limits (
  version INTEGER PRIMARY KEY AUTOINCREMENT,
  limits TEXT NOT NULL,
  changed_by TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
"""),
    (10, "position_exits", """
CREATE TABLE position_exits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER NOT NULL UNIQUE REFERENCES orders(id),
  position_id INTEGER REFERENCES positions(id),
  bot_id TEXT NOT NULL, exchange TEXT NOT NULL, environment TEXT NOT NULL, symbol TEXT NOT NULL,
  reason TEXT NOT NULL,
  requested_quantity REAL NOT NULL, filled_quantity REAL NOT NULL DEFAULT 0, remaining_quantity REAL NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('PENDING','PARTIAL','COMPLETE','CANCELLED','FAILED','UNKNOWN')),
  reconciliation_status TEXT NOT NULL CHECK (reconciliation_status IN ('CLEAR','REQUIRED')),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_progress_at TEXT NOT NULL
);
CREATE INDEX position_exits_position ON position_exits(position_id, status);
"""),
]

# Statements that differ by dialect (triggers/functions contain semicolons and cannot be translated). They run inside the same
# transaction as the migration. audit_log becomes append-only at the database level.
MIGRATION_EXTRA: dict[int, dict[str, list[str]]] = {
    5: {
        "sqlite": [
            "CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END",
            "CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END",
        ],
        "postgres": [
            "CREATE FUNCTION audit_log_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'audit_log is append-only'; END; $$",
            "CREATE TRIGGER audit_log_no_mod BEFORE UPDATE OR DELETE ON audit_log FOR EACH ROW EXECUTE FUNCTION audit_log_immutable()",
            "CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log FOR EACH STATEMENT EXECUTE FUNCTION audit_log_immutable()",
        ],
    },
}
