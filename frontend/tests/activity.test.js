const test = require('node:test'), assert = require('node:assert/strict');
const A = require('../prototype/activity.js'), P = require('../prototype/packages.js');
let n = 0;
const ev = (o = {}) => Object.assign({ schema_version: 1, event_id: 'audit:' + (++n), ts: '2026-01-01T00:00:' + String(10 + (n % 50)).padStart(2, '0') + '+00:00', type: 'X', category: 'signal', severity: 'INFO', summary: 's' + n,
  status: 'OK', source: 'audit', correlation_id: 'c', details: {}, reconciliation_required: false }, o);
const clock = () => { const c = { t: 1000 }; c.now = () => c.t; return c; };

test('ingest de-duplicates by event_id, rejects malformed events and keeps newest-first order', () => {
  const m = new A.Model(); const a = ev({ ts: '2026-01-01T00:00:01+00:00' }), b = ev({ ts: '2026-01-01T00:00:09+00:00' });
  assert.equal(m.ingest([a, b, a, { nope: 1 }, null, 5], {}).added, 2);
  assert.deepEqual(m.events.map(e => e.event_id), [b.event_id, a.event_id]);
  assert.equal(m.duplicates, 1); assert.equal(m.rejected, 3);
  assert.equal(m.ingest([a, b], { live: true }).added, 0);               // a replay after reconnect adds nothing
  assert.equal(m.events.length, 2);
  const same = ev({ ts: '2026-01-01T00:00:09+00:00', event_id: 'audit:zzz' });    // equal timestamps are ordered by id, deterministically
  m.ingest([same]); assert.equal(m.events[0].event_id, 'audit:zzz');
});

test('the buffer is bounded and the oldest events are dropped first', () => {
  const m = new A.Model({ max: 50 });
  const list = Array.from({ length: 120 }, (_, i) => ev({ event_id: 'audit:b' + i, ts: new Date(Date.UTC(2026, 0, 1, 0, 0, i)).toISOString() }));
  m.ingest(list); assert.equal(m.events.length, 50);
  assert.equal(m.events[0].event_id, 'audit:b119'); assert.equal(m.events[49].event_id, 'audit:b70');
  assert.equal(Object.keys(m.seen).length, 50);
});

test('pausing is visual only: events keep arriving into a bounded hold, critical ones are never dropped and are counted', () => {
  const m = new A.Model(); m.ingest([ev()]); m.pause();
  const crit = ev({ severity: 'ERROR', status: 'UNKNOWN', reconciliation_required: true });
  assert.deepEqual(m.ingest([crit, ev()], { live: true }), { added: 0, held: 2 });
  assert.equal(m.events.length, 1); assert.equal(m.criticalPending(), 1);
  for (let i = 0; i < 400; i++) m.ingest([ev()], { live: true });
  assert.ok(m.pending.length <= A.MAX_PENDING); assert.equal(m.criticalPending(), 1);      // still holds the critical one
  const added = m.resume(); assert.ok(added > 0); assert.equal(m.paused, false); assert.equal(m.pending.length, 0);
  assert.ok(m.events.some(e => e.event_id === crit.event_id));
});

test('view states: loading, error, denied, empty, ready, disconnected, stale', () => {
  const c = clock(); const m = new A.Model({ now: c.now });
  assert.equal(m.view(), 'loading');
  m.fail('x'); assert.equal(m.view(), 'error');
  m.ingest([]); assert.equal(m.view(), 'empty');
  m.ingest([ev()]); m.setConn('live'); assert.equal(m.view(), 'ready');
  c.t += A.STALE_MS + 1; assert.equal(m.view(), 'stale');
  m.touch(); assert.equal(m.view(), 'ready');
  m.setConn('reconnecting'); assert.equal(m.view(), 'disconnected'); m.setConn('disconnected'); assert.equal(m.view(), 'disconnected');
  m.setConn('unauthorized'); assert.equal(m.view(), 'denied');
});

test('SSE parser handles arbitrary chunking, comments, multi-line data, CRLF and oversize frames', () => {
  const p = new A.SSEParser(); const raw = 'retry: 3000\nevent: hello\ndata: {"a":1}\n\n: keepalive\n\nid: abc\nevent: activity\ndata: {"x":\n\n';
  const out = []; for (const ch of raw) out.push(...p.feed(ch));                   // one character at a time
  assert.equal(out.length, 3); assert.equal(out[0].event, 'hello'); assert.equal(out[0].retry, 3000); assert.equal(out[1].comment, true);
  assert.equal(out[2].id, 'abc'); assert.equal(out[2].event, 'activity');
  const q = new A.SSEParser(); assert.equal(q.feed('event: a\r\ndata: 1\r\n\r\n')[0].data, '1');
  const big = new A.SSEParser(); big.feed('data: ' + 'x'.repeat(200000)); assert.ok(big.buf.length === 0 && big.dropped >= 1);
  const inj = new A.SSEParser().feed('event: activity\ndata: {"summary":"a\\nevent: closed"}\n\n'); assert.equal(inj.length, 1);   // escaped JSON cannot add a frame
});

test('rendering escapes every field and never styles an unknown outcome as success', () => {
  const xss = '<img src=x onerror=alert(1)>"\'`';
  const html = A.renderEvent(ev({ summary: xss, strategy: xss, instrument: xss, correlation_id: xss, category: xss, status: xss, event_id: 'audit:' + xss, severity: 'ERROR' }));
  assert.ok(!/<img/i.test(html) && !/onerror=alert\(1\)>/.test(html.replace(/&lt;img[^]*?&gt;/g, '')) && html.includes('&lt;img'));
  assert.ok(!html.includes('"\'`'));
  const d = A.renderDetails(ev({ summary: xss, details: { k: xss } })); assert.ok(!/<img/i.test(d));
  const u = A.renderEvent(ev({ severity: 'ERROR', status: 'UNKNOWN', reconciliation_required: true, summary: 'Order 7 BTC: SUBMITTING → UNKNOWN' }));
  assert.ok(u.includes('UNKNOWN &middot; RECONCILIATION REQUIRED') && u.includes('sev-error') && u.includes('unknown') && !/FILLED|ev-up|success/i.test(u));
  assert.ok(A.renderDetails(ev({ reconciliation_required: true })).includes('cannot retry, cancel or submit'));
  assert.ok(A.renderEvent(ev({ severity: 'WARNING' })).includes('Warning'));                  // severity is text, not only colour
});

test('reset clears events but keeps the stream cursor and the connection state; UTC/local display toggles', () => {
  const m = new A.Model(); m.ingest([ev()]); m.setCursor('cur1'); m.setConn('live'); m.reset();
  assert.equal(m.events.length, 0); assert.equal(m.cursor, 'cur1'); assert.equal(m.conn, 'live'); assert.equal(m.view(), 'loading');
  const e = ev({ ts: '2026-03-04T05:06:07+00:00' });
  assert.ok(A.renderEvent(e, 'utc').includes('>2026-03-04 05:06:07 UTC</time>')); assert.ok(!A.renderEvent(e, 'local').includes('>2026-03-04 05:06:07 UTC</time>'));
});

test('filters, query string, time labels and backoff', () => {
  const m = new A.Model(); m.ingest([ev({ category: 'order', severity: 'ERROR', strategy: 'Alpha' }), ev({ category: 'auth', status: 'DENIED' }), ev({ summary: 'login amy' })]);
  assert.equal(m.visible({ category: 'order' }).length, 1); assert.equal(m.visible({ severity: 'ERROR' }).length, 1); assert.equal(m.visible({ strategy: 'alpha' }).length, 1);
  assert.equal(m.visible({ q: 'LOGIN' }).length, 1); assert.equal(m.visible({ status: 'DENIED' }).length, 1); assert.equal(m.visible({}).length, 3);
  assert.equal(A.queryString({ q: 'a&b=c', category: 'order' }, 'cur sor', 25), 'category=order&q=a%26b%3Dc&cursor=cur%20sor&limit=25');
  assert.ok(!A.queryString({ evil: 'x' }, null).includes('evil'));
  const t = A.timeLabels('2026-03-04T05:06:07.123+00:00'); assert.equal(t.utc, '2026-03-04 05:06:07 UTC'); assert.ok(t.local.length > 0);
  assert.equal(A.timeLabels('garbage').local, '');
  for (let a = 0; a < 12; a++) { const b = A.backoff(a, 1); assert.ok(b <= 30000 && b >= 750); }
  assert.ok(A.backoff(0, 0) < A.backoff(5, 0)); assert.equal(A.statusView(null).level, 'UNKNOWN'); assert.equal(A.statusView({ level: 'DEGRADED', reasons: ['x'] }).label, 'Degraded');
});

test('packages: client file check is advisory, base64 is exact, errors never show stack traces, labels match backend states', () => {
  assert.match(P.checkFile(null), /Choose/); assert.match(P.checkFile({ name: 'a.txt', size: 5 }), /\.zip/); assert.match(P.checkFile({ name: 'a.zip', size: 0 }), /empty/);
  assert.match(P.checkFile({ name: 'a.zip', size: P.MAX_BYTES + 1 }), /1 MiB/); assert.equal(P.checkFile({ name: 'A.ZIP', size: 10 }), null);
  const bytes = new Uint8Array(100000).map((_, i) => (i * 7) & 255); assert.equal(P.toBase64(bytes), Buffer.from(bytes).toString('base64'));
  assert.equal(P.describeError(0).kind, 'network'); assert.equal(P.describeError(401).kind, 'auth'); assert.equal(P.describeError(403).kind, 'denied');
  assert.equal(P.describeError(422, { detail: { code: 'HASH_MISMATCH', detail: 'parameters.json' } }).text, 'HASH_MISMATCH: parameters.json');
  const e500 = P.describeError(500, { detail: 'Traceback (most recent call last): secret' }); assert.ok(!/Traceback|secret/.test(e500.text) && /not have been imported/.test(e500.text));
  const states = ['IMPORTED', 'VALIDATION_FAILED', 'VALIDATED', 'BACKTEST_ELIGIBLE', 'PAPER_ELIGIBLE', 'TESTNET_ELIGIBLE', 'DISABLED', 'QUARANTINED'];
  assert.deepEqual(Object.keys(P.LABELS).sort(), states.slice().sort());                       // exactly the backend lifecycle: nothing invented, nothing missing
  assert.ok(!Object.values(P.LABELS).some(l => /release/i.test(l[0])));
  assert.equal(P.nextState('VALIDATED'), 'BACKTEST_ELIGIBLE'); assert.equal(P.nextState('TESTNET_ELIGIBLE'), null); assert.equal(P.nextState('QUARANTINED'), null);
  const row = P.renderPackageRow({ id: 1, name: '<b>x</b>', version: '1.0', state: 'VALIDATED', fingerprint: 'a'.repeat(64), imported_by: '<i>', state_reason: '' }); assert.ok(!row.includes('<b>x') && !row.includes('<i>'));
  const det = P.renderDetail({ id: 2, name: 'N', version: '1.0', state: 'VALIDATED', summary: { runtime: 'declarative-rules', schema_version: 1, instruments: ['XAUUSD'], timeframes: ['1m'], file_count: 4 }, findings: [{ code: '<x>', detail: 'd' }] }, true);
  assert.ok(det.includes('Promote to Eligible: backtest') && det.includes('Live trading is locked') && !det.includes('<x>'));
  assert.ok(!P.renderDetail({ id: 2, state: 'VALIDATED' }, false).includes('data-pk="promote"'));        // no promote button without permission
  assert.ok(!P.renderDetail({ id: 3, state: 'VALIDATION_FAILED' }, true).includes('data-pk="promote"'));
});
