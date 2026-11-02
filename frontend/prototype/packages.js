/* DEWAKO Strategy Manager: pure helpers for the package import workflow (no DOM, no network). All real validation happens on the server;
   the checks here only save a pointless upload and never decide whether a package is accepted. */
(function (root, factory) { if (typeof module === 'object' && module.exports) module.exports = factory(); else root.DewakoPackages = factory(); })(this, function () {
  'use strict';
  var MAX_BYTES = 1024 * 1024;
  var LABELS = {   // exactly the backend states: nothing is invented
    IMPORTED: ['Imported', 'warn', 'Stored, validation not yet complete.'],
    VALIDATION_FAILED: ['Rejected', 'dn', 'Failed server validation. It cannot be promoted.'],
    VALIDATED: ['Imported / Inactive', '', 'Validated and stored. Not registered, not selected, not trading.'],
    BACKTEST_ELIGIBLE: ['Eligible: backtest', 'ai', 'Registered for backtesting only.'],
    PAPER_ELIGIBLE: ['Eligible: paper', 'ai', 'May be assigned to paper bots. Signals still pass risk and authorization.'],
    TESTNET_ELIGIBLE: ['Eligible: testnet (label only)', 'ai', 'A label: this build cannot place testnet orders.'],
    DISABLED: ['Disabled', 'warn', 'Unregistered. Re-validate to bring it back.'],
    QUARANTINED: ['Quarantined', 'dn', 'Integrity check failed. An administrator must release it.']
  };
  var NEXT = { VALIDATED: 'BACKTEST_ELIGIBLE', BACKTEST_ELIGIBLE: 'PAPER_ELIGIBLE', PAPER_ELIGIBLE: 'TESTNET_ELIGIBLE' };   // the backend promotes one step at a time
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"'`]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;', '`': '&#96;' }[c]; }); }
  function label(state) { return LABELS[state] || [String(state), '', 'Unknown state reported by the server.']; }
  function nextState(state) { return NEXT[state] || null; }
  function checkFile(f) {
    if (!f) return 'Choose a .zip file.';
    if (!/\.zip$/i.test(f.name || '')) return 'The file must have a .zip extension.';
    if (!f.size) return 'The file is empty.';
    if (f.size > MAX_BYTES) return 'The file is larger than the 1 MiB limit.';
    return null;
  }
  function toBase64(bytes) {                       // bytes: Uint8Array. Chunked so large arrays do not overflow the call stack.
    var s = '', i, CH = 0x8000;
    for (i = 0; i < bytes.length; i += CH) s += String.fromCharCode.apply(null, bytes.subarray(i, i + CH));
    return (typeof btoa === 'function') ? btoa(s) : Buffer.from(s, 'binary').toString('base64');
  }
  /* Turn an HTTP result into text for the user. Server detail is shown (it is already bounded and sanitized); stack traces never are. */
  function describeError(status, body) {
    var d = body && body.detail;
    if (status === 0) return { kind: 'network', text: 'Could not reach the server. The package was not imported.' };
    if (status === 401) return { kind: 'auth', text: 'Your session has expired. Sign in again.' };
    if (status === 403) return { kind: 'denied', text: 'Your role is not allowed to do this. Nothing was changed.' };
    if (status === 409 || status === 422) {
      if (d && typeof d === 'object') return { kind: 'rejected', code: d.code || '', text: (d.code ? d.code + ': ' : '') + (d.detail || 'rejected') };
      return { kind: 'rejected', text: String(d || 'rejected').slice(0, 300) };
    }
    if (status === 413) return { kind: 'rejected', text: 'The upload is too large.' };
    if (status === 429) return { kind: 'busy', text: 'Too many requests. Wait a moment and try again.' };
    if (status >= 500) return { kind: 'server', text: 'The server hit an error. Check the Live Activity feed; the package may not have been imported.' };
    return { kind: 'unknown', text: 'Unexpected response (' + status + ').' };
  }
  function renderPackageRow(p) {
    var l = label(p.state);
    return '<tr data-pid="' + esc(p.id) + '"><td class="mono">' + esc(p.id) + '</td><td><b>' + esc(p.name || '(rejected upload)') + '</b></td><td class="mono">' + esc(p.version || '') + '</td>' +
      '<td><span class="pill ' + esc(l[1]) + '" title="' + esc(l[2]) + '">' + esc(l[0].toUpperCase()) + '</span>' + (p.state_reason && p.state !== 'VALIDATED' ? '<div class="mut" style="font-size:11px">' + esc(p.state_reason) + '</div>' : '') + '</td>' +
      '<td class="mono" title="' + esc(p.fingerprint || '') + '">' + esc((p.fingerprint || '').slice(0, 12)) + '</td><td class="mono mut">' + esc(p.imported_by) + '</td>' +
      '<td><button class="btn sm" data-pk="detail" data-pid="' + esc(p.id) + '">Details</button></td></tr>';
  }
  function renderDetail(p, canPromote) {
    var l = label(p.state), s = p.summary || {}, nx = nextState(p.state);
    var kv = [['Name', p.name], ['Version', p.version], ['Lifecycle', l[0]], ['Meaning', l[2]], ['Package SHA-256 (computed by the server)', p.package_sha256], ['Strategy fingerprint', p.fingerprint],
      ['Format', s.runtime ? s.runtime + ' (schema v' + s.schema_version + ')' : ''], ['Instruments', (s.instruments || []).join(', ')], ['Timeframes', (s.timeframes || []).join(', ')],
      ['Files in package', s.file_count], ['Imported by', p.imported_by], ['Created', p.created_at], ['Updated', p.updated_at]]
      .filter(function (r) { return r[1] !== undefined && r[1] !== null && r[1] !== ''; }).map(function (r) { return '<dt>' + esc(r[0]) + '</dt><dd class="mono">' + esc(r[1]) + '</dd>'; }).join('');
    var f = (p.findings || []).map(function (x) { return '<li><b>' + esc(x.code) + '</b> ' + esc(x.detail) + '</li>'; }).join('');
    return '<h3>' + esc(p.name || 'Rejected upload') + ' ' + esc(p.version || '') + '</h3><dl class="kv">' + kv + '</dl>' + (f ? '<h4>Validation findings</h4><ul>' + f + '</ul>' : '') +
      '<p class="mut">Importing never enables or selects a package. Promotion is one step at a time, by a person with permission; signals still go through risk checks and execution authorization. Live trading is locked.</p>' +
      '<div class="row">' + (nx && canPromote ? '<button class="btn pri" data-pk="promote" data-pid="' + esc(p.id) + '" data-to="' + esc(nx) + '">Promote to ' + esc(label(nx)[0]) + '</button>' : '') +
      (['BACKTEST_ELIGIBLE', 'PAPER_ELIGIBLE', 'TESTNET_ELIGIBLE', 'VALIDATED'].indexOf(p.state) !== -1 && canPromote ? '<button class="btn" data-pk="promote" data-pid="' + esc(p.id) + '" data-to="DISABLED">Disable</button>' : '') +
      '<button class="btn" data-act="close">Close</button></div>';
  }
  return { checkFile: checkFile, toBase64: toBase64, describeError: describeError, label: label, nextState: nextState, renderPackageRow: renderPackageRow, renderDetail: renderDetail, MAX_BYTES: MAX_BYTES, esc: esc, LABELS: LABELS };
});
