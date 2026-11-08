"""A small LOCAL web page to import Binance archive data and run backtests. A thin wrapper: every action runs the same command-line tools
(dewako.market_data.archive_import, dewako.market_data.resample, dewako.backtesting.cli) as a subprocess, so the GUI has no backtest logic of its own.

    cd backend
    python -m dewako.backtesting.gui            # then open http://127.0.0.1:8765

What it does NOT do: connect to any exchange, download anything, hold credentials, place orders, save evidence records (every run is --dry-run)
or enable trading. It listens on 127.0.0.1 only, requires a random token on every action, and rejects requests whose Host header is not local
(against other web pages poking at a local server). It uses only the Python standard library.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

MIN_FEE = 0.001                                   # the platform's rule: 0.1% per side is always charged; the gate refuses less
TIMEFRAMES = ("5m", "15m", "1h", "4h")
_SPIKE = re.compile(r"SUSPECT_SPIKE: (\d{4}-\d{2}-\d{2}T[0-9:+\-.]+):")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?)?$")
_SAFE = re.compile(r"^[A-Za-z0-9 ._:\-/]+$")
WORK = Path.cwd()                                  # relative file names are taken relative to where the GUI was started


class BadRequest(ValueError):
    pass


def _text(v: Any, name: str, pattern: re.Pattern = _SAFE, max_len: int = 300) -> str:
    if not isinstance(v, str) or not v.strip() or len(v) > max_len or not pattern.match(v.strip()):
        raise BadRequest(f"{name} is missing or has characters that are not allowed")
    return v.strip()


def _path(v: Any, name: str, must_exist: bool = False) -> str:
    if not isinstance(v, str) or not v.strip() or "\0" in v or len(v) > 500:
        raise BadRequest(f"{name} is missing")
    v = v.strip().strip('"').strip("'").strip()          # Windows "Copy as path" adds quotes
    if not v:
        raise BadRequest(f"{name} is missing")
    p = Path(v).expanduser()
    p = p if p.is_absolute() else WORK / p
    if must_exist and not p.is_file():
        raise BadRequest(f"{name}: no such file: {p}")
    return str(p)


def build_command(action: str, body: dict, *, work_dir: Path) -> tuple[list[str], dict[str, str]]:
    """Validate a request and turn it into an argument list (never a shell string). Raises BadRequest."""
    py, env = [sys.executable], {}
    if action == "import":
        return py + ["-m", "dewako.market_data.archive_import", "--zip", _path(body.get("zip"), "zip file", True), "--store", _path(body.get("store"), "candle store")], env
    if action == "resample":
        return py + ["-m", "dewako.market_data.resample", "--store", _path(body.get("store"), "candle store", True),
                     "--symbol", _text(body.get("symbol"), "symbol"), "--from", _text(body.get("from_tf"), "from timeframe"),
                     "--to", _text(body.get("to_tf"), "to timeframe"), "--source", _text(body.get("source"), "source"),
                     "--start", _text(body.get("start"), "start", _DATE), "--end", _text(body.get("end"), "end", _DATE)], env
    if action == "run":
        try:
            fee = float(body.get("fee_rate", MIN_FEE))
        except (TypeError, ValueError):
            raise BadRequest("fee must be a number") from None
        if not fee >= MIN_FEE:
            raise BadRequest(f"fee must be at least {MIN_FEE:g} (0.1% per side): runs without full fees are not meaningful here")
        try:
            lookback, cash = int(body.get("lookback", 300)), float(body.get("cash", 10000))
        except (TypeError, ValueError):
            raise BadRequest("lookback and cash must be numbers") from None
        if not (60 <= lookback <= 2000) or not cash > 0:
            raise BadRequest("lookback must be 60..2000 and cash positive")
        mode = body.get("mode", "screen")
        if mode not in ("screen", "full"):
            raise BadRequest("mode must be screen or full")
        tf = _text(body.get("timeframe"), "timeframe")
        if tf not in TIMEFRAMES:
            raise BadRequest("unknown timeframe")
        args = ["-m", "dewako.backtesting.gui", "_cli", "--candles", _path(body.get("store"), "candle store", True),
                "--symbol", _text(body.get("symbol"), "symbol"), "--timeframe", tf, "--source", _text(body.get("source"), "source"),
                "--start", _text(body.get("start"), "start", _DATE), "--end", _text(body.get("end"), "end", _DATE),
                "--strategy", _text(body.get("strategy"), "strategy"), "--version", _text(body.get("version"), "version"),
                "--cash", str(cash), "--lookback", str(lookback), "--fee-rate", str(fee), "--streak-reset", "daily", "--dry-run", "--json"]
        if mode == "screen":
            args.append("--screen")
        for t in body.get("ack_spikes") or []:
            args += ["--ack-spike", _text(t, "spike time", re.compile(r"^\d{4}-\d{2}-\d{2}T[0-9:+\-.]+$"))]
        plugin = body.get("plugin")
        if plugin:
            args += ["--plugin", _path(plugin, "strategy file", True)]
        env["DEWAKO_TRADING_DB"] = str(work_dir / "gui_runs.db")
        return [sys.executable] + args, env
    raise BadRequest("unknown action")


def coverage(store: str) -> list[dict]:
    """What a candle store holds: symbol / timeframe / source with first and last candle and count. Read-only."""
    p = Path(store).expanduser()
    p = p if p.is_absolute() else WORK / p
    if not p.is_file():
        raise BadRequest("no such candle store")
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        rows = c.execute("SELECT symbol,timeframe,source,MIN(open_ms),MAX(open_ms),COUNT(DISTINCT open_ms) FROM candles GROUP BY 1,2,3 ORDER BY 1,2,3").fetchall()
    except sqlite3.DatabaseError as e:
        raise BadRequest(f"not a candle store: {e}") from None
    finally:
        c.close()
    iso = lambda ms: time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))  # noqa: E731
    return [{"symbol": s, "timeframe": tf, "source": src, "first": iso(a), "last": iso(b), "candles": n} for s, tf, src, a, b, n in rows]


def strategies() -> list[dict]:
    from dewako.strategies import base, library  # noqa: F401
    out = []
    for n in base.names():
        for v in base.versions(n):
            cls = base.get(n, v)
            out.append({"name": n, "version": v, "candidate": cls.candidate, "directions": list(cls().describe()["directions"])})
    return out


class Jobs:
    """One job at a time (a backtest uses a whole core); the last output is kept for the page to poll."""

    def __init__(self, work_dir: Path):
        self.work_dir, self.lock, self.job = work_dir, threading.Lock(), None

    def start(self, label: str, cmd: list[str], env: dict[str, str], cwd: Path) -> dict:
        with self.lock:
            if self.job and self.job["status"] == "running":
                raise BadRequest("a job is already running")
            self.job = {"id": secrets.token_hex(4), "label": label, "status": "running", "stdout": "", "stderr": "", "code": None, "started": time.time()}
            job = self.job
        full_env = {**os.environ, **env, "PYTHONPATH": str(cwd), "PYTHONDONTWRITEBYTECODE": "1"}
        full_env.pop("PYTHONSAFEPATH", None)

        def work():
            try:
                pr = subprocess.run(cmd, cwd=cwd, env=full_env, capture_output=True, text=True)
                job.update(stdout=pr.stdout, stderr=pr.stderr, code=pr.returncode)
            except Exception as e:                                          # noqa: BLE001  (shown to the person, never swallowed)
                job.update(stderr=f"could not start: {e}", code=-1)
            job["status"] = "done"
            job["seconds"] = round(time.time() - job["started"], 1)
        threading.Thread(target=work, daemon=True).start()
        return {"id": job["id"]}

    def view(self) -> dict:
        j = self.job
        if not j:
            return {"status": "idle"}
        out = {k: j[k] for k in ("id", "label", "status", "code", "stdout", "stderr")}
        out["seconds"] = j.get("seconds", round(time.time() - j["started"], 1))
        if j["status"] == "done" and j["code"] == 2:
            out["spikes"] = sorted(set(_SPIKE.findall(j["stderr"])))
        if j["status"] == "done":
            try:
                out["result"] = json.loads(j["stdout"])
            except ValueError:
                pass
        return out


PAGE = r"""<!doctype html><meta charset=utf-8><title>Dewako backtest (local)</title><meta name=viewport content="width=device-width,initial-scale=1">
<style>:root{--bg:#fff;--fg:#1c1f26;--mut:#667;--line:#d8dbe2;--acc:#1f5eff;--bad:#b3261e;--ok:#17703a}
@media(prefers-color-scheme:dark){:root{--bg:#14161b;--fg:#e8eaf0;--mut:#9aa1b2;--line:#2c313c;--acc:#7aa2ff;--bad:#ff8a80;--ok:#6fd49a}}
body{background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif;max-width:860px;margin:0 auto;padding:16px}
h1{font-size:20px}h2{font-size:16px;margin:22px 0 8px}fieldset{border:1px solid var(--line);border-radius:8px;padding:12px;margin:0 0 12px}
label{display:block;font-size:13px;color:var(--mut);margin:8px 0 2px}input,select,button{font:inherit;padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);max-width:100%}
input[type=text],.row input,.row select{width:100%;box-sizing:border-box}.row{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px}
button{background:var(--acc);color:#fff;border:0;cursor:pointer;margin-top:10px}button.sec{background:none;color:var(--acc);border:1px solid var(--acc)}
pre{background:var(--line);padding:10px;border-radius:6px;overflow:auto;max-height:260px;font-size:12px;white-space:pre-wrap}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid var(--line);padding:4px 6px;text-align:left;font-size:13px}.bad{color:var(--bad)}.ok{color:var(--ok)}.mut{color:var(--mut);font-size:13px}</style>
<h1>Dewako backtest (local, simulated only)</h1>
<p class=mut>Runs on your computer. No exchange connection, no orders, no credentials. Fee is always at least 0.1% per side. Runs are not saved as evidence.</p>
<fieldset><legend>1. Data</legend>
<label>Candle store file (created if missing)</label><input type=text id=store value="candles.db">
<label>Binance archive .zip (monthly spot kline files, e.g. BTCUSDT-5m-2024-10.zip)</label><input type=text id=zip placeholder="C:\data\BTCUSDT-5m.zip or /home/me/BTCUSDT-5m.zip">
<button onclick="go('import',{zip:v('zip'),store:v('store')})">Import zip</button> <button class=sec onclick=cov()>Show what is in the store</button>
<div id=cov></div></fieldset>
<fieldset><legend>2. Make a slower timeframe (much faster backtests)</legend><div class=row>
<div><label>Symbol</label><input id=rs_sym value="BTC/USDT"></div><div><label>From</label><select id=rs_from><option>5m</option></select></div>
<div><label>To</label><select id=rs_to><option>1h</option><option>4h</option><option>15m</option></select></div>
<div><label>Source (as shown above)</label><input id=rs_src value="binance_archive"></div><div><label>Start</label><input id=rs_s value="2020-01-01"></div><div><label>End</label><input id=rs_e value="2026-11-01"></div></div>
<button onclick="go('resample',{store:v('store'),symbol:v('rs_sym'),from_tf:v('rs_from'),to_tf:v('rs_to'),source:v('rs_src'),start:v('rs_s'),end:v('rs_e')})">Resample</button></fieldset>
<fieldset><legend>3. Backtest</legend><div class=row>
<div><label>Strategy</label><select id=strat></select></div><div><label>Symbol</label><input id=sym value="BTC/USDT"></div>
<div><label>Timeframe</label><select id=tf><option>1h</option><option>4h</option><option>15m</option><option>5m</option></select></div>
<div><label>Source label</label><input id=src value="binance_archive:resampled:5m"></div>
<div><label>Start</label><input id=s value="2020-01-01"></div><div><label>End</label><input id=e value="2026-11-01"></div>
<div><label>Fee per side (min 0.001)</label><input id=fee value="0.001"></div><div><label>Start cash</label><input id=cash value="10000"></div>
<div><label>Lookback bars</label><input id=lb value="300"></div>
<div><label>Mode</label><select id=mode><option value=screen>Screen (fast, one run, not evidence)</option><option value=full>Full validation (slow)</option></select></div></div>
<label>Optional: your own strategy file (.py)</label><input type=text id=plugin placeholder="leave empty for the built-in strategies">
<button onclick=run()>Run backtest</button> <span class=mut>Screening 5m over years takes a long time; prefer 1h or 4h.</span></fieldset>
<h2 id=jt>Result</h2><div id=res class=mut>Nothing run yet.</div><pre id=log hidden></pre>
<script>
const TOKEN="__TOKEN__";let acks=[];const v=i=>document.getElementById(i).value;
async function api(p,b){const r=await fetch(p,{method:b?'POST':'GET',headers:{'X-Token':TOKEN,'Content-Type':'application/json'},body:b?JSON.stringify(b):undefined});
 const j=await r.json().catch(()=>({error:'bad response'}));if(!r.ok)throw new Error(j.error||r.status);return j}
function esc(s){return String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
async function go(a,b){try{await api('/api/'+a,b);poll()}catch(e){document.getElementById('res').innerHTML='<span class=bad>'+esc(e.message)+'</span>'}}
async function cov(){try{const r=await api('/api/coverage?store='+encodeURIComponent(v('store')));
 document.getElementById('cov').innerHTML='<table><tr><th>symbol<th>tf<th>source<th>from<th>to<th>candles</tr>'+r.rows.map(x=>`<tr><td>${esc(x.symbol)}<td>${x.timeframe}<td>${esc(x.source)}<td>${x.first}<td>${x.last}<td>${x.candles}`).join('')+'</table>'}
 catch(e){document.getElementById('cov').innerHTML='<span class=bad>'+esc(e.message)+'</span>'}}
function run(){go('run',{store:v('store'),strategy:S[v('strat')].name,version:S[v('strat')].version,symbol:v('sym'),timeframe:v('tf'),source:v('src'),start:v('s'),end:v('e'),
 fee_rate:v('fee'),cash:v('cash'),lookback:v('lb'),mode:v('mode'),plugin:v('plugin')||null,ack_spikes:acks})}
let S=[];api('/api/strategies').then(r=>{S=r.rows;document.getElementById('strat').innerHTML=S.map((x,i)=>`<option value=${i}>${esc(x.name)} v${x.version}${x.candidate?' (research)':''} ${x.directions.join('/')}</option>`).join('')});
let timer;async function poll(){clearInterval(timer);timer=setInterval(show,2000);show()}
async function show(){const j=await api('/api/job');const R=document.getElementById('res'),L=document.getElementById('log');
 if(j.status==='idle')return;document.getElementById('jt').textContent=j.label+' ('+j.seconds+'s)';
 if(j.status==='running'){R.innerHTML='Running... this page updates itself.';return}
 clearInterval(timer);let h='';
 if(j.spikes&&j.spikes.length){h+='<p class=bad>The data check refused these price spikes: '+j.spikes.map(esc).join(', ')+'. If you have looked at them (e.g. a real crash day) you can accept them and run again.</p><button class=sec onclick="acks=acks.concat(S_).filter((x,i,a)=>a.indexOf(x)==i);run()">Accept these spikes and re-run</button>';window.S_=j.spikes}
 const r=j.result;if(r){h+=`<p><b>${r.mode==='SCREENING_ONLY'?'SCREENING ONLY (not evidence)':'Verdict: '+r.verdict}</b> &middot; trades ${r.trades} &middot; net ${(+r.net_pnl).toFixed(2)} &middot; max drawdown ${(100*r.max_drawdown_pct).toFixed(2)}%</p>`;
  if(r.checks)h+='<table>'+r.checks.map(c=>`<tr><td class=${c.ok?'ok':'bad'}>${c.ok?'ok':'FAIL'}<td>${esc(c.name)}<td>${esc(c.detail)}`).join('')+'</table>';
  if(r.warnings&&r.warnings.length)h+='<p class=mut>'+r.warnings.map(esc).join('<br>')+'</p>'}
 else if(j.code!==0&&!(j.spikes&&j.spikes.length))h+='<p class=bad>Failed (exit '+j.code+'). Details in the box below.</p>';
 else if(j.code===0&&j.label==='import')h+='<p class=ok><b>Import finished</b> in '+j.seconds+' s. The report is in the box below (refused=0 means every file was loaded). Next: resample to 1h or 4h, then backtest.</p>';
 else if(j.code===0&&j.label==='resample')h+='<p class=ok><b>Resample finished</b> in '+j.seconds+' s. Check the new source with Show what is in the store.</p>';
 R.innerHTML=h;L.hidden=false;L.textContent=(j.stderr||'')+(r?'':j.stdout)}
poll();
</script>"""


def make_handler(token: str, jobs: Jobs, cwd: Path, port: int):
    local_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):                                              # quiet
            pass

        def _send(self, code: int, body: Any, ctype: str = "application/json"):
            raw = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _guard(self) -> bool:
            if self.headers.get("Host", "") not in local_hosts:
                self._send(403, {"error": "bad host"})
                return False
            return True

        def _authed(self) -> bool:
            if not secrets.compare_digest(self.headers.get("X-Token", ""), token):
                self._send(403, {"error": "bad token"})
                return False
            return True

        def do_GET(self):
            if not self._guard():
                return
            path, _, query = self.path.partition("?")
            if path == "/":
                return self._send(200, PAGE.replace("__TOKEN__", token), "text/html")
            if not self._authed():
                return
            try:
                if path == "/api/job":
                    return self._send(200, jobs.view())
                if path == "/api/strategies":
                    return self._send(200, {"rows": strategies()})
                if path == "/api/coverage":
                    from urllib.parse import parse_qs
                    return self._send(200, {"rows": coverage(parse_qs(query).get("store", [""])[0])})
            except BadRequest as e:
                return self._send(400, {"error": str(e)})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._guard() or not self._authed():
                return
            try:
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(min(n, 100_000)) or b"{}")
                action = self.path.rsplit("/", 1)[-1]
                cmd, env = build_command(action, body if isinstance(body, dict) else {}, work_dir=cwd)
                self._send(200, jobs.start(action, cmd, env, ROOT))
            except (BadRequest, ValueError) as e:
                self._send(400, {"error": str(e)})
    return H


ROOT = Path(__file__).resolve().parents[2]                                       # the backend directory


def _cli_with_plugin(argv: list[str]) -> int:
    """Subprocess entry: optionally load one research strategy file (the person's own code) and then run the normal backtest CLI."""
    if "--plugin" in argv:
        i = argv.index("--plugin")
        plugin = argv[i + 1]
        del argv[i:i + 2]
        import importlib.util
        spec = importlib.util.spec_from_file_location("user_strategy", plugin)
        mod = importlib.util.module_from_spec(spec)                              # type: ignore[arg-type]
        spec.loader.exec_module(mod)                                             # type: ignore[union-attr]
    from dewako.backtesting.cli import main
    return main(argv)


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "_cli":
        return _cli_with_plugin(argv[1:])
    p = argparse.ArgumentParser(prog="dewako.backtesting.gui", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--work-dir", default=".", help="where gui_runs.db (scratch database for the runs) is kept")
    p.add_argument("--no-browser", action="store_true")
    a = p.parse_args(argv)
    global WORK
    work = WORK = Path(a.work_dir).resolve()
    work.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(token, Jobs(work), work, a.port))
    url = f"http://127.0.0.1:{a.port}/"
    print(f"Dewako backtest GUI on {url}  (local only; Ctrl+C to stop)")
    if not a.no_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
