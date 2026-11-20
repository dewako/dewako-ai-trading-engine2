#!/usr/bin/env bash
# Dewako ETH 1.0 - PAPER run on a Linux server (simulated money only; live trading stays LOCKED; no exchange keys are used or asked for).
# Linux counterpart of scripts/windows/dewako_paper.ps1: same settings, same bot, same status output; systemd replaces the scheduled
# tasks and the watchdog.
#
#   First time / update:   bash dewako_paper.sh setup
#   Check on it:           bash dewako_paper.sh status
#   Stop it:               bash dewako_paper.sh stop
#   (run is what the systemd service calls; selftest is what CI runs; you do not need to run them yourself.)
#
# What setup does, in order (re-running it is safe: it updates the code and restarts, it never creates a second bot):
#   1. checks git and Python 3.12/3.13      2. clones or fast-forwards the repo into $DEWAKO_ROOT/dewako-ai-trading-engine2 (main)
#   3. creates a virtualenv with the exact versions CI tests (constraints-test.txt); wheels only, never a source build
#   4. keeps all state OUTSIDE the repo in $DEWAKO_ROOT/paper-state (database, logs, secrets.env, mode 600)
#   5. verifies live Binance/Bybit/OKX data gives the 1,210 candles the strategy needs
#   6. installs and starts the systemd service "dewako-paper" (starts at boot, restarts on failure after 10 s)
#   7. creates the first admin, creates + enables bot "dewako-eth-paper" (Dewako ETH 1.0, ETH/USDT, 4h, PAPER)
#   8. prints the status
# The app listens on 127.0.0.1 only: nothing is reachable from the internet and no inbound firewall rule is needed.
set -euo pipefail

ACTION="${1:-setup}"
ROOT="${DEWAKO_ROOT:-$HOME/dewako}"
REPO_URL="${DEWAKO_REPO_URL:-https://github.com/dewako/dewako-ai-trading-engine2.git}"
REPO="$ROOT/dewako-ai-trading-engine2"
STATE="$ROOT/paper-state"
VENV="$ROOT/.venv"
PY="$VENV/bin/python"
SECRETS="$STATE/secrets.env"
LOGS="$STATE/logs"
API="${DEWAKO_API:-http://127.0.0.1:8000}"
UNIT="dewako-paper"
BOT_ID="dewako-eth-paper"
SELF_COPY="$ROOT/dewako_paper.sh"

say()  { printf '==> %s\n' "$*"; }
ok()   { printf 'ok  %s\n' "$*"; }
warn() { printf '!!  %s\n' "$*"; }
die()  { printf 'FAILED: %s\n' "$*" >&2; exit 1; }

# python used for small helpers: the venv once it exists, the system python3 before that (and in the self-test)
hpy() { if [ -x "$PY" ]; then echo "$PY"; else echo python3; fi; }

random_secret() { python3 -c 'import secrets; print(secrets.token_hex(24))'; }   # 48 hex characters

load_secrets() {
    if [ ! -f "$SECRETS" ]; then
        mkdir -p "$STATE"
        ( umask 077
          { echo "BOOTSTRAP_TOKEN=$(random_secret)"; echo "ADMIN_USER=dewako"; echo "ADMIN_PASSWORD=$(random_secret)"; } > "$SECRETS" )
    fi
    chmod 600 "$SECRETS"
    # shellcheck disable=SC1090
    . "$SECRETS"
}

set_app_env() {
    export DEWAKO_ENV="development"                                   # local single-machine paper run (SQLite)
    export DEWAKO_TRADING_DB="$STATE/dewako_trading.db"
    export DEWAKO_DB_PATH="$STATE/dewako_market.db"
    export DEWAKO_STRATEGY_LOCK="$STATE/strategy_versions.lock.json"
    export DEWAKO_MARKET_DATA="public"                                # Binance/Bybit/OKX public candles, no keys
    export DEWAKO_PAPER_TRADING="1"
    export DEWAKO_PAPER_BALANCE="10000"
    export DEWAKO_WORKERS="1"
    export DEWAKO_ALLOW_CODE_PLUGINS="0"
    export DEWAKO_BOOTSTRAP_TOKEN="$BOOTSTRAP_TOKEN"
    # quote guard: without these three the execution engine refuses EVERY order ("quote guard configuration is missing").
    # Same values the CI paper soak trades with and the Windows script uses.
    export DEWAKO_MAX_SPREAD_PCT="0.5"
    export DEWAKO_MAX_PRICE_DEVIATION_PCT="1.0"
    export DEWAKO_MAX_QUOTE_AGE_SECONDS="120"
    export PYTHONUTF8="1"
}

engine_args() { echo "-m uvicorn dewako.api.main:app --host 127.0.0.1 --port 8000"; }

unit_text() {   # $1 = user, $2 = root, $3 = the script copy the service runs
    cat <<EOF
[Unit]
Description=Dewako ETH 1.0 paper run (paper money only)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$1
Environment=DEWAKO_ROOT=$2
ExecStart=/usr/bin/env bash $3 run
Restart=always
RestartSec=10
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full

[Install]
WantedBy=multi-user.target
EOF
}

# One HTTP call with the standard library (no curl needed). Body, bearer token and bootstrap token come from the environment
# (API_BODY, API_AUTH, API_BOOT) so that they never appear in a process list. Prints the body; exit 22 on an HTTP error
# (the first line on stderr is "HTTP <code> ..."), 7 when nothing answers.
api() {   # api METHOD PATH
    API_BASE="$API" "$(hpy)" - "$1" "$2" <<'PYEOF'
import os, sys, urllib.request, urllib.error
method, path = sys.argv[1], sys.argv[2]
body = os.environ.get("API_BODY")
hdrs = {"Content-Type": "application/json"}
if os.environ.get("API_AUTH"): hdrs["Authorization"] = "Bearer " + os.environ["API_AUTH"]
if os.environ.get("API_BOOT"): hdrs["X-Bootstrap-Token"] = os.environ["API_BOOT"]
req = urllib.request.Request(os.environ["API_BASE"] + path, data=body.encode() if body else None, method=method, headers=hdrs)
try:
    with urllib.request.urlopen(req, timeout=int(os.environ.get("API_TIMEOUT", "10"))) as r:
        sys.stdout.write(r.read().decode())
except urllib.error.HTTPError as e:
    sys.stderr.write("HTTP %d %s\n" % (e.code, e.read().decode()[:300])); sys.exit(22)
except Exception as e:
    sys.stderr.write("ERROR %s\n" % e); sys.exit(7)
PYEOF
}

api_up() { API_TIMEOUT=3 api GET /health/live 2>/dev/null | grep -q '"alive"'; }

json_field() {   # json_field FIELD...: first non-empty of the named top-level fields of the JSON on stdin
    "$(hpy)" -c 'import sys, json
d = json.load(sys.stdin)
print(next((str(d[k]) for k in sys.argv[1:] if d.get(k)), ""))' "$@"
}

login() {
    local body token
    body=$(printf '{"username":"%s","password":"%s"}' "$ADMIN_USER" "$ADMIN_PASSWORD")
    token=$(API_BODY="$body" api POST /auth/login | json_field token access_token session_token) || die "login failed"
    [ -n "$token" ] || die "login answered without a token"
    TOKEN="$token"
}

admin_and_bot() {   # step 7; safe to run again
    local body err rc=0
    body=$(printf '{"username":"%s","password":"%s"}' "$ADMIN_USER" "$ADMIN_PASSWORD")
    err=$(API_BODY="$body" API_BOOT="$BOOTSTRAP_TOKEN" api POST /auth/bootstrap 2>&1 >/dev/null) || rc=$?
    if [ "$rc" -eq 0 ]; then ok "admin '$ADMIN_USER' created"
    elif [ "$rc" -eq 22 ] && [[ "$err" == "HTTP 409"* ]]; then ok "admin already exists"
    else die "bootstrap failed: $err"; fi
    login
    body='{"bot_id":"'"$BOT_ID"'","strategy":"Dewako ETH","version":"1.0","symbol":"ETH/USDT","timeframe":"4h","exchange":"paper","environment":"PAPER"}'
    rc=0; err=$(API_BODY="$body" API_AUTH="$TOKEN" api POST /bots 2>&1 >/dev/null) || rc=$?
    if [ "$rc" -eq 0 ]; then ok "bot $BOT_ID created"
    elif [ "$rc" -eq 22 ] && [[ "$err" == "HTTP 409"* ]] && [[ "$err" == *"already exists"* ]]; then ok "bot $BOT_ID already exists"
    else die "bot creation failed: $err"; fi
    API_AUTH="$TOKEN" api POST "/bots/$BOT_ID/enable" >/dev/null || die "could not enable bot $BOT_ID"
    ok "bot $BOT_ID enabled"
}

# Prints the same lines as the Windows status. Exit 0 when the app answers.
status_report() {
    login
    API_BASE="$API" API_AUTH="$TOKEN" STATE="$STATE" BOT_ID="$BOT_ID" "$(hpy)" - <<'PYEOF'
import json, os, sqlite3, sys, urllib.request, urllib.error
base, state, bot_id, tok = os.environ["API_BASE"], os.environ["STATE"], os.environ["BOT_ID"], os.environ["API_AUTH"]
errors = []                                                 # every call that failed is reported at the end; nothing is turned into "0"
def get(path, auth=True, tolerate=False):
    h = {"Authorization": "Bearer " + tok} if auth else {}
    try:
        with urllib.request.urlopen(urllib.request.Request(base + path, headers=h), timeout=10) as r: return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        if tolerate:                                        # /health answers 503 with a JSON body when not READY
            try: return json.loads(body)
            except Exception: pass
        errors.append("GET %s failed: HTTP %d" % (path, e.code)); return None
    except Exception as e:
        errors.append("GET %s failed: %s" % (path, e)); return None
def compare(api, db, cap):                                  # same rule as the Windows Compare-Counts
    if api == min(db, cap): return ""
    return "MISMATCH: the API lists %d, the database has %d%s" % (api, db, " (the API shows the latest %d)" % cap if db > cap else "")
h = get("/health", auth=False, tolerate=True) or {}
print("ok  health: %s (%s)" % (h.get("status"), h.get("state")))
d = get("/health/detailed")
if d:
    bad = [s["name"] for s in d["startup"]["steps"] if not s.get("ok")]
    if not bad: print("ok  startup: %s, every step ok; live trading: %s" % (d["startup"]["status"], d.get("live_trading")))
    else: print("!!  startup: %s; not ok: %s (trading_configuration = no orders can be placed)" % (d["startup"]["status"], ", ".join(bad)))
else: print("!!  could not read the startup report")
bots = get("/bots")
bot = next((b for b in (bots or []) if b.get("bot_id") == bot_id), None)
if bot: print("ok  bot %s: enabled=%s %s %s %s %s %s" % (bot_id, bot["enabled"], bot["strategy_id"], bot["strategy_version"], bot["symbol"], bot["timeframe"], bot["environment"]))
else: print("!!  bot %s not found" % bot_id)
pos = get("/positions")
orders = get("/orders?limit=500")
dbc = None
try:
    c = sqlite3.connect("file:" + os.path.join(state, "dewako_trading.db") + "?mode=ro", uri=True)
    dbc = {t: c.execute("SELECT COUNT(*) FROM " + t).fetchone()[0] for t in ("positions", "orders")}
except Exception: pass
dp = dbc["positions"] if dbc else "?"; do = dbc["orders"] if dbc else "?"
if pos is None: print("positions: ? (could not be read); database: %s" % dp)
else:
    print("positions: %d (open %d); database: %s" % (len(pos), sum(1 for p in pos if p.get("status") != "CLOSED"), dp))
    for p in pos[:5]: print("  ", p.get("id"), p.get("symbol"), p.get("side"), p.get("quantity"), p.get("average_price"), p.get("status"))
if orders is None: print("orders: ? (could not be read); database: %s" % do)
else: print("orders: %d; database: %s" % (len(orders), do))
if dbc:
    for m in ((compare(len(pos), dbc["positions"], 500) if pos is not None else ""), (compare(len(orders), dbc["orders"], 500) if orders is not None else "")):
        if m: print("!!  " + m)
else: print("!!  could not read the database counts (read-only query failed)")
full = get("/health/detailed")
if full is not None:
    with open(os.path.join(state, "last-health.json"), "w") as f: json.dump(full, f, indent=2)
    print("full health report: " + os.path.join(state, "last-health.json"))
for e in errors: print("!!  " + e)
sys.exit(1 if errors else 0)
PYEOF
}

sudo_cmd() { if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi; }
unit_active() { command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet "$UNIT" 2>/dev/null; }

do_run() {
    load_secrets; set_app_env
    mkdir -p "$LOGS"
    local stamp log pid code
    stamp=$(date +%Y%m%d-%H%M%S); log="$LOGS/engine-$stamp.log"       # one log per start, never overwritten
    cd "$REPO/backend"
    # shellcheck disable=SC2046
    "$PY" $(engine_args) 2> "$log" > "$LOGS/engine-$stamp.out.log" &
    pid=$!
    echo "$(date +%Y-%m-%dT%H:%M:%S) start pid $pid log engine-$stamp.log" >> "$LOGS/runs.log"
    trap 'kill -TERM "$pid" 2>/dev/null || true' TERM INT
    set +e
    wait "$pid"; code=$?
    if kill -0 "$pid" 2>/dev/null; then wait "$pid"; code=$?; fi       # the first wait returns early when a signal arrived
    echo "$(date +%Y-%m-%dT%H:%M:%S) exit pid $pid code $code" >> "$LOGS/runs.log"
    exit "$code"
}

do_stop() {
    if command -v systemctl >/dev/null 2>&1 && systemctl cat "$UNIT" >/dev/null 2>&1; then
        sudo_cmd systemctl disable --now "$UNIT" >/dev/null 2>&1 || true
    fi
    pkill -f "$PY -m uvicorn dewako.api.main:app" 2>/dev/null || true
    ok "stopped; it will not start again, not even at boot, until 'setup' is run"
}

do_status() {
    load_secrets
    if ! api_up; then
        printf 'NOT RUNNING: the app is not answering on %s\n' "$API"
        echo "  service $UNIT: $(systemctl is-active "$UNIT" 2>/dev/null || echo unknown)"
        if [ -f "$LOGS/runs.log" ]; then echo "  last lines of runs.log:"; tail -n 3 "$LOGS/runs.log" | sed 's/^/    /'; fi
        exit 1
    fi
    status_report
}

do_setup() {
    say "1/8 checking git and Python"
    command -v git >/dev/null 2>&1 || die "git is not installed (Ubuntu: sudo apt-get install -y git; Oracle Linux: sudo dnf install -y git)"
    local base=""
    for c in python3.13 python3.12 python3; do
        if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info[:2] in ((3,12),(3,13)) else 1)' 2>/dev/null; then base="$c"; break; fi
    done
    [ -n "$base" ] || die "Python 3.13 (or 3.12) is needed (Ubuntu 24.04: sudo apt-get install -y python3 python3-venv)"
    "$base" -c 'import venv, ensurepip' 2>/dev/null || die "the venv module is missing (Ubuntu: sudo apt-get install -y python3-venv)"
    command -v systemctl >/dev/null 2>&1 || die "systemd is required: it restarts the app after a crash and at boot"
    ok "git found; Python: $base ($(uname -m))"

    say "2/8 syncing the repo into $REPO"
    mkdir -p "$ROOT"
    if [ -d "$REPO/.git" ]; then
        local dirty; dirty=$(git -C "$REPO" status --porcelain)
        [ -z "$dirty" ] || die "the repo at $REPO has local changes; not touching it. Commit or remove them first:
$dirty"
        git -C "$REPO" fetch --quiet origin && git -C "$REPO" checkout --quiet main && git -C "$REPO" merge --ff-only --quiet origin/main || die "could not fast-forward main"
    elif [ -e "$REPO" ]; then die "$REPO exists but is not a git clone; move it away and run this again"
    else git clone --quiet "$REPO_URL" "$REPO" || die "git clone failed"; fi
    local sha; sha=$(git -C "$REPO" rev-parse --short HEAD)
    ok "repo at main $sha"

    say "3/8 Python environment (the versions CI tests)"
    if api_up; then do_stop; sleep 3; fi
    [ -x "$PY" ] || "$base" -m venv "$VENV" || die "could not create the virtualenv"
    "$PY" -m pip install --quiet --disable-pip-version-check --only-binary=:all: -r "$REPO/backend/requirements.txt" -c "$REPO/backend/constraints-test.txt" \
        || die "pip install failed. --only-binary means no source builds: if a package has no prebuilt wheel for $(uname -m), it is named above"
    ok "dependencies installed"

    say "4/8 state folder (outside the repo): $STATE"
    load_secrets; mkdir -p "$LOGS"
    ok "secrets in $SECRETS (bootstrap token and the admin password; mode 600; keep this file private)"

    say "5/8 verifying live market data on this server (ETH/USDT 4h, 1,210 candles needed)"
    if [ "${DEWAKO_SETUP_SKIP_FEED_CHECK:-0}" = "1" ]; then
        warn "SKIPPED by DEWAKO_SETUP_SKIP_FEED_CHECK=1: live market data was NOT verified. Do not use this for the real run."
    else
        set_app_env
        local feed
        feed=$(cd "$REPO/backend" && "$PY" - 2>&1 <<'PYEOF'
import asyncio
from dewako.market_data.hub import MarketDataHub
from dewako.market_data.binance_public import BinancePublicProvider
from dewako.market_data.bybit_public import BybitPublicProvider
from dewako.market_data.okx_public import OKXPublicProvider
from dewako.runtime.market_feed import make_state_provider, FEED_HISTORY
async def main():
    st = await make_state_provider(MarketDataHub([BinancePublicProvider(), BybitPublicProvider(), OKXPublicProvider()]))('ETH/USDT', '4h')
    print(len(st.candles), FEED_HISTORY, st.candles[0].t, st.candles[-1].t, st.candles[-1].c)
asyncio.run(main())
PYEOF
        ) || die "could not load live candles (is the internet reachable? exchanges blocked from this region?):
$feed"
        local n last; n=$(echo "$feed" | tail -n 1 | awk '{print $1}'); last=$(echo "$feed" | tail -n 1 | awk '{print $NF}')
        [ "$n" -ge 1200 ] 2>/dev/null || die "only $n candles came back; the strategy needs 1,200"
        ok "live feed: $n candles of ETH/USDT 4h, last close $last"
    fi

    say "6/8 systemd service '$UNIT' (starts at boot, restarts 10 s after a failure)"
    cp "$0" "$SELF_COPY.new" && mv "$SELF_COPY.new" "$SELF_COPY"        # the service runs this copy, so deleting the download breaks nothing
    unit_text "$(id -un)" "$ROOT" "$SELF_COPY" > "$ROOT/$UNIT.service"
    sudo_cmd cp "$ROOT/$UNIT.service" "/etc/systemd/system/$UNIT.service"
    sudo_cmd systemctl daemon-reload
    sudo_cmd systemctl enable --now "$UNIT" >/dev/null 2>&1
    local t=0; while ! api_up && [ "$t" -lt 45 ]; do sleep 2; t=$((t + 1)); done
    api_up || die "the app did not start within 90 s. Logs: $LOGS (runs.log names the engine log); journalctl -u $UNIT"
    ok "app running on $API (127.0.0.1 only)"

    say "7/8 admin user and the paper bot"
    admin_and_bot

    say "8/8 final check"
    sleep 20
    do_status
    echo
    ok "Dewako ETH 1.0 is paper trading here from main $sha. Paper money only; live trading is locked."
    echo "Check any time:  bash $SELF_COPY status"
}

do_selftest() {
    local fail=0 tmp; tmp=$(mktemp -d); TMPD="$tmp"; STUB_PID=""
    trap 'rm -rf "$TMPD"; [ -z "$STUB_PID" ] || kill "$STUB_PID" 2>/dev/null || true' EXIT
    check() { if [ "$2" = "$3" ]; then echo "ok    $1"; else echo "FAIL  $1 : got '$2', expected '$3'"; fail=$((fail + 1)); fi; }
    contains() { if [[ "$2" == *"$3"* ]]; then echo "ok    $1"; else echo "FAIL  $1 : '$3' not found in: $2"; fail=$((fail + 1)); fi; }
    local prc=0; bash -n "$0" || prc=$?; check "script parses" "$prc" "0"

    ROOT="$tmp/root"; STATE="$ROOT/paper-state"; SECRETS="$STATE/secrets.env"; LOGS="$STATE/logs"; PY="$tmp/no-venv"
    load_secrets
    check "secrets: bootstrap token is 48 hex characters" "$(printf '%s' "$BOOTSTRAP_TOKEN" | grep -cE '^[0-9a-f]{48}$')" "1"
    check "secrets: admin password is 48 hex characters" "$(printf '%s' "$ADMIN_PASSWORD" | grep -cE '^[0-9a-f]{48}$')" "1"
    check "secrets: file mode is 600" "$(stat -c %a "$SECRETS")" "600"
    local before; before=$(cat "$SECRETS"); load_secrets
    check "secrets: loading again keeps them" "$(cat "$SECRETS")" "$before"
    set_app_env
    check "env: paper trading on" "$DEWAKO_PAPER_TRADING" "1"
    check "env: code plugins off" "$DEWAKO_ALLOW_CODE_PLUGINS" "0"
    check "env: quote guard spread" "$DEWAKO_MAX_SPREAD_PCT" "0.5"
    check "env: quote guard deviation" "$DEWAKO_MAX_PRICE_DEVIATION_PCT" "1.0"
    check "env: quote guard age" "$DEWAKO_MAX_QUOTE_AGE_SECONDS" "120"
    check "env: market data is public (no keys)" "$DEWAKO_MARKET_DATA" "public"
    check "env: state is outside any repo checkout" "${DEWAKO_TRADING_DB#"$STATE"/}" "dewako_trading.db"
    contains "engine listens on 127.0.0.1 only" "$(engine_args)" "--host 127.0.0.1"
    local u; u=$(unit_text svc /srv/d /srv/d/dewako_paper.sh)
    check "unit: exactly one Restart= line" "$(printf '%s\n' "$u" | grep -c '^Restart=')" "1"
    contains "unit: restarts on failure" "$u" "Restart=always"
    contains "unit: no new privileges" "$u" "NoNewPrivileges=yes"
    contains "unit: runs the run action" "$u" "ExecStart=/usr/bin/env bash /srv/d/dewako_paper.sh run"
    contains "unit: starts at boot" "$u" "WantedBy=multi-user.target"
    contains "unit: runs as the given user" "$u" "User=svc"
    check "unit: nothing listens on 0.0.0.0" "$(printf '%s' "$u" | grep -c '0.0.0.0')" "0"

    # the real HTTP/JSON paths against a stand-in API (stdlib only)
    cat > "$tmp/stub.py" <<'PYEOF'
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
BOOT, port_file = sys.argv[1], sys.argv[2]
n = {"boot": 0, "bot": 0, "enable": 0}
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def out(self, code, obj):
        b = json.dumps(obj).encode(); self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def authed(self): return self.headers.get("Authorization") == "Bearer tok"
    def do_GET(self):
        p = self.path
        if p == "/health/live": return self.out(200, {"status": "alive"})
        if p == "/health": return self.out(200, {"status": "ok", "state": "READY"})
        if not self.authed(): return self.out(401, {})
        if p == "/health/detailed": return self.out(200, {"startup": {"status": "READY", "steps": [{"name": "trading_configuration", "ok": True}]}, "live_trading": "disabled"})
        if p == "/bots": return self.out(200, [{"bot_id": "dewako-eth-paper", "enabled": 1, "strategy_id": "Dewako ETH", "strategy_version": "1.0", "symbol": "ETH/USDT", "timeframe": "4h", "environment": "PAPER"}])
        if p == "/positions": return self.out(200, [])
        if p.startswith("/orders"): return self.out(200, [])
        self.out(404, {})
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        p = self.path
        if p == "/auth/bootstrap":
            if self.headers.get("X-Bootstrap-Token") != BOOT: return self.out(403, {})
            n["boot"] += 1; return self.out(201 if n["boot"] == 1 else 409, {"detail": "admin exists"})
        if p == "/auth/login": return self.out(200, {"token": "tok"})
        if not self.authed(): return self.out(401, {})
        if p == "/bots":
            n["bot"] += 1; return self.out(201 if n["bot"] == 1 else 409, {"detail": "bot already exists"})
        if p == "/bots/dewako-eth-paper/enable": n["enable"] += 1; return self.out(200, {})
        self.out(404, {})
srv = HTTPServer(("127.0.0.1", 0), H)
open(port_file, "w").write(str(srv.server_address[1]))
srv.serve_forever()
PYEOF
    python3 "$tmp/stub.py" "$BOOTSTRAP_TOKEN" "$tmp/port" & STUB_PID=$!
    local i=0; while [ ! -s "$tmp/port" ] && [ "$i" -lt 50 ]; do sleep 0.1; i=$((i + 1)); done
    API="http://127.0.0.1:$(cat "$tmp/port")"
    local rc=0; api_up || rc=$?; check "api_up sees the stand-in API" "$rc" "0"
    local out
    out=$(admin_and_bot 2>&1); contains "first setup creates the admin" "$out" "admin 'dewako' created"
    contains "first setup creates the bot" "$out" "bot $BOT_ID created"
    contains "first setup enables the bot" "$out" "bot $BOT_ID enabled"
    out=$(admin_and_bot 2>&1); contains "second setup: admin already exists" "$out" "admin already exists"
    contains "second setup: bot already exists, no second bot" "$out" "bot $BOT_ID already exists"
    out=$(status_report 2>&1) || true
    contains "status: health" "$out" "ok  health: ok (READY)"
    contains "status: startup" "$out" "ok  startup: READY, every step ok; live trading: disabled"
    contains "status: bot" "$out" "ok  bot $BOT_ID: enabled=1 Dewako ETH 1.0 ETH/USDT 4h PAPER"
    contains "status: no database yet is reported, not hidden" "$out" "!!  could not read the database counts"
    python3 -c "
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute('CREATE TABLE positions (id)'); c.execute('CREATE TABLE orders (id)'); c.commit()" "$STATE/dewako_trading.db"
    out=$(status_report 2>&1) || true
    check "status: API and database agree (0 and 0) -> no MISMATCH line at all" "$(printf '%s\n' "$out" | grep -c 'MISMATCH')" "0"
    contains "status: positions line" "$out" "positions: 0 (open 0); database: 0"
    contains "status: orders line" "$out" "orders: 0; database: 0"
    python3 -c "
import sqlite3, sys
c = sqlite3.connect(sys.argv[1]); c.execute('INSERT INTO orders VALUES (1)'); c.commit()" "$STATE/dewako_trading.db"
    out=$(status_report 2>&1) || true
    contains "status: API says 0 orders, database has 1 -> MISMATCH" "$out" "!!  MISMATCH: the API lists 0, the database has 1"
    check "status: exactly one MISMATCH line (positions still agree)" "$(printf '%s\n' "$out" | grep -c 'MISMATCH')" "1"
    kill "$STUB_PID"; STUB_PID=""; sleep 0.3
    rc=0; api_up || rc=$?; check "api_up is false when nothing answers" "$rc" "1"
    out=$(do_status 2>&1) || true; contains "status says NOT RUNNING when nothing answers" "$out" "NOT RUNNING"

    # the run action: per-start log, runs.log start/exit lines, exit code passed on, SIGTERM forwarded (what systemd sends on stop)
    REPO="$ROOT/dewako-ai-trading-engine2"; mkdir -p "$REPO/backend"
    PY="$tmp/fakepy"
    printf '#!/usr/bin/env bash\necho "engine started: $*" >&2\nexit 7\n' > "$PY"; chmod +x "$PY"
    rc=0; ( do_run ) || rc=$?
    check "run: the engine's exit code is passed on" "$rc" "7"
    contains "run: runs.log has a start line" "$(cat "$LOGS/runs.log")" " start pid "
    contains "run: runs.log has the exit line with the code" "$(cat "$LOGS/runs.log")" " code 7"
    contains "run: the engine log holds its stderr and the host is 127.0.0.1" "$(cat "$LOGS"/engine-*[0-9].log)" "--host 127.0.0.1 --port 8000"
    rm -f "$LOGS/runs.log" "$LOGS"/engine-*
    printf '#!/usr/bin/env bash\necho up >&2\nexec sleep 30\n' > "$PY"
    ( do_run ) & local wp=$!; sleep 1.5
    kill -TERM "$wp"; local w=0; while kill -0 "$wp" 2>/dev/null && [ "$w" -lt 50 ]; do sleep 0.1; w=$((w + 1)); done
    check "run: SIGTERM stops the wrapper within 5 s" "$(kill -0 "$wp" 2>/dev/null && echo alive || echo stopped)" "stopped"
    contains "run: the exit is recorded after a SIGTERM" "$(cat "$LOGS/runs.log")" " exit pid "

    if [ "$fail" -ne 0 ]; then echo "selftest: $fail check(s) FAILED"; exit 1; fi
    echo "selftest: all checks passed"
}

case "$ACTION" in
    setup)    do_setup ;;
    run)      do_run ;;
    status)   do_status ;;
    stop)     do_stop ;;
    selftest) do_selftest ;;
    *)        die "unknown action '$ACTION' (setup, status, stop; run and selftest are internal)" ;;
esac
