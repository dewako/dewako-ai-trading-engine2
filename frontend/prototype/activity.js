/* DEWAKO Live Activity: pure view-model logic (no DOM, no network). Loaded by index.html and unit-tested under Node.
   Nothing here creates events: it only buffers, de-duplicates, orders, filters and renders what the backend sent. */
(function (root, factory) { if (typeof module === 'object' && module.exports) module.exports = factory(); else root.DewakoActivity = factory(); })(this, function () {
  'use strict';
  var MAX_BUFFER = 500, MAX_PENDING = 200, STALE_MS = 45000, MAX_FRAME = 65536;

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"'`]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;', '`': '&#96;' }[c]; });
  }
  function cmp(a, b) { return a.ts < b.ts ? 1 : a.ts > b.ts ? -1 : a.event_id < b.event_id ? 1 : a.event_id > b.event_id ? -1 : 0; }   // newest first
  function isCritical(e) { return e.severity === 'ERROR' || e.severity === 'CRITICAL' || e.status === 'UNKNOWN' || e.reconciliation_required === true; }
  function validEvent(e) { return !!e && typeof e === 'object' && typeof e.event_id === 'string' && typeof e.ts === 'string' && typeof e.severity === 'string' && typeof e.summary === 'string'; }

  /* Incremental Server-Sent-Events parser: tolerant of arbitrary chunk boundaries, bounded per frame. */
  function SSEParser() { this.buf = ''; this.dropped = 0; }
  SSEParser.prototype.feed = function (chunk) {
    this.buf += chunk.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
    var out = [], i;
    while ((i = this.buf.indexOf('\n\n')) !== -1) {
      var raw = this.buf.slice(0, i); this.buf = this.buf.slice(i + 2);
      if (raw.length > MAX_FRAME) { this.dropped++; continue; }
      var f = { event: 'message', data: '', id: null, comment: false }, has = false;
      raw.split('\n').forEach(function (line) {
        if (line.charAt(0) === ':') { f.comment = true; has = true; return; }
        var k = line.indexOf(':'); var name = k === -1 ? line : line.slice(0, k); var val = k === -1 ? '' : line.slice(k + 1).replace(/^ /, '');
        if (name === 'event') { f.event = val; has = true; } else if (name === 'data') { f.data += (f.data ? '\n' : '') + val; has = true; }
        else if (name === 'id') { f.id = val; has = true; } else if (name === 'retry') { f.retry = parseInt(val, 10); has = true; }
      });
      if (has) out.push(f);
    }
    if (this.buf.length > MAX_FRAME * 2) { this.buf = ''; this.dropped++; }       // an unterminated giant frame is discarded, not buffered forever
    return out;
  };

  function backoff(attempt, rnd) { var base = Math.min(30000, 1000 * Math.pow(2, Math.max(0, attempt))); return Math.round(base * (0.75 + 0.25 * (rnd == null ? Math.random() : rnd))); }

  function Model(opts) {
    opts = opts || {};
    this.now = opts.now || Date.now; this.max = opts.max || MAX_BUFFER;
    this.events = []; this.seen = Object.create(null); this.pending = []; this.paused = false;
    this.conn = 'idle'; this.connInfo = ''; this.lastSignal = null; this.loaded = false; this.error = null; this.denied = false; this.cursor = null;
    this.duplicates = 0; this.rejected = 0; this.lastStatus = null;
  }
  Model.prototype._merge = function (list) {
    var added = 0, self = this;
    list.forEach(function (e) { if (self.seen[e.event_id]) { self.duplicates++; return; } self.seen[e.event_id] = 1; self.events.push(e); added++; });
    if (added) {
      this.events.sort(cmp);
      if (this.events.length > this.max) { var drop = this.events.splice(this.max); drop.forEach(function (e) { delete self.seen[e.event_id]; }); }
    }
    return added;
  };
  /* history=true: a page loaded by the user; live=true: pushed by the stream (held back, not dropped, while the view is paused). */
  Model.prototype.ingest = function (list, opts) {
    opts = opts || {}; var good = [], self = this;
    (Array.isArray(list) ? list : []).forEach(function (e) { if (validEvent(e)) good.push(e); else self.rejected++; });
    if (opts.live) this.lastSignal = this.now();
    if (opts.live && this.paused) {
      var self2 = this;
      good.forEach(function (e) { if (!self2.seen[e.event_id] && !self2.pending.some(function (p) { return p.event_id === e.event_id; })) self2.pending.push(e); else self2.duplicates++; });
      if (this.pending.length > MAX_PENDING) {                                   // bounded: keep every critical item, drop the oldest non-critical ones
        var crit = this.pending.filter(isCritical), rest = this.pending.filter(function (e) { return !isCritical(e); }).sort(cmp);
        this.pending = crit.concat(rest.slice(0, Math.max(0, MAX_PENDING - crit.length)));
      }
      return { added: 0, held: good.length };
    }
    this.loaded = true; this.error = null;
    return { added: this._merge(good), held: 0 };
  };
  Model.prototype.reset = function () { this.events = []; this.seen = Object.create(null); this.pending = []; this.loaded = false; this.error = null; };   // keeps connection state and stream cursor
  Model.prototype.pause = function () { this.paused = true; };            // visual only: the stream keeps being read and buffered
  Model.prototype.resume = function () { this.paused = false; var p = this.pending; this.pending = []; return this._merge(p); };
  Model.prototype.criticalPending = function () { return this.pending.filter(isCritical).length; };
  Model.prototype.setConn = function (state, info) { this.conn = state; this.connInfo = info || ''; if (state === 'live') this.lastSignal = this.now(); if (state === 'unauthorized') this.denied = true; };
  Model.prototype.touch = function () { this.lastSignal = this.now(); };
  Model.prototype.isStale = function () { return this.conn === 'live' && this.lastSignal != null && this.now() - this.lastSignal > STALE_MS; };
  Model.prototype.setCursor = function (c) { if (c) this.cursor = c; };
  Model.prototype.fail = function (msg) { this.error = msg || 'backend error'; };
  Model.prototype.view = function () {
    if (this.denied) return 'denied';
    if (!this.loaded && this.error) return 'error';
    if (!this.loaded) return 'loading';
    if (this.conn === 'disconnected' || this.conn === 'reconnecting') return 'disconnected';
    if (this.isStale()) return 'stale';
    if (this.error) return 'error';
    return this.events.length ? 'ready' : 'empty';
  };
  Model.prototype.visible = function (f) {
    f = f || {}; var q = (f.q || '').toLowerCase();
    return this.events.filter(function (e) {
      return (!f.category || e.category === f.category) && (!f.severity || e.severity === f.severity) && (!f.status || e.status === f.status) &&
        (!f.strategy || (e.strategy || '').toLowerCase() === f.strategy.toLowerCase()) && (!f.since || e.ts >= f.since) && (!f.until || e.ts <= f.until) &&
        (!q || [e.summary, e.type, e.correlation_id, e.strategy, e.instrument, e.related_id, e.status].join(' ').toLowerCase().indexOf(q) !== -1);
    });
  };

  var SEV = { INFO: 'Info', WARNING: 'Warning', ERROR: 'Error', CRITICAL: 'Critical' };
  var LEVELS = { RUNNING: 'Running', IDLE: 'Idle', DEGRADED: 'Degraded', ERROR: 'Error' };
  function timeLabels(ts) {
    var d = new Date(ts); if (isNaN(d)) return { utc: String(ts).slice(0, 25), local: '' };
    return { utc: d.toISOString().replace('T', ' ').replace(/\.\d+Z$/, ' UTC'), local: d.toLocaleString() };
  }
  /* Everything interpolated is escaped. Severity and status are text as well as colour. An unknown outcome is never styled as success. */
  function renderEvent(e, tz) {
    var t = timeLabels(e.ts);
    var unknown = e.status === 'UNKNOWN' || e.reconciliation_required === true;
    var cls = 'ev sev-' + escapeHtml(String(e.severity).toLowerCase()) + (unknown ? ' unknown' : '');
    return '<li class="' + cls + '" data-id="' + escapeHtml(e.event_id) + '" tabindex="0" role="button" aria-label="' + escapeHtml(SEV[e.severity] || e.severity) + ': ' + escapeHtml(e.summary) + '. Open details">' +
      '<span class="ev-dot" aria-hidden="true"></span><div class="ev-main"><div class="ev-top"><span class="ev-sev">' + escapeHtml(SEV[e.severity] || e.severity) + '</span>' +
      '<span class="ev-cat">' + escapeHtml(e.category) + '</span>' +
      (unknown ? '<span class="ev-unk">UNKNOWN &middot; RECONCILIATION REQUIRED</span>' : '<span class="ev-status">' + escapeHtml(e.status) + '</span>') +
      '<time class="ev-time mono" datetime="' + escapeHtml(e.ts) + '" title="' + escapeHtml(tz === 'utc' ? t.local : t.utc) + '">' + escapeHtml(tz === 'utc' ? t.utc : (t.local || t.utc)) + '</time></div>' +
      '<div class="ev-sum">' + escapeHtml(e.summary) + '</div><div class="ev-meta mono">' +
      [e.strategy ? 'strategy ' + e.strategy + (e.strategy_version ? ' v' + e.strategy_version : '') : '', e.instrument || '', e.environment || '', e.correlation_id ? 'cid ' + e.correlation_id : '']
        .filter(Boolean).map(escapeHtml).join(' &middot; ') + '</div></div></li>';
  }
  function renderDetails(e) {
    var t = timeLabels(e.ts), rows = [['Event ID', e.event_id], ['Time (UTC)', t.utc], ['Time (local)', t.local], ['Type', e.type], ['Category', e.category], ['Severity', e.severity], ['Status', e.status],
      ['Source', e.source], ['Actor', e.actor], ['Environment', e.environment], ['Instrument', e.instrument], ['Adapter / provider', e.adapter], ['Strategy', e.strategy ? e.strategy + (e.strategy_version ? ' v' + e.strategy_version : '') : ''],
      ['Strategy fingerprint', e.strategy_fingerprint], ['Related', e.related_id], ['Correlation ID', e.correlation_id], ['Schema version', e.schema_version]];
    var kv = rows.filter(function (r) { return r[1] !== null && r[1] !== undefined && r[1] !== ''; }).map(function (r) { return '<dt>' + escapeHtml(r[0]) + '</dt><dd class="mono">' + escapeHtml(r[1]) + '</dd>'; }).join('');
    return '<h3>' + escapeHtml(e.summary) + '</h3>' + (e.reconciliation_required ? '<p class="ev-unk">The outcome of this order is UNKNOWN. Reconciliation is required. This screen cannot retry, cancel or submit anything.</p>' : '') +
      '<dl class="kv">' + kv + '</dl><h4>Details (sanitized by the server)</h4><pre class="mono">' + escapeHtml(JSON.stringify(e.details || {}, null, 2)) + '</pre>';
  }
  function statusView(s) {
    if (!s) return { level: 'UNKNOWN', label: 'Unknown', reasons: ['status not loaded'] };
    return { level: s.level, label: LEVELS[s.level] || 'Unknown', reasons: Array.isArray(s.reasons) ? s.reasons : [], unresolved: s.unresolved_orders || 0, killSwitch: !!s.kill_switch, live: s.live_trading };
  }
  function queryString(filters, cursor, limit) {
    var p = [];
    ['category', 'severity', 'status', 'strategy', 'q', 'since', 'until'].forEach(function (k) { if (filters && filters[k]) p.push(k + '=' + encodeURIComponent(filters[k])); });
    if (cursor) p.push('cursor=' + encodeURIComponent(cursor)); p.push('limit=' + (limit || 50));
    return p.join('&');
  }
  return { Model: Model, SSEParser: SSEParser, escapeHtml: escapeHtml, renderEvent: renderEvent, renderDetails: renderDetails, statusView: statusView, queryString: queryString, backoff: backoff,
           isCritical: isCritical, validEvent: validEvent, timeLabels: timeLabels, cmp: cmp, MAX_BUFFER: MAX_BUFFER, MAX_PENDING: MAX_PENDING, STALE_MS: STALE_MS };
});
