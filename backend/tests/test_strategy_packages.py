"""Phase 4.2: strategy ZIP packages. Every archive is built in memory; nothing here touches the network or runs package content."""
import hashlib
import io
import json
import re
import stat
import struct
import zipfile

import pytest

from dewako.persistence.audit import AuditLog
from dewako.strategies import base as sbase
from dewako.strategy_packages import archive as A
from dewako.strategy_packages.archive import PackageRejected
from dewako.strategy_packages.manager import PackageManager, TransitionRefused
from hardening_helpers import *
from stage3b_evidence import record_run

TRADER = {"user_id": 2, "username": "trader", "role": "TRADER"}
ANALYST = {"user_id": 3, "username": "analyst", "role": "ANALYST"}
VIEWER = {"user_id": 4, "username": "viewer", "role": "VIEWER"}
RULES = {"entry_long": [{"type": "price_above_ema", "length": 20}, {"type": "rsi_below", "length": 14, "value": 70}], "atr_len": 14, "atr_stop": 2.0}
_n = [0]


def _name():
    _n[0] += 1
    return f"Pkg Gold {_n[0]}-{id(_n)%9973}"


def build(name=None, version="1.0", *, rules=None, params=None, meta=None, extra=None, manifest_over=None, drop=(), raw=None, tamper=None):
    """Return zip bytes for a valid package, then apply requested defects."""
    name = name or _name()
    files = {"strategy/rules.json": json.dumps({"name": name, "version": version, **(rules or RULES)}),
             "parameters.json": json.dumps(params if params is not None else {"atr_stop": 2.5}),
             "metadata.json": json.dumps(meta if meta is not None else {"author_note": "test"})}
    files.update(extra or {})
    for d in drop:
        files.pop(d, None)
    enc = {k: (v if isinstance(v, bytes) else v.encode()) for k, v in files.items()}
    man = {"schema_version": 1, "name": name, "version": version, "runtime": "declarative-rules", "entry": "strategy/rules.json",
           "description": "xau scalp", "author": "me", "instruments": ["XAUUSD"], "timeframes": ["1m", "5m"], "dependencies": [],
           "requested_capabilities": [], "files": {k: hashlib.sha256(v).hexdigest() for k, v in enc.items()}}
    man.update(manifest_over or {})
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        if raw is None:
            z.writestr("manifest.json", json.dumps(man))
        else:
            z.writestr("manifest.json", raw)
        for k, v in enc.items():
            z.writestr(k, tamper.get(k, v) if tamper else v)
    return out.getvalue()


def raw_zip(entries):
    """entries: list of (name_or_ZipInfo, bytes). No validation, for hostile layouts."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for n, b in entries:
            z.writestr(n, b)
    return out.getvalue()


def mgr():
    db, _ = tmp_db()
    return PackageManager(db, AuditLog(db)), db


def rejected(data, code):
    with pytest.raises(PackageRejected) as e:
        PackageManager.validate(data)
    assert e.value.code == code, (e.value.code, e.value.detail)


def _unreg(m):
    for pid in list(m._registered):
        m._unregister(pid)


# ------------------------------------------------------------------ happy path and lifecycle
def test_valid_package_imports_as_validated_and_is_not_registered_or_executable_yet():
    m, db = mgr()
    r = m.import_package(build("Gold Scalp A"), ADMIN, "c1")
    assert r["state"] == "VALIDATED" and r["name"] == "Gold Scalp A" and len(r["fingerprint"]) == 64
    assert ("Gold Scalp A", "1.0") not in sbase._REGISTRY                      # nothing runs until BACKTEST_ELIGIBLE
    with db.read() as c:
        acts = [x["action"] for x in c.execute("SELECT action FROM audit_log ORDER BY id").fetchall()]
    assert "STRATEGY_PACKAGE_IMPORT" in acts and acts.count("STRATEGY_PACKAGE_STATE") == 1


def test_lifecycle_ladder_is_one_step_at_a_time_and_registers_only_when_eligible():
    m, _ = mgr()
    try:
        pid = m.import_package(build("Gold Scalp B"), ADMIN)["id"]
        with pytest.raises(TransitionRefused):
            m.transition(pid, "PAPER_ELIGIBLE", ADMIN)                         # skipping a step
        assert m.transition(pid, "BACKTEST_ELIGIBLE", ADMIN)["state"] == "BACKTEST_ELIGIBLE"
        cls = sbase.get("Gold Scalp B", "1.0")
        assert cls.experimental is True and cls.supported_markets == ["XAUUSD"] and cls.supported_timeframes == ["1m", "5m"]
        assert cls.default_parameters["atr_stop"] == 2.5                       # parameters.json applied
        with pytest.raises(TransitionRefused):
            m.transition(pid, "PAPER_ELIGIBLE", TRADER)                        # since Stage 3B: no backtest evidence, no promotion
        record_run(m.runs, ANALYST, "Gold Scalp B", "1.0", m.get(pid)["fingerprint"])
        m.transition(pid, "PAPER_ELIGIBLE", TRADER)
        m.transition(pid, "TESTNET_ELIGIBLE", TRADER)
        m.transition(pid, "PAPER_ELIGIBLE", TRADER)                            # demotion allowed
        m.transition(pid, "DISABLED", TRADER)
        assert ("Gold Scalp B", "1.0") not in sbase._REGISTRY                  # disabling unregisters
        with pytest.raises(TransitionRefused):
            m.transition(pid, "PAPER_ELIGIBLE", TRADER)                        # must re-validate first
        m.transition(pid, "VALIDATED", TRADER)
    finally:
        _unreg(m)


def test_permissions_are_enforced_in_the_manager_not_only_in_the_api():
    m, _ = mgr()
    try:
        with pytest.raises(TransitionRefused):
            m.import_package(build(), VIEWER)
        pid = m.import_package(build(), ANALYST)["id"]                         # analysts may import and backtest-promote
        m.transition(pid, "BACKTEST_ELIGIBLE", ANALYST)
        with pytest.raises(TransitionRefused):
            m.transition(pid, "PAPER_ELIGIBLE", ANALYST)
        with pytest.raises(TransitionRefused):
            m.transition(pid, "QUARANTINED", ANALYST)
        m.transition(pid, "QUARANTINED", TRADER)
        with pytest.raises(TransitionRefused):
            m.transition(pid, "DISABLED", TRADER)                              # releasing quarantine is admin-only
        m.transition(pid, "DISABLED", ADMIN)
    finally:
        _unreg(m)


def test_imported_strategy_can_never_pass_the_live_gate_and_has_no_execution_access():
    m, db = mgr()
    try:
        name = _name()
        pid = m.import_package(build(name), ADMIN)["id"]
        for to in ("BACKTEST_ELIGIBLE", "PAPER_ELIGIBLE", "TESTNET_ELIGIBLE"):
            if to == "PAPER_ELIGIBLE":
                record_run(m.runs, ADMIN, name, "1.0", m.get(pid)["fingerprint"])
            m.transition(pid, to, ADMIN)
        safety = SafetyController(db)
        eid = make_live_ready(db, safety)
        g = safety.live_gate(exchange_id=eid, strategy_id=name, strategy_version="1.0", symbol="XAUUSD")
        assert not g.allowed                                                    # no RELEASED strategy_versions row is ever written by import
        with db.read() as c:
            assert c.execute("SELECT COUNT(*) FROM strategy_versions WHERE strategy_id=?", (name,)).fetchone()[0] == 0
    finally:
        _unreg(m)
    import dewako.strategy_packages as pkg, pathlib
    for f in pathlib.Path(pkg.__file__).parent.glob("*.py"):
        src = f.read_text()
        assert not re.search(r"^\s*(from|import)\s+dewako\.(exchanges|execution|risk|safety|paper_trading)", src, re.M), f.name
        assert not re.search(r"(?<![\w.])(eval|exec|compile|__import__|subprocess|importlib|os\.system|pickle)\s*\(", src), f.name
        assert not re.search(r"^\s*(from|import)\s+dewako\.(security|vault|api|runtime)", src, re.M), f.name


# ------------------------------------------------------------------ hostile archives
def test_unsafe_paths_are_rejected():
    for bad in ["../evil.json", "/etc/passwd", "strategy/../../x.json", "..\\evil.json", "C:/x.json", "a//b.json", "./strategy/rules.json",
                ".hidden.json", "strategy/.x.json", "name\x00.json", "tests/ünï.json"]:
        z = raw_zip([("manifest.json", "{}"), (bad, "{}")])
        with pytest.raises(PackageRejected) as e:
            A.open_package(z)
        # zipfile truncates a name at NUL before we see it, so that case surfaces as an unsupported entry; either way it is refused
        assert e.value.code == ("UNSUPPORTED_ENTRY" if "\x00" in bad else "UNSAFE_PATH"), (bad, e.value.code)


def test_symlink_special_files_duplicates_and_encryption_are_rejected():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        zi = zipfile.ZipInfo("strategy/rules.json"); zi.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(zi, "../../etc/passwd")
    with pytest.raises(PackageRejected) as e:
        A.open_package(out.getvalue())
    assert e.value.code == "SYMLINK"
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        zi = zipfile.ZipInfo("tests/a.json"); zi.external_attr = (stat.S_IFIFO | 0o644) << 16
        z.writestr(zi, "{}")
    with pytest.raises(PackageRejected) as e:
        A.open_package(out.getvalue())
    assert e.value.code == "SPECIAL_FILE"
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        d = raw_zip([("manifest.json", "{}"), ("parameters.json", "{}"), ("parameters.json", "{\"atr_len\":3}")])
    with pytest.raises(PackageRejected) as e:
        A.open_package(d)
    assert e.value.code == "DUPLICATE_PATH"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        d = raw_zip([("manifest.json", "{}"), ("Parameters.json", "{}"), ("parameters.json", "{}")])
    with pytest.raises(PackageRejected) as e:
        A.open_package(d)
    assert e.value.code in ("DUPLICATE_PATH", "UNSUPPORTED_ENTRY")             # a case variant is never an accepted name (Windows would collide it)
    z = bytearray(raw_zip([("manifest.json", "{}"), ("parameters.json", "{}")]))
    i = z.rfind(b"PK\x01\x02")                                              # last central-directory record: parameters.json
    z[i + 8] |= 0x1                                                         # general-purpose flag bit 0 = encrypted
    with pytest.raises(PackageRejected) as e:
        A.open_package(bytes(z))
    assert e.value.code == "ENCRYPTED_ENTRY"


def test_archive_bombs_and_size_limits():
    bomb = raw_zip([("manifest.json", "{}"), ("parameters.json", b"0" * (200 * 1024))])           # ~200KB of zeros compresses >100:1
    with pytest.raises(PackageRejected) as e:
        A.open_package(bomb)
    assert e.value.code == "COMPRESSION_RATIO"
    big = raw_zip([("manifest.json", "{}"), ("metadata.json", b"x" * (A.MAX_FILE_BYTES + 10))])
    with pytest.raises(PackageRejected) as e:
        A.open_package(big)
    assert e.value.code in ("FILE_TOO_LARGE", "COMPRESSION_RATIO")
    many = raw_zip([("manifest.json", "{}")] + [(f"tests/t{i}.json", "{}") for i in range(A.MAX_ENTRIES + 1)])
    with pytest.raises(PackageRejected) as e:
        A.open_package(many)
    assert e.value.code == "TOO_MANY_ENTRIES"
    import os
    with pytest.raises(PackageRejected) as e:
        A.open_package(os.urandom(A.MAX_ARCHIVE_BYTES + 1))
    assert e.value.code == "ARCHIVE_TOO_LARGE"
    for junk, code in ((b"", "EMPTY_ARCHIVE"), (b"not a zip at all", "NOT_A_ZIP"), (b"PK\x03\x04" + b"\x00" * 30, "NOT_A_ZIP")):
        with pytest.raises(PackageRejected) as e:
            A.open_package(junk)
        assert e.value.code == code
    # compressible enough to pass the ratio check (a repeated 4KB random block) but over the total decompressed budget
    blk = os.urandom(4096)
    rep = [(f"tests/r{i}.json", (blk * 64)[: 250 * 1024]) for i in range(5)]
    z = raw_zip([("manifest.json", "{}")] + rep)
    assert len(z) < A.MAX_ARCHIVE_BYTES
    with pytest.raises(PackageRejected) as e:
        A.open_package(z)
    assert e.value.code in ("TOTAL_TOO_LARGE", "COMPRESSION_RATIO"), e.value.code


def test_a_header_that_lies_about_the_uncompressed_size_is_rejected():
    z = bytearray(build("Gold Lie"))
    # shrink the uncompressed-size field of the central-directory record for parameters.json
    i = z.rfind(b"PK\x01\x02")
    while i != -1:
        nlen = struct.unpack_from("<H", z, i + 28)[0]
        if bytes(z[i + 46:i + 46 + nlen]) == b"parameters.json":
            struct.pack_into("<I", z, i + 24, 2)
            break
        i = z.rfind(b"PK\x01\x02", 0, i)
    assert i != -1
    with pytest.raises(PackageRejected):
        PackageManager.validate(bytes(z))


def test_executable_and_install_files_are_rejected():
    for fname in ["setup.py", "requirements.txt", "evil.py", "run.sh", "install.sh", "a.exe", "package.json", "tests/x.py", "payload.dll", "Makefile", "x.js"]:
        z = raw_zip([("manifest.json", "{}"), (fname, "print(1)")])
        with pytest.raises(PackageRejected) as e:
            A.open_package(z)
        assert e.value.code == "EXECUTABLE_OR_INSTALL_FILE", (fname, e.value.code)


def test_unlisted_entries_and_missing_required_files_are_rejected():
    with pytest.raises(PackageRejected) as e:
        A.open_package(raw_zip([("manifest.json", "{}"), ("notes.txt", "x")]))
    assert e.value.code == "UNSUPPORTED_ENTRY"
    rejected(build(drop=("parameters.json",)), "MISSING_FILE")
    rejected(build(drop=("strategy/rules.json",)), "MISSING_FILE")


# ------------------------------------------------------------------ manifest, runtime, hashes
def test_python_pine_and_unknown_runtimes_are_refused_with_the_reason():
    for rt, frag in (("python", "isolation"), ("pine", "Pine"), ("wasm", "sandbox"), ("lua", "unknown")):
        with pytest.raises(PackageRejected) as e:
            PackageManager.validate(build(manifest_over={"runtime": rt}))
        assert e.value.code == "UNSUPPORTED_RUNTIME" and frag in e.value.detail, (rt, e.value.detail)


def test_malformed_and_overreaching_manifests_are_rejected():
    for over in ({"schema_version": 2}, {"name": "x"}, {"version": "1"}, {"instruments": []}, {"instruments": ["bad symbol!"]}, {"timeframes": ["7s"]},
                 {"dependencies": ["requests"]}, {"requested_capabilities": ["network"]}, {"install_script": "rm -rf /"}, {"entry": "strategy/run.py"},
                 {"files": {"x": "nothex"}}):
        rejected(build(manifest_over=over), "MALFORMED_MANIFEST")
    rejected(build(raw="{not json"), "MALFORMED_JSON")
    rejected(build(raw='{"a":1,"a":2}'), "MALFORMED_JSON")
    rejected(build(raw='{"schema_version": NaN}'), "MALFORMED_JSON")
    with pytest.raises(PackageRejected) as e:
        A.parse_json(b"[" * 200000, "x")                                    # RecursionError inside the JSON parser is a clean rejection
    assert e.value.code == "MALFORMED_JSON"
    with pytest.raises(PackageRejected) as e:
        A.open_package(build(raw="[" * 5000))                              # the same payload in an archive trips the ratio limit first
    assert e.value.code == "COMPRESSION_RATIO"
    rejected(build(raw="[" * 30 + "]" * 30), "JSON_TOO_DEEP")


def test_hash_tampering_and_file_list_mismatch_are_rejected():
    rejected(build(tamper={"parameters.json": b'{"atr_stop": 9}'}), "HASH_MISMATCH")
    good = build(extra={"tests/v.json": "{}"})
    zf = zipfile.ZipFile(io.BytesIO(good)); man = json.loads(zf.read("manifest.json")); man["files"].pop("tests/v.json")
    z2 = raw_zip([("manifest.json", json.dumps(man))] + [(n, zf.read(n)) for n in zf.namelist() if n != "manifest.json"])
    rejected(z2, "FILE_LIST_MISMATCH")                                       # an unlisted (unhashed) file smuggled in
    man2 = json.loads(zf.read("manifest.json")); man2["files"]["tests/ghost.json"] = "0" * 64
    z3 = raw_zip([("manifest.json", json.dumps(man2))] + [(n, zf.read(n)) for n in zf.namelist() if n != "manifest.json"])
    rejected(z3, "FILE_LIST_MISMATCH")


def test_rules_parameters_metadata_and_identity_are_validated():
    rejected(build(rules={"entry_long": []}), "INVALID_RULES")
    rejected(build(rules={"entry_long": [{"type": "run_python", "code": "os.system('x')"}]}), "INVALID_RULES")
    rejected(build(rules={**RULES, "atr_stop": 99}, params={}), "INVALID_RULES")
    rejected(build(params={"leverage": 100}), "UNSUPPORTED_PARAMETER")
    rejected(build(params={"atr_stop": 99}), "INVALID_RULES")
    rejected(build(meta={"k": {"nested": 1}}), "MALFORMED_METADATA")
    rejected(build(meta={f"k{i}": "v" for i in range(21)}), "MALFORMED_METADATA")
    rejected(build(extra={"tests/bad.json": "{oops"}), "MALFORMED_JSON")
    bad = build(rules=None)
    zf = zipfile.ZipFile(io.BytesIO(bad)); name = json.loads(zf.read("manifest.json"))["name"]
    other = json.dumps({"name": "Other Name", "version": "1.0", **RULES}).encode()
    man = json.loads(zf.read("manifest.json")); man["files"]["strategy/rules.json"] = hashlib.sha256(other).hexdigest()
    rejected(raw_zip([("manifest.json", json.dumps(man)), ("strategy/rules.json", other), ("parameters.json", zf.read("parameters.json")), ("metadata.json", zf.read("metadata.json"))]), "IDENTITY_MISMATCH")


def test_hostile_text_in_rules_is_inert_data():
    # strings that look like code are only data: invalid rules are refused, free-text fields are stored, nothing is evaluated
    marker = "/tmp/dewako_pwned_marker"
    rejected(build(rules={"entry_long": [{"type": "__import__('os').system('touch %s')" % marker}]}), "INVALID_RULES")
    PackageManager.validate(build(meta={"note": "__import__('os').system('touch %s')" % marker}))
    import os
    assert not os.path.exists(marker)


def test_builtin_name_collision_and_version_conflicts_are_rejected():
    m, db = mgr()
    try:
        with pytest.raises(PackageRejected) as e:
            m.import_package(build("Trend AI", "9.0"), ADMIN)
        assert e.value.code == "NAME_COLLIDES_WITH_BUILTIN"
        data = build("Gold Conflict", "1.0")
        m.import_package(data, ADMIN)
        with pytest.raises(PackageRejected) as e:
            m.import_package(data, ADMIN)
        assert e.value.code == "DUPLICATE_PACKAGE"
        with pytest.raises(PackageRejected) as e:
            m.import_package(build("Gold Conflict", "1.0", params={"atr_stop": 3.0}), ADMIN)   # same name+version, different bytes
        assert e.value.code == "VERSION_CONFLICT"
        assert m.import_package(build("Gold Conflict", "1.1"), ADMIN)["state"] == "VALIDATED"     # a new version is fine
        with db.read() as c:
            assert c.execute("SELECT COUNT(*) FROM strategy_packages WHERE state='VALIDATION_FAILED'").fetchone()[0] == 1   # only the built-in name collision, recorded
    finally:
        _unreg(m)


def test_failed_imports_are_recorded_audited_idempotent_and_do_not_block_a_fixed_version():
    m, db = mgr()
    bad = build("Gold Fix", manifest_over={"runtime": "python"})
    with pytest.raises(PackageRejected):
        m.import_package(bad, ADMIN)
    with pytest.raises(PackageRejected) as e:
        m.import_package(bad, ADMIN)
    assert e.value.code == "DUPLICATE_PACKAGE"
    rows = m.list()
    assert [r["state"] for r in rows] == ["VALIDATION_FAILED"] and rows[0]["findings"][0]["code"] == "UNSUPPORTED_RUNTIME"
    with pytest.raises(TransitionRefused):
        m.transition(rows[0]["id"], "BACKTEST_ELIGIBLE", ADMIN)
    assert m.import_package(build("Gold Fix"), ADMIN)["state"] == "VALIDATED"
    assert AuditLog(db).verify_chain()["ok"]


# ------------------------------------------------------------------ integrity, restart, quarantine
def test_stored_spec_tampering_is_caught_by_the_fingerprint_and_quarantines():
    m, db = mgr()
    try:
        pid = m.import_package(build("Gold Tamper"), ADMIN)["id"]
        with db.tx() as c:
            spec = json.loads(c.execute("SELECT spec FROM strategy_packages WHERE id=?", (pid,)).fetchone()["spec"])
            spec["atr_stop"] = 9.0
            c.execute("UPDATE strategy_packages SET spec=? WHERE id=?", (json.dumps(spec), pid))
        with pytest.raises(TransitionRefused):
            m.transition(pid, "BACKTEST_ELIGIBLE", ADMIN)
        assert m.get(pid)["state"] == "QUARANTINED" and ("Gold Tamper", "1.0") not in sbase._REGISTRY
        with db.read() as c:
            assert "STRATEGY_PACKAGE_QUARANTINE" in [x["action"] for x in c.execute("SELECT action FROM audit_log").fetchall()]
    finally:
        _unreg(m)


def test_restart_re_registers_verified_packages_and_quarantines_tampered_ones():
    m, db = mgr()
    try:
        ok = m.import_package(build("Gold Restart OK"), ADMIN)["id"]
        bad = m.import_package(build("Gold Restart Bad"), ADMIN)["id"]
        for p in (ok, bad):
            m.transition(p, "BACKTEST_ELIGIBLE", ADMIN)
        _unreg(m)                                                              # simulate a process restart: in-memory registry is gone
        assert ("Gold Restart OK", "1.0") not in sbase._REGISTRY
        with db.tx() as c:
            c.execute("UPDATE strategy_packages SET fingerprint=? WHERE id=?", ("0" * 64, bad))
        m2 = PackageManager(db, AuditLog(db))
        problems = m2.sync_registry()
        assert ("Gold Restart OK", "1.0") in sbase._REGISTRY and ("Gold Restart Bad", "1.0") not in sbase._REGISTRY
        assert len(problems) == 1 and "fingerprint" in problems[0]
        assert m2.get(bad)["state"] == "QUARANTINED" and m2.get(ok)["state"] == "BACKTEST_ELIGIBLE"
        _unreg(m2)
    finally:
        _unreg(m)


def test_registered_package_emits_only_a_normal_signal_through_the_standard_signal_engine():
    import scenarios as S
    from dewako.core.types import Signal
    from dewako.strategies.signal_engine import SignalEngine
    m, _ = mgr()
    try:
        pid = m.import_package(build("Gold Sig", rules={"entry_long": [{"type": "price_above_ema", "length": 5}]}, params={}), ADMIN)["id"]
        m.transition(pid, "BACKTEST_ELIGIBLE", ADMIN)
        cls = sbase.get("Gold Sig", "1.0")
        st = S.state(S.steady_uptrend())
        res = SignalEngine(cls()).evaluate(st)           # the same validation path every strategy uses
        assert res.signal is not None and isinstance(res.signal, Signal)
        assert res.signal.strategy == "Gold Sig" and res.signal.stop_price is not None
        assert set(Signal.model_fields) >= {"symbol", "side", "entry_price", "stop_price"}     # a proposal: no order, size or account fields
        assert not any(k in Signal.model_fields for k in ("api_key", "exchange", "adapter", "order_id"))
    finally:
        _unreg(m)


def test_startup_registry_step_marks_package_strategies_draft_never_released():
    from dewako.runtime.startup import StrategyRegistryStore
    m, db = mgr()
    try:
        name = _name()
        pid = m.import_package(build(name), ADMIN)["id"]
        m.transition(pid, "BACKTEST_ELIGIBLE", ADMIN)
        StrategyRegistryStore(db).verify(sbase.all_strategies())               # the real startup step over the whole registry
        with db.read() as c:
            row = c.execute("SELECT release_status FROM strategy_versions WHERE strategy_id=?", (name,)).fetchone()
        assert row["release_status"] == "DRAFT"                                 # experimental: never live-eligible
        safety = SafetyController(db)
        eid = make_live_ready(db, safety)
        assert not safety.live_gate(exchange_id=eid, strategy_id=name, strategy_version="1.0", symbol="XAUUSD").allowed
    finally:
        _unreg(m)


def test_routes_are_registered_with_the_expected_permissions():
    import ast, pathlib
    src = pathlib.Path(__file__).resolve().parents[1].joinpath("dewako/api/main.py").read_text()
    tree = ast.parse(src)
    found = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef):
            for d in n.decorator_list:
                if isinstance(d, ast.Call) and getattr(d.func, "attr", "") in ("get", "post") and d.args and "strategy-packages" in ast.unparse(d.args[0]):
                    found[n.name] = ast.unparse(n)
    assert set(found) == {"import_strategy_package", "list_strategy_packages", "get_strategy_package", "transition_strategy_package",
                          "strategy_package_evidence", "review_existing_packages"}
    assert all("permission('strategies')" in v for v in found.values())          # none is anonymous
