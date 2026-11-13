# Dewako - Phase 5 Remediation Task 1: READ-ONLY investigation of the paper order/position records.
#   powershell -ExecutionPolicy Bypass -File .\dewako_investigate.ps1
# Writes ONE new report file: <Root>\paper-state\investigation\report-<time>.txt (and a .json copy). Changes nothing else:
#   * the database is opened read-only (SQLite mode=ro + PRAGMA query_only=ON): no write is possible;
#   * the running app is not stopped, restarted or reconfigured; logs are only read;
#   * no secret is printed (the admin password is used only to read two API lists).
param([string]$Root = "C:\Users\Dewako\Documents\DEWAKO-AI")
$ErrorActionPreference = "Stop"
$State = Join-Path $Root "paper-state"
$Py = Join-Path $Root ".venv\Scripts\python.exe"
$Db = Join-Path $State "dewako_trading.db"
$Out = Join-Path $State "investigation"
if (-not (Test-Path $Db)) { Write-Host "FAILED: database not found at $Db" -ForegroundColor Red; exit 1 }
New-Item -ItemType Directory -Force -Path $Out | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$report = Join-Path $Out "report-$stamp.txt"

# --- 1. what the API really returns (shows whether the status script's counts were wrong) -------------------------------
$api = "http://127.0.0.1:8000"
$apiLines = @()
try {
    $s = Get-Content (Join-Path $State "secrets.json") -Raw | ConvertFrom-Json
    $tok = (Invoke-RestMethod "$api/auth/login" -Method Post -ContentType "application/json" -Body (@{ username = $s.admin_user; password = $s.admin_password } | ConvertTo-Json)).token
    $h = @{ Authorization = "Bearer $tok" }
    foreach ($ep in @("positions", "orders")) {
        $wrapped = @(Invoke-RestMethod "$api/$ep" -Headers $h).Count      # the exact expression the old -Action status used
        $r = Invoke-RestMethod "$api/$ep" -Headers $h
        $real = @($r | ForEach-Object { $_ } | Where-Object { $_ -ne $null }).Count     # enumerated: the real number of rows
        $apiLines += "API /${ep}: old status-script count = $wrapped, real row count = $real"
    }
} catch { $apiLines += "API check skipped: $($_.Exception.Message)" }

# --- 2. the database, read-only ---------------------------------------------------------------------------------------
$code = @'
import json, sqlite3, sys
db, out_json = sys.argv[1], sys.argv[2]
uri = "file:" + db.replace("\\", "/") + "?mode=ro"
c = sqlite3.connect(uri, uri=True)
c.row_factory = sqlite3.Row
c.execute("PRAGMA query_only=ON")
tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
want_all = ["positions", "position_events", "position_exits", "orders", "order_events", "trades", "execution_intents",
            "incidents", "reconciliation_findings", "paper_accounts", "bots", "risk_reservations"]
want_tail = {"signals": "created_at", "risk_decisions": "id", "audit_log": "id", "reconciliation_runs": "id"}
res = {"counts": {}, "rows": {}}
for t in tables:
    try: res["counts"][t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    except sqlite3.Error as e: res["counts"][t] = f"error: {e}"
for t in want_all:
    if t in tables:
        res["rows"][t] = [dict(r) for r in c.execute(f"SELECT * FROM {t} ORDER BY rowid")]
for t, col in want_tail.items():
    if t in tables:
        cols = [r[1] for r in c.execute(f"PRAGMA table_info({t})")]
        order = col if col in cols else "rowid"
        res["rows"][t] = [dict(r) for r in c.execute(f"SELECT * FROM {t} ORDER BY {order} DESC LIMIT 200")][::-1]
for r in res["rows"].get("audit_log", []):                      # keep the audit trail; drop nothing but long hashes
    for k in ("row_hash", "prev_hash"):
        if k in r and r[k]: r[k] = str(r[k])[:12] + "..."
json.dump(res, open(out_json, "w", encoding="utf-8"), indent=1, default=str)
print("TABLE COUNTS")
for t, n in res["counts"].items():
    if t in want_all or t in want_tail: print(f"  {t}: {n}")
for t in ["positions", "position_events", "position_exits", "orders", "order_events", "trades", "execution_intents", "incidents",
          "reconciliation_findings", "paper_accounts"]:
    rows = res["rows"].get(t, [])
    print(f"\n=== {t} ({len(rows)} rows)")
    for r in rows: print("  " + json.dumps(r, default=str))
print("\n=== signals (last 200)")
for r in res["rows"].get("signals", []):
    print("  " + json.dumps({k: r.get(k) for k in ("signal_id", "bot_id", "side", "signal_type", "status", "rejection_reason", "timestamp", "created_at")}, default=str))
print("\n=== risk_decisions (last 200)")
for r in res["rows"].get("risk_decisions", []): print("  " + json.dumps(r, default=str)[:400])
print("\n=== audit_log (last 200): action / result / resource / metadata")
for r in res["rows"].get("audit_log", []):
    print("  " + json.dumps({k: r.get(k) for k in ("id", "created_at", "username", "action", "result", "resource", "metadata")}, default=str)[:500])
'@
$tmp = Join-Path $Out "read_only_query-$stamp.py"
Set-Content -Path $tmp -Value $code -Encoding UTF8
$old = $ErrorActionPreference; $ErrorActionPreference = "Continue"
$dbText = & $Py $tmp $Db (Join-Path $Out "report-$stamp.json") 2>&1
$rc = $LASTEXITCODE
$ErrorActionPreference = $old
Remove-Item $tmp -ErrorAction SilentlyContinue

# --- 3. log lines about orders, positions, exits, incidents -------------------------------------------------------------
$logLines = @()
Get-ChildItem (Join-Path $State "logs") -Include "app-*.log", "engine-*.log", "runs.log", "watchdog.log" -Recurse -ErrorAction SilentlyContinue |
  Sort-Object LastWriteTime | ForEach-Object {
    $logLines += "--- $($_.Name)"
    $logLines += Select-String -Path $_.FullName -Pattern "order|position|exit|fill|incident|BLOCKED|reject|error|Traceback|dewako\.|start pid|DOWN|restart|WARNING" -CaseSensitive:$false |
                 Select-Object -Last 150 | ForEach-Object { $_.Line }
}

@("Dewako paper investigation $stamp (read-only)", "database: $Db", "", "== API vs status script") + $apiLines +
  @("", "== database (read-only, python exit code $rc)") + $dbText + @("", "== app log lines (last 150 matching per file)") + $logLines |
  Set-Content -Path $report -Encoding UTF8
Write-Host "report written: $report" -ForegroundColor Green
Write-Host ($apiLines -join "`n")
