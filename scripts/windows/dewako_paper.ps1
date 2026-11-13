# Dewako ETH 1.0 - PAPER run on this PC (simulated money only; live trading stays LOCKED; no exchange keys are used or asked for).
#
#   First time / update:   powershell -ExecutionPolicy Bypass -File .\dewako_paper.ps1 -Action setup
#   Check on it:           powershell -ExecutionPolicy Bypass -File .\dewako_paper.ps1 -Action status
#   Stop it:               powershell -ExecutionPolicy Bypass -File .\dewako_paper.ps1 -Action stop
#   (-Action run is what the scheduled task calls, -Action watchdog what the watchdog task calls every 5 minutes,
#    -Action selftest what CI runs under Windows PowerShell 5.1 and PowerShell 7; you do not need to run them yourself.)
#
# What setup does, in order (re-running it is safe: it updates the code and restarts, it never creates a second bot):
#   1. checks git and Python 3.12/3.13      2. clones or fast-forwards the repo into <Root>\dewako-ai-trading-engine2 (main)
#   3. creates a virtualenv with the exact versions CI tests (constraints-test.txt)
#   4. keeps all state OUTSIDE the repo in <Root>\paper-state (database, logs, secrets) so git updates never touch it
#   5. registers a scheduled task "Dewako Paper" that starts the app at logon, and "Dewako Paper Watchdog" that checks it every
#      5 minutes and starts it again if no engine process is left (every check is written to paper-state\logs\watchdog.log)
#   6. starts it, creates the first admin, creates + enables bot "dewako-eth-paper" (Dewako ETH 1.0, ETH/USDT, 4h, PAPER)
#   7. stops this PC from sleeping or hibernating on AC power
#   8. verifies: the app is healthy, live Binance data gives the 1,210 candles the strategy needs, the bot is enabled
param(
    [ValidateSet("setup", "run", "status", "stop", "watchdog", "selftest")] [string]$Action = "setup",
    [string]$Root = "C:\Users\Dewako\Documents\DEWAKO-AI",
    [string]$TestUrl = "",         # selftest only: a local HTTP server serving rows0.json / rows1.json / rows2.json (CI starts one)
    [switch]$TestTasks             # selftest only (CI): also register a throw-away watchdog task, read it back, and remove it
)
$ErrorActionPreference = "Stop"
$Repo    = Join-Path $Root "dewako-ai-trading-engine2"
$State   = Join-Path $Root "paper-state"
$Venv    = Join-Path $Root ".venv"
$Py      = Join-Path $Venv "Scripts\python.exe"
$Secrets = Join-Path $State "secrets.json"
$Api     = "http://127.0.0.1:8000"
$Task    = "Dewako Paper"
$Watch   = "Dewako Paper Watchdog"
$Logs    = Join-Path $State "logs"
$BotId   = "dewako-eth-paper"

function Say($m)  { Write-Host "==> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "ok  $m" -ForegroundColor Green }
function Warn($m) { Write-Host "!!  $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "FAILED: $m" -ForegroundColor Red; exit 1 }
function RandomSecret([int]$n = 24) {
    $b = New-Object byte[] $n; [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
    return ([BitConverter]::ToString($b) -replace '-', '').ToLower()              # 48 hex characters
}
# Native programs (git, pip, python, uvicorn) write normal progress to stderr; under PowerShell 5.1 with ErrorActionPreference=Stop
# that alone aborts the script. Run them with Continue and judge them by their exit code instead.
function Native([scriptblock]$b) {
    $old = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    try { & $b } finally { $ErrorActionPreference = $old }
}
function Load-Secrets {
    if (Test-Path $Secrets) { return Get-Content $Secrets -Raw | ConvertFrom-Json }
    $s = [pscustomobject]@{ bootstrap_token = (RandomSecret); admin_user = "dewako"; admin_password = (RandomSecret) }
    New-Item -ItemType Directory -Force -Path $State | Out-Null
    $s | ConvertTo-Json | Set-Content -Path $Secrets -Encoding UTF8
    return $s
}
function Set-AppEnv($s) {
    $env:DEWAKO_ENV = "development"                                   # local single-machine paper run (SQLite)
    $env:DEWAKO_TRADING_DB = Join-Path $State "dewako_trading.db"
    $env:DEWAKO_DB_PATH = Join-Path $State "dewako_market.db"
    $env:DEWAKO_STRATEGY_LOCK = Join-Path $State "strategy_versions.lock.json"
    $env:DEWAKO_MARKET_DATA = "public"                                # Binance/Bybit/OKX public candles, no keys
    $env:DEWAKO_PAPER_TRADING = "1"
    $env:DEWAKO_PAPER_BALANCE = "10000"
    $env:DEWAKO_WORKERS = "1"
    $env:DEWAKO_ALLOW_CODE_PLUGINS = "0"
    $env:DEWAKO_BOOTSTRAP_TOKEN = $s.bootstrap_token
    # quote guard: without these three the execution engine refuses EVERY order ("quote guard configuration is missing").
    # Same values the CI paper soak trades with. ETH/USDT spread is normally ~0.01%, so 0.5% only stops a broken quote.
    $env:DEWAKO_MAX_SPREAD_PCT = "0.5"
    $env:DEWAKO_MAX_PRICE_DEVIATION_PCT = "1.0"
    $env:DEWAKO_MAX_QUOTE_AGE_SECONDS = "120"
    $env:PYTHONUTF8 = "1"
}
# D1 (Phase 5 remediation): under Windows PowerShell 5.1, Invoke-RestMethod / ConvertFrom-Json emit a JSON array as ONE object, so
# @(Invoke-RestMethod ...).Count was 1 for an empty list ("1 total" with nothing there). Enumerate the rows instead.
function Get-Rows($r) { return , @($r | ForEach-Object { $_ } | Where-Object { $null -ne $_ }) }
# The API lists at most $cap rows; compare it with the database count only where both can be complete.
function Compare-Counts([int]$api, [int]$db, [int]$cap) {
    $expect = [Math]::Min($db, $cap)
    if ($api -eq $expect) { return "" }
    return "MISMATCH: the API lists $api, the database has $db" + $(if ($db -gt $cap) { " (the API shows the latest $cap)" } else { "" })
}
# D2: what the watchdog does. It never stops a running engine process (an order transition could be cut in half); it only starts
# the task when no engine process is left.
function Watchdog-Decision([bool]$apiUp, [int]$engineProcs, [string]$taskState) {
    if ($apiUp) { return "alive" }
    if ($engineProcs -gt 0) { return "wait" }              # starting up, or hung: logged, not touched
    if ($taskState -eq "Running") { return "restart-stuck" }  # the task's PowerShell host outlived the engine
    return "restart"
}
function Register-Watchdog([string]$name, [string]$self, [string]$root) {
    $a = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$self`" -Action watchdog -Root `"$root`""
    $t = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)   # no duration = indefinitely
    $o = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
         -ExecutionTimeLimit (New-TimeSpan -Minutes 2) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $name -Action $a -Trigger $t -Settings $o -Description "Starts the Dewako paper run again if it stopped" -Force | Out-Null
    Enable-ScheduledTask -TaskName $name | Out-Null
}
function Engine-Procs { @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like "*uvicorn dewako.api.main:app*" }) }
function Db-Counts {
    $db = Join-Path $State "dewako_trading.db"
    # no double quotes in the code: Windows PowerShell 5.1 mangles them in arguments to native programs
    $code = 'import sqlite3,sys; c=sqlite3.connect(''file:''+sys.argv[1].replace(chr(92),''/'')+''?mode=ro'',uri=True); ' +
            'print(*[c.execute(''SELECT COUNT(*) FROM ''+t).fetchone()[0] for t in (''positions'',''orders'')])'
    $out = Native { & $Py -c $code $db 2>&1 }
    if ($LASTEXITCODE -ne 0) { return $null }
    $p = ("" + ($out | Select-Object -Last 1)).Trim().Split(" ")
    return @{ positions = [int]$p[0]; orders = [int]$p[1] }
}
function Api-Up { try { (Invoke-RestMethod "$Api/health/live" -TimeoutSec 3).status -eq "alive" } catch { $false } }
function Login($s) {
    $body = @{ username = $s.admin_user; password = $s.admin_password } | ConvertTo-Json
    $r = Invoke-RestMethod "$Api/auth/login" -Method Post -ContentType "application/json" -Body $body
    $t = if ($r.token) { $r.token } elseif ($r.access_token) { $r.access_token } else { $r.session_token }
    if (-not $t) { Die "login answered without a token: $($r | ConvertTo-Json -Compress)" }
    return @{ Authorization = "Bearer $t" }
}

# ------------------------------------------------------------------------------------------------------------- run
if ($Action -eq "run") {
    $s = Load-Secrets; Set-AppEnv $s
    New-Item -ItemType Directory -Force -Path $Logs | Out-Null
    # one log file per start (never overwritten, plain UTF-8, no PowerShell error wrapping); runs.log records every start and exit
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $log = Join-Path $Logs "engine-$stamp.log"
    Set-Location (Join-Path $Repo "backend")
    $p = Start-Process -FilePath $Py -ArgumentList @("-m", "uvicorn", "dewako.api.main:app", "--host", "127.0.0.1", "--port", "8000") `
         -NoNewWindow -PassThru -RedirectStandardError $log -RedirectStandardOutput (Join-Path $Logs "engine-$stamp.out.log")
    $null = $p.Handle                     # keep the handle, or ExitCode is empty after the process ends (.NET Process quirk)
    Add-Content (Join-Path $Logs "runs.log") "$(Get-Date -Format s) start pid $($p.Id) log engine-$stamp.log"
    $p.WaitForExit()
    Add-Content (Join-Path $Logs "runs.log") "$(Get-Date -Format s) exit pid $($p.Id) code $($p.ExitCode)"
    exit $p.ExitCode
}

# ------------------------------------------------------------------------------------------------------------- watchdog
if ($Action -eq "watchdog") {
    New-Item -ItemType Directory -Force -Path $Logs | Out-Null
    $procs = Engine-Procs
    $state = "$((Get-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue).State)"
    $d = Watchdog-Decision (Api-Up) $procs.Count $state
    $line = "$(Get-Date -Format s) $d (engine processes: $($procs.Count), task: $state)"
    if ($d -eq "restart-stuck") { Stop-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue; Start-Sleep 2 }
    if ($d -like "restart*") { Start-ScheduledTask -TaskName $Task; $line += " -> task started" }
    Add-Content (Join-Path $Logs "watchdog.log") $line
    exit 0
}

# ------------------------------------------------------------------------------------------------------------- selftest
if ($Action -eq "selftest") {
    $fail = 0
    function Check($name, $got, $want) {
        if ("$got" -eq "$want") { Write-Host "ok    $name" } else { Write-Host "FAIL  $name : got '$got', expected '$want'"; $script:fail++ }
    }
    Write-Host "PowerShell $($PSVersionTable.PSVersion) ($($PSVersionTable.PSEdition))"
    foreach ($f in @($PSCommandPath, (Join-Path $PSScriptRoot "dewako_investigate.ps1"))) {
        $errs = $null; [void][System.Management.Automation.Language.Parser]::ParseFile($f, [ref]$null, [ref]$errs)
        Check "parses: $(Split-Path $f -Leaf)" @($errs).Count 0
    }
    foreach ($case in @(@('[]', 0), @('[{"id":1}]', 1), @('[{"id":1},{"id":2}]', 2))) {
        Check "Get-Rows of $($case[0])" (Get-Rows (ConvertFrom-Json $case[0])).Count $case[1]
        $v = ConvertFrom-Json $case[0]
        Check "Get-Rows of a stored $($case[0])" (Get-Rows $v).Count $case[1]
    }
    Check "Get-Rows of null" (Get-Rows $null).Count 0
    if ($PSVersionTable.PSVersion.Major -le 5) {    # the bug D1 fixed must reproduce here, or this test would prove nothing
        Check "Windows PowerShell 5.1 reproduces the old miscount" @(ConvertFrom-Json '[]').Count 1
    } else {
        Write-Host "info  PowerShell 7 enumerates arrays itself; the old expression counts $(@(ConvertFrom-Json '[]').Count) here"
    }
    if ($TestUrl) {                                   # the real cmdlet over HTTP, as status uses it
        foreach ($n in 0, 1, 2) { Check "Invoke-RestMethod rows$n.json" (Get-Rows (Invoke-RestMethod "$TestUrl/rows$n.json")).Count $n }
    } else { Write-Host "FAIL  -TestUrl not given: the Invoke-RestMethod checks did not run"; $fail++ }
    Check "counts agree" (Compare-Counts 0 0 500) ""
    Check "counts disagree" (Compare-Counts 1 0 500) "MISMATCH: the API lists 1, the database has 0"
    Check "count above the API cap" (Compare-Counts 500 731 500) ""
    Check "count above the API cap, short" (Compare-Counts 499 731 500) "MISMATCH: the API lists 499, the database has 731 (the API shows the latest 500)"
    Check "watchdog: answering" (Watchdog-Decision $true 1 "Running") "alive"
    Check "watchdog: process present, not answering" (Watchdog-Decision $false 1 "Running") "wait"
    Check "watchdog: no process, task ended" (Watchdog-Decision $false 0 "Ready") "restart"
    Check "watchdog: no process, task still running" (Watchdog-Decision $false 0 "Running") "restart-stuck"
    Check "watchdog: no process, task missing" (Watchdog-Decision $false 0 "") "restart"
    if ($TestTasks) {
        $tn = "Dewako Selftest Watchdog"
        try {
            Register-Watchdog $tn $PSCommandPath "C:\nowhere"
            $t = Get-ScheduledTask -TaskName $tn
            Check "watchdog task registered and enabled" "$($t.State)" "Ready"
            Check "watchdog repeats every 5 minutes" $t.Triggers[0].Repetition.Interval "PT5M"
            Check "watchdog repeats indefinitely" "$($t.Triggers[0].Repetition.Duration)" ""
            Check "watchdog runs this script's watchdog action" ($t.Actions[0].Arguments -like "*-Action watchdog*") $true
            Check "watchdog cannot pile up" "$($t.Settings.MultipleInstances)" "IgnoreNew"
        } catch { Write-Host "FAIL  watchdog task registration: $($_.Exception.Message)"; $fail++ }
        finally { Unregister-ScheduledTask -TaskName $tn -Confirm:$false -ErrorAction SilentlyContinue }
    }
    if ($fail) { Write-Host "selftest: $fail check(s) FAILED"; exit 1 }
    Write-Host "selftest: all checks passed"; exit 0
}

# ------------------------------------------------------------------------------------------------------------- stop
if ($Action -eq "stop") {
    Disable-ScheduledTask -TaskName $Watch -ErrorAction SilentlyContinue | Out-Null      # or it would start the app again within 5 minutes
    Stop-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like "*uvicorn dewako.api.main:app*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Ok "stopped; the watchdog is disabled (both tasks stay registered; -Action setup starts and re-enables them)"
    exit 0
}

# ------------------------------------------------------------------------------------------------------------- status
if ($Action -eq "status") {
    $s = Load-Secrets
    if (-not (Api-Up)) {
        Write-Host "NOT RUNNING: the app is not answering on $Api" -ForegroundColor Red
        Write-Host "  engine processes: $((Engine-Procs).Count); task '$Task': $((Get-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue).State); watchdog: $((Get-ScheduledTask -TaskName $Watch -ErrorAction SilentlyContinue).State)"
        foreach ($f in "runs.log", "watchdog.log") {
            $fp = Join-Path $Logs $f
            if (Test-Path $fp) { Write-Host "  last lines of ${f}:"; Get-Content $fp -Tail 3 | ForEach-Object { Write-Host "    $_" } }
        }
        exit 1
    }
    $h = Login $s
    $ready = try { Invoke-RestMethod "$Api/health" } catch { $_.ErrorDetails.Message | ConvertFrom-Json }
    Ok "health: $($ready.status) ($($ready.state))"
    try {
        $d0 = Invoke-RestMethod "$Api/health/detailed" -Headers $h
        $bad = @($d0.startup.steps | Where-Object { -not $_.ok } | ForEach-Object { $_.name })
        if ($bad.Count -eq 0) { Ok "startup: $($d0.startup.status), every step ok; live trading: $($d0.live_trading)" }
        else { Warn "startup: $($d0.startup.status); not ok: $($bad -join ', ') (trading_configuration = no orders can be placed)" }
    } catch { Warn "could not read the startup report: $_" }
    $bot = (Invoke-RestMethod "$Api/bots" -Headers $h) | Where-Object { $_.bot_id -eq $BotId }
    if ($bot) { Ok "bot ${BotId}: enabled=$($bot.enabled) $($bot.strategy_id) $($bot.strategy_version) $($bot.symbol) $($bot.timeframe) $($bot.environment)" } else { Warn "bot $BotId not found" }
    $pos = Get-Rows (Invoke-RestMethod "$Api/positions" -Headers $h)
    $ord = Get-Rows (Invoke-RestMethod "$Api/orders?limit=500" -Headers $h)
    $dbc = Db-Counts
    $dbp = if ($dbc) { $dbc.positions } else { "?" }; $dbo = if ($dbc) { $dbc.orders } else { "?" }
    Write-Host ("positions: {0} (open {1}); database: {2}" -f $pos.Count, @($pos | Where-Object { $_.status -ne "CLOSED" }).Count, $dbp)
    if ($pos.Count) { $pos | Select-Object -First 5 id, symbol, side, quantity, average_price, status | Format-Table -AutoSize | Out-String | Write-Host }
    Write-Host ("orders: {0}; database: {1}" -f $ord.Count, $dbo)
    if ($dbc) {
        foreach ($m in @((Compare-Counts $pos.Count $dbc.positions 500), (Compare-Counts $ord.Count $dbc.orders 500))) { if ($m) { Warn $m } }
    } else { Warn "could not read the database counts (read-only query failed)" }
    $w = Join-Path $Logs "watchdog.log"
    if (Test-Path $w) { Write-Host "watchdog: $(Get-Content $w -Tail 1)" } else { Warn "watchdog has not written anything yet" }
    try { $d = Invoke-RestMethod "$Api/health/detailed" -Headers $h; $d | ConvertTo-Json -Depth 4 | Set-Content (Join-Path $State "last-health.json"); Write-Host "full health report: $State\last-health.json" } catch { Warn "detailed health not readable: $_" }
    exit 0
}

# ------------------------------------------------------------------------------------------------------------- setup
Say "1/8 checking git and Python"
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Die "git is not installed. Install it from https://git-scm.com/download/win, reopen PowerShell, run this again." }
$base = $null
foreach ($v in @("3.13", "3.12")) {
    try { $p = Native { & py "-$v" -c "import sys; print(sys.executable)" 2>$null }; if ($LASTEXITCODE -eq 0 -and $p) { $base = "$p".Trim(); break } } catch {}
}
if (-not $base) {
    try { $ver = Native { & python -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null }; if ("$ver".Trim() -in @("3.12", "3.13")) { $base = ("" + (Native { & python -c "import sys; print(sys.executable)" })).Trim() } } catch {}
}
if (-not $base) { Die "Python 3.13 (or 3.12) is needed. Install 3.13 from https://www.python.org/downloads/windows/ (tick 'Add python.exe to PATH'), then run this again." }
Ok "git found; Python: $base"

Say "2/8 syncing the repo into $Repo"
New-Item -ItemType Directory -Force -Path $Root | Out-Null
if (Test-Path (Join-Path $Repo ".git")) {
    Push-Location $Repo
    $dirty = Native { git status --porcelain }
    if ($dirty) { Pop-Location; Die "the repo at $Repo has local changes; not touching it. Commit or remove them first:`n$dirty" }
    Native { git fetch --quiet origin; git checkout --quiet main; git merge --ff-only --quiet origin/main }
    if ($LASTEXITCODE -ne 0) { Pop-Location; Die "could not fast-forward main" }
    Pop-Location
} elseif (Test-Path $Repo) {
    Die "$Repo exists but is not a git clone; move it away and run this again"
} else {
    Native { git clone --quiet https://github.com/dewako/dewako-ai-trading-engine2.git $Repo }
    if ($LASTEXITCODE -ne 0) { Die "git clone failed" }
}
$sha = ("" + (Native { git -C $Repo rev-parse --short HEAD })).Trim()
Ok "repo at main $sha"

Say "3/8 Python environment (the versions CI tests)"
$wasRunning = Api-Up
if ($wasRunning) { & $PSCommandPath -Action stop -Root $Root | Out-Null; Start-Sleep 3 }
if (-not (Test-Path $Py)) { Native { & $base -m venv $Venv }; if ($LASTEXITCODE -ne 0) { Die "could not create the virtualenv" } }
Native { & $Py -m pip install --quiet --disable-pip-version-check -r (Join-Path $Repo "backend\requirements.txt") -c (Join-Path $Repo "backend\constraints-test.txt") }
if ($LASTEXITCODE -ne 0) { Die "pip install failed (see above)" }
Ok "dependencies installed"

Say "4/8 state folder (outside the repo): $State"
$s = Load-Secrets
New-Item -ItemType Directory -Force -Path (Join-Path $State "logs") | Out-Null
Ok "secrets in $Secrets (bootstrap token and the admin password; keep this file private)"

Say "5/8 verifying live market data on this PC (ETH/USDT 4h, 1,210 candles needed)"
Set-AppEnv $s
Push-Location (Join-Path $Repo "backend")
$feed = Native { & $Py -c @"
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
"@ 2>&1 }
Pop-Location
if ($LASTEXITCODE -ne 0) { Die "could not load live candles (is the internet reachable? Binance blocked here?):`n$feed" }
$parts = ("" + ($feed | Select-Object -Last 1)).Trim().Split(" ")
if ([int]$parts[0] -lt 1200) { Die "only $($parts[0]) candles came back; the strategy needs 1,200" }
Ok "live feed: $($parts[0]) candles of ETH/USDT 4h, last close $($parts[-1])"

Say "6/8 scheduled tasks '$Task' (starts at logon) and '$Watch' (every 5 minutes)"
$Self = Join-Path $Root "dewako_paper.ps1"                          # the task runs this copy, so deleting the download breaks nothing
if ($PSCommandPath -ne $Self) { Copy-Item -Path $PSCommandPath -Destination $Self -Force }
$taskAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Self`" -Action run -Root `"$Root`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$set     = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
           -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $Task -Action $taskAction -Trigger $trigger -Settings $set -Description "Dewako ETH 1.0 paper run (paper money only)" -Force | Out-Null
Start-ScheduledTask -TaskName $Task
Register-Watchdog $Watch $Self $Root
$deadline = (Get-Date).AddSeconds(90)
while (-not (Api-Up) -and (Get-Date) -lt $deadline) { Start-Sleep 2 }
if (-not (Api-Up)) { Die "the app did not start within 90 s. Logs: $Logs (runs.log names the engine log)" }
Ok "app running on $Api; watchdog registered"

Say "7/8 admin user and the paper bot"
$bb = @{ username = $s.admin_user; password = $s.admin_password } | ConvertTo-Json
try { Invoke-RestMethod "$Api/auth/bootstrap" -Method Post -ContentType "application/json" -Headers @{ "X-Bootstrap-Token" = $s.bootstrap_token } -Body $bb | Out-Null; Ok "admin '$($s.admin_user)' created" }
catch { if ($_.Exception.Response.StatusCode.value__ -eq 409) { Ok "admin already exists" } else { Die "bootstrap failed: $($_.ErrorDetails.Message)" } }
$h = Login $s
$botBody = @{ bot_id = $BotId; strategy = "Dewako ETH"; version = "1.0"; symbol = "ETH/USDT"; timeframe = "4h"; exchange = "paper"; environment = "PAPER" } | ConvertTo-Json
try { Invoke-RestMethod "$Api/bots" -Method Post -ContentType "application/json" -Headers $h -Body $botBody | Out-Null; Ok "bot $BotId created" }
catch { if ($_.Exception.Response.StatusCode.value__ -eq 409 -and "$($_.ErrorDetails.Message)" -like "*already exists*") { Ok "bot $BotId already exists" } else { Die "bot creation failed: $($_.ErrorDetails.Message)" } }
Invoke-RestMethod "$Api/bots/$BotId/enable" -Method Post -Headers $h | Out-Null
Ok "bot $BotId enabled"

Say "8/8 power settings and final check"
Native { powercfg /change standby-timeout-ac 0 | Out-Null; powercfg /change hibernate-timeout-ac 0 | Out-Null }
if ($LASTEXITCODE -eq 0) { Ok "sleep and hibernate disabled on AC power (the screen may still turn off; that is fine)" }
else { Warn "could not change the power settings; set Settings > System > Power > 'When plugged in, put my device to sleep after' = Never" }
Start-Sleep 20
& $Self -Action status -Root $Root
Write-Host ""
Ok "Dewako ETH 1.0 is paper trading on this PC from main $sha. Paper money only; live trading is locked."
Write-Host "Check any time:  powershell -ExecutionPolicy Bypass -File `"$Self`" -Action status"
