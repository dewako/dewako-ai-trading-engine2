/* DEWAKO Backtest records: pure helpers for the READ-ONLY report page (no DOM, no network). Everything shown comes from the server's stored run
   records; nothing is computed or invented here, and nothing here can start, edit or promote a run. */
(function (root, factory) { if (typeof module === 'object' && module.exports) module.exports = factory(); else root.DewakoBacktests = factory(); })(this, function () {
  'use strict';
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function num(v, d) { return v == null || isNaN(v) ? 'n/a' : Number(v).toFixed(d == null ? 2 : d); }
  function pct(v, d) { return v == null || isNaN(v) ? 'n/a' : (Number(v) * 100).toFixed(d == null ? 1 : d) + '%'; }
  function day(s) { return s ? String(s).slice(0, 10) : '?'; }
  function verdictPill(v) { return '<span class="pill ' + (v === 'PASSED' ? 'ai' : 'dn') + '">' + esc(v) + '</span>'; }

  function renderRows(list) {
    if (!list || !list.length) return '<div class="act-empty">No backtest has been recorded yet. Runs are produced on the server by the backtest command and listed here; this page cannot start one.</div>';
    return '<div class="scroll"><table class="tbl"><tr><th>#</th><th>Strategy</th><th>Market</th><th>Data</th><th>Trades</th><th>Net P&amp;L</th><th>Max DD</th><th>Result</th><th></th></tr>' +
      list.map(function (r) {
        return '<tr><td class="num">' + esc(r.id) + '</td><td>' + esc(r.strategy) + ' v' + esc(r.version) + '</td><td>' + esc(r.symbol) + ' ' + esc(r.timeframe) +
          '</td><td class="mono">' + esc(day(r.dataset_start)) + ' to ' + esc(day(r.dataset_end)) + '</td><td class="num">' + esc(r.trades) + '</td><td class="num">' + num(r.net_pnl) +
          '</td><td class="num">' + pct(r.max_drawdown_pct) + '</td><td>' + verdictPill(r.verdict) + ' <span class="pill warn">SIMULATED</span></td>' +
          '<td><button class="btn sm" data-bk="btdetail" data-id="' + esc(r.id) + '">Report</button></td></tr>';
      }).join('') + '</table></div>';
  }

  function section(title, body) { return '<div class="card"><h2>' + esc(title) + '</h2>' + body + '</div>'; }

  function renderDetail(d) {
    var g = d.official_gate || {}, out = [];
    out.push('<div class="row">' + verdictPill(d.verdict) + ' <span class="pill warn">SIMULATED</span> ' +
      (d.counts_for_promotion ? '<span class="pill ai">NEWEST RUN: counts for promotion</span>' : '<span class="pill">older run: does not count</span>') + '</div>');
    out.push('<p class="mut">' + esc(d.strategy) + ' v' + esc(d.version) + ' &middot; ' + esc(d.symbol) + ' ' + esc(d.timeframe) + ' &middot; data ' + esc(day(d.dataset_start)) + ' to ' + esc(day(d.dataset_end)) +
      ' &middot; recorded by ' + esc(d.created_by) + ' at ' + esc(d.created_at) + '</p>');
    out.push(section('Promotion gate (recomputed now, against the owner-approved limits)',
      '<div class="' + (g.ok ? 'note' : 'warnbox') + '"><b>' + (g.ok ? 'Meets every limit.' : 'Does not meet the limits:') + '</b>' +
      ((g.reasons || []).length ? '<ul>' + g.reasons.map(function (x) { return '<li>' + esc(x) + '</li>'; }).join('') + '</ul>' : '') +
      ((g.notes || []).length ? '<ul>' + g.notes.map(function (x) { return '<li>' + esc(x) + '</li>'; }).join('') + '</ul>' : '') + '</div>'));
    var m = d.metrics || {};
    out.push(section('Result (after costs)', '<dl class="kv"><dt>Closed trades</dt><dd>' + esc(m.trades) + '</dd><dt>Net P&amp;L</dt><dd>' + num(m.net_pnl) + '</dd><dt>Return</dt><dd>' + pct(m.total_return_pct) +
      '</dd><dt>Win rate</dt><dd>' + pct(m.win_rate) + '</dd><dt>Profit factor</dt><dd>' + num(m.profit_factor) + '</dd><dt>Expectancy / trade</dt><dd>' + num(m.expectancy) +
      '</dd><dt>Realised reward/risk</dt><dd>' + num(m.payoff_ratio) + '</dd><dt>Max drawdown</dt><dd>' + pct(m.max_drawdown_pct) + '</dd><dt>Longest losing streak</dt><dd>' + esc(m.max_consecutive_losses) +
      '</dd><dt>Time in market</dt><dd>' + pct(m.exposure_pct) + '</dd><dt>Fees</dt><dd>' + num(m.fees) + '</dd><dt>Spread + slippage cost</dt><dd>' + num(m.execution_cost) + '</dd></dl>'));
    var st = d.stress || {}, keys = Object.keys(st).sort(function (a, b) { return a - b; });
    out.push(section('If costs were higher', '<table class="tbl"><tr><th>Costs</th><th>Net P&amp;L</th><th>Trades</th></tr>' + keys.map(function (k) {
      return '<tr><td>' + esc(k) + 'x</td><td class="num">' + num(st[k].net_pnl) + '</td><td class="num">' + esc(st[k].trades) + '</td></tr>'; }).join('') + '</table>'));
    var i = d.in_sample || {}, o = d.out_of_sample || {};
    out.push(section('Held-out data', '<dl class="kv"><dt>Earlier part: expectancy</dt><dd>' + num(i.expectancy) + ' (' + esc(i.trades) + ' trades)</dd><dt>Held-out last part: expectancy</dt><dd>' +
      num(o.expectancy) + ' (' + esc(o.trades) + ' trades)</dd></dl><p class="mut" style="margin:6px 0 0">The system does not tune any parameter yet, so this checks consistency, not over-fitting.</p>'));
    out.push(section('Equal consecutive periods', '<div class="row">' + (d.periods || []).map(function (p, n) {
      return '<span class="pill ' + (p.net_pnl > 0 ? 'ai' : 'dn') + '">' + (n + 1) + ': ' + num(p.net_pnl) + ' (' + esc(p.trades) + ')</span>'; }).join(' ') + '</div>'));
    if (d.monte_carlo) out.push(section('Same trades, shuffled order', '<p class="mut" style="margin:0">Drawdown over ' + esc(d.monte_carlo.runs) + ' shuffles: median ' + pct(d.monte_carlo.p50) + ', 95th percentile ' + pct(d.monte_carlo.p95) + ', worst ' + pct(d.monte_carlo.worst) + '.</p>'));
    out.push(section('Weaknesses and warnings', (d.weaknesses || []).length ? '<ul>' + d.weaknesses.map(function (w) { return '<li>' + esc(w) + '</li>'; }).join('') + '</ul>' : '<span class="mut">None raised. One run is still not proof.</span>'));
    out.push(section('Identity (so the exact run can be checked again)', '<dl class="kv"><dt>Data fingerprint</dt><dd class="mono">' + esc(d.dataset_hash) + '</dd><dt>Result digest</dt><dd class="mono">' + esc(d.digest) +
      '</dd><dt>Code version</dt><dd class="mono">' + esc(d.code_version) + '</dd><dt>Thresholds approved by</dt><dd>' + esc((d.thresholds || {}).approved_by || 'nobody (placeholder)') + '</dd></dl>'));
    out.push('<div class="warnbox"><b>Limits</b><ul>' + (d.limits || []).map(function (x) { return '<li>' + esc(x) + '</li>'; }).join('') + '</ul></div>');
    return out.join('');
  }

  function describeError(status, body) {
    if (status === 401) return 'Your session has expired. Sign in again.';
    if (status === 403) return 'Your role cannot view backtest records.';
    if (status === 404) return 'That run does not exist.';
    return 'The server could not return the backtest records (HTTP ' + status + ').';
  }
  return { renderRows: renderRows, renderDetail: renderDetail, describeError: describeError, esc: esc };
});
