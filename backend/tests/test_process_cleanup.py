"""Tests of the bounded-execution helper itself (tests/bounded.py). Cross-platform: pid liveness is checked through the operating
system, never through pgrep/ps pattern matching, so unrelated processes cannot influence the result."""
import re
import sys
import time

import pytest

from bounded import (KILLED_RC, WINDOWS, TestTimeout, WorkerHung, bound_all, hard_kill_self, pid_alive, run_isolated, run_process,
                     wait_until_dead, watchdog, watchdog_mode)
import os


def _pids(text):
    m = re.search(r"PIDS (\d+)(?: (\d+))?", text)
    assert m, f"child did not report its pids: {text!r}"
    return [int(x) for x in m.groups() if x]


def test_unexpected_hang_fails_with_diagnostics_and_the_whole_process_tree_dies():
    # PID semantics: WorkerHung.pid is the process run_process LAUNCHED. The interpreter that prints PIDS is either that very
    # process (POSIX, plain Windows python) or its CHILD (Windows venv launcher stub). e.tree lists every descendant alive at the kill.
    with pytest.raises(WorkerHung) as ei:
        run_isolated("bounded:selftest_tree", 4, label="tree-test")
    e = ei.value
    msg = str(e)
    assert "hung for 4s" in msg and "tree-test" in msg and "about to hang" in msg and "PIDS" in msg, msg
    child, grandchild = _pids(e.out)
    assert child == e.pid or child in e.tree, f"printing interpreter {child} is neither the launched pid {e.pid} nor one of its descendants {e.tree}"
    assert grandchild in e.tree, f"grandchild {grandchild} was not found in the process tree snapshot {e.tree}"
    assert grandchild != child
    everything = {e.pid, child, grandchild, *e.tree}
    assert wait_until_dead(sorted(everything), 10, e.stamps) == [], "a process of the hung tree survived the timeout cleanup"


def test_cleanup_is_verified_and_idempotent_and_an_unkillable_survivor_is_reported():
    from bounded import CleanupError, _verified_cleanup, descendants
    import subprocess
    # idempotent: cleaning up an already finished process is a no-op, twice
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    assert _verified_cleanup(p, None, "done")[0] == [] and _verified_cleanup(p, None, "again")[0] == []
    # descendants() really finds a grandchild
    parent = subprocess.Popen([sys.executable, "-c", "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); time.sleep(60)"])
    try:
        deadline = time.monotonic() + 15
        while not descendants(parent.pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        kids = descendants(parent.pid)
        assert kids, "descendants() did not find the child of a live process"
    finally:
        parent.kill()
        parent.wait()
        for k in kids:
            from bounded import _kill_pid
            _kill_pid(k)
        assert wait_until_dead(kids, 10) == []


def test_a_reused_pid_is_a_different_process_it_is_never_counted_alive_or_killed(monkeypatch):
    # Windows reuses a pid within seconds of its process exiting. Identity is (pid, creation stamp): a pid now held by a NEWER process
    # means ours is gone, and the last-resort kill must never hit that stranger. Simulated by changing the stamp of a live process.
    import bounded
    me = os.getpid()
    real = bounded.start_stamp(me)
    assert real is not None, "no creation stamp for a live process on this platform"
    import subprocess
    time.sleep(0.05)
    newer = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert bounded.start_stamp(newer.pid) > real, "a process started later must carry a later creation stamp"
    finally:
        newer.kill()
        newer.wait()
    assert pid_alive(me, real) and pid_alive(me)                      # same process: alive; no stamp: plain pid check
    assert not pid_alive(me, real - 1)                                # the pid is held by a different (newer) process
    assert wait_until_dead([me], 1, {me: real - 1}) == []
    killed = []
    monkeypatch.setattr(bounded.os, "kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(bounded.subprocess, "run", lambda *a, **k: killed.append(a))
    bounded._kill_pid(me, real - 1)
    assert killed == [], "the last-resort kill hit a process that only shares the pid"
    bounded._kill_pid(me, real)
    assert killed, "the last-resort kill skipped the process it was meant for"


def test_an_older_process_is_never_adopted_as_a_child_through_a_stale_parent_pid(monkeypatch):
    # Windows keeps a dead parent's pid as ppid; once that pid is reused, an old unrelated process would look like our child.
    import bounded
    table = {10: 1, 11: 10, 12: 11, 5: 10}                            # 5 claims parent 10 but is older than 10
    stamps = {1: 100, 10: 200, 11: 300, 12: 400, 5: 50}
    monkeypatch.setattr(bounded, "_parent_map", lambda: table)
    monkeypatch.setattr(bounded, "start_stamp", lambda pid: stamps.get(pid))
    assert sorted(bounded.descendants(10)) == [11, 12]
    stamps[5] = 250                                                   # a genuinely newer child is found
    assert sorted(bounded.descendants(10)) == [5, 11, 12]


def test_a_survivor_report_names_the_process(monkeypatch):
    import bounded
    me = os.getpid()
    line = bounded.describe([me], {me: bounded.start_stamp(me)})
    assert f"{me} exe=" in line and "same process" in line and "ppid=" in line
    assert "pid REUSED" in bounded.describe([me], {me: bounded.start_stamp(me) - 1})


def test_a_normal_exit_returns_output_and_leaves_nothing_running():
    rc, out, err = run_process([sys.executable, "-c", "import os,sys; print('hello', os.getpid()); sys.stderr.write('warn')"], dict(os.environ), 30)
    assert rc == 0 and out.startswith("hello") and err == "warn"
    assert not pid_alive(int(out.split()[1]))


def test_non_zero_and_abrupt_exit_statuses_are_reported_not_swallowed():
    rc, _, _ = run_process([sys.executable, "-c", "raise SystemExit(7)"], dict(os.environ), 30)
    assert rc == 7
    rc, _, _ = run_process([sys.executable, "-c", "import bounded; bounded.hard_kill_self()"],
                           {**os.environ, "PYTHONPATH": os.path.dirname(os.path.abspath(__file__))}, 30)
    assert rc == KILLED_RC                       # -9 on POSIX, 9 on Windows (TerminateProcess)


def test_a_chatty_child_cannot_deadlock_on_a_full_pipe():
    code = "import sys; sys.stdout.write('x' * 5_000_000); sys.stderr.write('y' * 5_000_000)"
    rc, out, err = run_process([sys.executable, "-c", code], dict(os.environ), 60)
    assert rc == 0 and len(out) == 5_000_000 and len(err) == 5_000_000


def test_only_the_tree_of_the_timed_out_child_is_killed():
    bystander = __import__("subprocess").Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        with pytest.raises(WorkerHung):
            run_isolated("bounded:selftest_sleep", 2)
        assert pid_alive(bystander.pid), "cleanup killed an unrelated process"
    finally:
        bystander.kill()
        bystander.wait()


def test_hard_timeout_of_in_process_code_through_isolation():
    t0 = time.monotonic()
    with pytest.raises(WorkerHung):
        run_isolated("bounded:selftest_sleep", 3, 30)                  # would sleep 30 s; the isolation bound ends it at 3
    assert time.monotonic() - t0 < 20


def test_watchdog_guarantee_matches_its_mode_and_never_passes_silently_after_the_deadline():
    mode = watchdog_mode()
    t0 = time.monotonic()

    @watchdog(1)
    def stuck():
        time.sleep(30 if mode == "interrupting" else 2)

    with pytest.raises(TestTimeout) as ei:
        stuck()
    elapsed = time.monotonic() - t0
    if mode == "interrupting":                                         # POSIX main thread: the blocked call is interrupted
        assert elapsed < 10 and "interrupting" in str(ei.value) and "sleep" in str(ei.value)
    else:                                                              # Windows: cannot interrupt; must say so and still fail
        assert 2 <= elapsed < 10 and "diagnostic mode" in str(ei.value) and "NOT interrupted" in str(ei.value)
        assert "Thread" in str(ei.value) and "in stuck" in str(ei.value)   # real stacks captured at the deadline, incl. the decorated frame


def test_watchdog_lets_a_fast_body_through_and_restores_an_outer_deadline():
    @watchdog(5)
    def fast():
        return 42
    assert fast() == 42
    if watchdog_mode() == "interrupting":
        import signal
        @watchdog(30)
        def outer():
            fast()
            return signal.getitimer(signal.ITIMER_REAL)[0]
        assert 0 < outer() <= 30                                       # the inner watchdog did not cancel the outer one


def test_diagnostic_mode_is_exercised_on_every_platform_by_running_off_the_main_thread():
    # The Windows code path (no SIGALRM) is also what a non-main thread gets on POSIX, so it is testable here.
    import threading
    box = {}

    def body():
        assert watchdog_mode() == "diagnostic"

        @watchdog(1)
        def late():
            time.sleep(2)
        try:
            late()
            box["result"] = "passed silently"
        except TestTimeout as e:
            box["result"] = str(e)
    t = threading.Thread(target=body)
    t.start()
    t.join(30)
    assert not t.is_alive()
    r = box["result"]
    assert "diagnostic mode" in r and "NOT interrupted" in r, r
    assert "Thread" in r and "in late" in r, r                              # real stacks: the thread running the body, at the frame `late`
    assert "none: nested" not in r and "(ident=" in r, r


def test_a_launcher_stub_architecture_is_handled_the_launched_pid_is_not_the_interpreter():
    # Reproduces the Windows venv layout on any platform: the launched process (stub) starts the real interpreter as its child.
    stub = ("import subprocess,sys; "
            "subprocess.run([sys.executable, '-c', 'import sys; sys.path.insert(0, sys.argv[1]); import bounded; bounded.selftest_tree()', sys.argv[1]])")
    here = os.path.dirname(os.path.abspath(__file__))
    with pytest.raises(WorkerHung) as ei:
        run_process([sys.executable, "-c", stub, here], dict(os.environ), 5, label="stub-test")
    e = ei.value
    child, grandchild = _pids(e.out)
    assert child != e.pid and child in e.tree and grandchild in e.tree, (e.pid, child, grandchild, e.tree)
    assert wait_until_dead(sorted({e.pid, child, grandchild, *e.tree}), 10, e.stamps) == []


def test_nested_watchdogs_in_diagnostic_mode_each_keep_their_own_diagnostics():
    import bounded
    real = bounded.watchdog_mode
    bounded.watchdog_mode = lambda: "diagnostic"
    try:
        @watchdog(1)
        def late():
            time.sleep(2)

        @watchdog(30)
        def outer():
            try:
                late()
            except TestTimeout as e:
                return str(e)
            return "no failure"
        r = outer()
    finally:
        bounded.watchdog_mode = real
    assert "diagnostic mode" in r and "NOT interrupted" in r and "in late" in r and "Thread" in r and "none: nested" not in r, r


bound_all(globals())
