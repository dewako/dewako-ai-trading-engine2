"""Bounded execution for the crash-recovery suites: cross-platform (POSIX and Windows).

What this module guarantees, precisely
--------------------------------------
run_process()  HARD bound for a child process *tree*.
    POSIX   : the child is its own session/process group; on timeout or in `finally` the whole group gets SIGKILL (os.killpg).
    Windows : the child is assigned to a Job Object created with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; the job is terminated
              (TerminateJobObject) and closed, which kills the child and every descendant that was started after the assignment.
              If the job cannot be created/assigned (e.g. an old nested-job configuration) `taskkill /T /F /PID <pid>` is used for
              exactly that pid's tree. Nothing is ever killed by image name or by pattern, so unrelated processes are untouched.
    Windows limitations: pid reuse between the snapshot and the verification is theoretically possible (seconds apart; ignored);
              NtResumeProcess is an ntdll export (stable in practice, not a documented Win32 API).
    Identity: WorkerHung.pid is the process this module LAUNCHED (argv[0]). It is not necessarily the interpreter that runs the
              code: a Windows virtual-environment python.exe is a launcher stub that starts the real interpreter as a CHILD, so the
              pid the child prints for itself differs from WorkerHung.pid. WorkerHung.tree lists every descendant that existed at the
              moment of the kill (real interpreter, grandchildren, ...). Cleanup is VERIFIED, not assumed: after the kill every pid in
              {pid} + tree must be gone within 10 s (retry once per survivor) or run_process raises CleanupError.
    Windows start-up: the child is created SUSPENDED, put in the job, then resumed (NtResumeProcess), so even the launcher's own
              child cannot escape the job; no window exists in which a descendant is outside it.
    Output is collected with communicate(timeout=) (reader threads on Windows), so a chatty child cannot deadlock on a full pipe.
    Not killed on success: a child that exited normally is simply reaped (descendants that outlived it are still killed).

watchdog()     SOFT bound for the in-process part of a test. It does NOT claim to forcibly stop arbitrary Python code.
    mode "interrupting" (POSIX, main thread): SIGALRM raises TestTimeout inside the blocked call (sleep, socket read, lock wait).
    mode "diagnostic"   (Windows, or not the main thread): nothing can interrupt the main thread safely, so a per-call timer thread
              captures every Python thread's stack at the deadline (sys._current_frames + threading.enumerate); when the body finally
              returns, TestTimeout is raised with that capture. No shared global state: watchdogs nest freely. Limitation: if the
              interpreter is blocked inside a C call that never releases the GIL, the timer thread cannot run and no stacks exist.
              A test that never returns is NOT stopped by the watchdog; hard bounds come from run_process()/run_isolated() and the
              database-side timeouts (PostgreSQL lock/statement timeouts, SQLite busy timeout).
    Both modes: a body that finished after the deadline is reported as a failure, never as a pass.

run_isolated() HARD bound for in-process code: runs "module:function" in a child process under run_process().

hard_kill_self() / KILLED_RC   the abrupt, uncatchable self-termination used by the crash workers and its expected exit status.
"""
import ctypes
import functools
import os
import signal
import subprocess
import sys
import threading
import time

WINDOWS = os.name == "nt"
TEST_TIMEOUT = float(os.environ.get("DEWAKO_TEST_TIMEOUT", "240"))

# SIGKILL does not exist on Windows. os.kill(pid, 9) there calls TerminateProcess(handle, 9): immediate, no finally blocks, no atexit.
KILLED_RC = 9 if WINDOWS else -signal.SIGKILL


def hard_kill_self():
    """Die the way `kill -9` does: no finally, no atexit, no flush, no rollback hook."""
    os.kill(os.getpid(), 9 if WINDOWS else signal.SIGKILL)


def _tail(s, n=1500):
    return (s or "")[-n:]


# ------------------------------------------------------------------------------------------------ Windows job objects
if WINDOWS:
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _PROCESS_TERMINATE, _PROCESS_SET_QUOTA, _PROCESS_QUERY_LIMITED_INFORMATION = 0x0001, 0x0100, 0x1000
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, _JobObjectExtendedLimitInformation, _STILL_ACTIVE = 0x2000, 9, 259

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                      "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class _EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BASIC), ("IoInfo", _IO_COUNTERS), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    _k32.CreateJobObjectW.restype, _k32.CreateJobObjectW.argtypes = wintypes.HANDLE, [wintypes.LPVOID, wintypes.LPCWSTR]
    _k32.SetInformationJobObject.restype = wintypes.BOOL
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    _k32.OpenProcess.restype, _k32.OpenProcess.argtypes = wintypes.HANDLE, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.AssignProcessToJobObject.restype = wintypes.BOOL
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.TerminateJobObject.restype, _k32.TerminateJobObject.argtypes = wintypes.BOOL, [wintypes.HANDLE, wintypes.UINT]
    _k32.GetExitCodeProcess.restype = wintypes.BOOL
    _k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _k32.CloseHandle.restype, _k32.CloseHandle.argtypes = wintypes.BOOL, [wintypes.HANDLE]
    _ntdll = ctypes.WinDLL("ntdll")
    _ntdll.NtResumeProcess.restype, _ntdll.NtResumeProcess.argtypes = ctypes.c_long, [wintypes.HANDLE]
    _PROCESS_SUSPEND_RESUME = 0x0800
    _CREATE_SUSPENDED = 0x00000004

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    _k32.CreateToolhelp32Snapshot.restype, _k32.CreateToolhelp32Snapshot.argtypes = wintypes.HANDLE, [wintypes.DWORD, wintypes.DWORD]
    _k32.Process32FirstW.restype = _k32.Process32NextW.restype = wintypes.BOOL
    _k32.Process32FirstW.argtypes = _k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]

    def _process_parents():
        """{pid: parent_pid} for every process on the machine (ToolHelp snapshot)."""
        snap = _k32.CreateToolhelp32Snapshot(0x2, 0)                       # TH32CS_SNAPPROCESS
        if snap in (None, 0, ctypes.c_void_p(-1).value):
            raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot")
        out = {}
        try:
            e = _PROCESSENTRY32W()
            e.dwSize = ctypes.sizeof(e)
            ok = _k32.Process32FirstW(snap, ctypes.byref(e))
            while ok:
                out[int(e.th32ProcessID)] = int(e.th32ParentProcessID)
                ok = _k32.Process32NextW(snap, ctypes.byref(e))
        finally:
            _k32.CloseHandle(snap)
        return out

    def _resume_process(pid):
        h = _k32.OpenProcess(_PROCESS_SUSPEND_RESUME, False, pid)
        if not h:
            raise OSError(ctypes.get_last_error(), "OpenProcess(PROCESS_SUSPEND_RESUME)")
        try:
            status = _ntdll.NtResumeProcess(h)
            if status != 0:
                raise OSError(status, "NtResumeProcess failed")
        finally:
            _k32.CloseHandle(h)

    def _make_job_for(pid):
        """A kill-on-close Job Object containing `pid`, or None when that is not possible (caller falls back to taskkill)."""
        job = _k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _EXTENDED()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        proc = None
        try:
            if not _k32.SetInformationJobObject(job, _JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)):
                raise OSError(ctypes.get_last_error(), "SetInformationJobObject")
            proc = _k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
            if not proc:
                raise OSError(ctypes.get_last_error(), "OpenProcess")
            if not _k32.AssignProcessToJobObject(job, proc):
                raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject")
            return job
        except OSError:
            _k32.CloseHandle(job)
            return None
        finally:
            if proc:
                _k32.CloseHandle(proc)

    def _kill_job(job):
        _k32.TerminateJobObject(job, 1)
        _k32.CloseHandle(job)

    def _taskkill_tree(pid):
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(int(pid))], capture_output=True, timeout=20)
        except Exception:
            pass


def pid_alive(pid):
    """True while a process with this pid is running. Zombies (already dead, not yet reaped) count as dead."""
    if WINDOWS:
        h = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        try:
            code = wintypes.DWORD()
            return bool(_k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == _STILL_ACTIVE
        finally:
            _k32.CloseHandle(h)
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:                                                                    # Linux: a zombie is not a running process
        with open(f"/proc/{int(pid)}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


def wait_until_dead(pids, timeout=10.0):
    """Poll until none of `pids` is alive; returns the list still alive after `timeout`."""
    end = time.monotonic() + timeout
    alive = [p for p in pids if pid_alive(p)]
    while alive and time.monotonic() < end:
        time.sleep(0.1)
        alive = [p for p in alive if pid_alive(p)]
    return alive


def _parent_map():
    if WINDOWS:
        return _process_parents()
    out = {}
    if os.path.isdir("/proc"):
        for name in os.listdir("/proc"):
            if name.isdigit():
                try:
                    with open(f"/proc/{name}/stat") as f:
                        out[int(name)] = int(f.read().rsplit(")", 1)[1].split()[1])      # field 4: ppid
                except (OSError, IndexError, ValueError):
                    pass
        return out
    ps = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True, timeout=20)
    for line in ps.stdout.splitlines():
        a = line.split()
        if len(a) == 2 and all(x.isdigit() for x in a):
            out[int(a[0])] = int(a[1])
    return out


def descendants(pid):
    """Every process below `pid` right now (children, grandchildren, ...), excluding `pid` itself. [] if it cannot be determined."""
    try:
        parents = _parent_map()
    except Exception:
        return []
    found, frontier = [], [pid]
    while frontier:
        nxt = [c for c, par in parents.items() if par in frontier and c not in found and c != pid]
        found += nxt
        frontier = nxt
    return found


# ------------------------------------------------------------------------------------------------ process tree runner
class WorkerHung(AssertionError):
    """A child exceeded its time limit and its whole process tree was killed. Carries diagnostics."""

    def __init__(self, msg, pid=None, out="", err="", elapsed=0.0, tree=()):
        super().__init__(msg)
        # pid  = the process run_process LAUNCHED (argv[0]); on a Windows venv this is the launcher stub, not the interpreter.
        # tree = every descendant alive at the moment of the kill (real interpreter, grandchildren, ...).
        self.pid, self.out, self.err, self.elapsed, self.tree = pid, out, err, elapsed, list(tree)


class CleanupError(AssertionError):
    """The kill was issued but a process of the tree was still alive afterwards. Never swallowed."""


def _kill_tree(p, job):
    if WINDOWS:
        if job is not None:
            _kill_job(job)
        if p.poll() is None:
            _taskkill_tree(p.pid)
    else:
        try:
            os.killpg(p.pid, signal.SIGKILL)               # p.pid is the group id: the child was started with start_new_session
        except (ProcessLookupError, PermissionError):
            pass


def _kill_pid(pid):
    """Last resort for one pid that survived the tree kill (idempotent: an already-dead pid is fine)."""
    if WINDOWS:
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(int(pid))], capture_output=True, timeout=20)
        except Exception:
            pass
    else:
        try:
            os.kill(int(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _verified_cleanup(p, job, label):
    """Snapshot the tree, kill it, then PROVE every pid is gone. Returns the snapshot. Raises CleanupError otherwise."""
    tree = descendants(p.pid) if p.poll() is None else []
    _kill_tree(p, job)
    try:
        p.wait(timeout=10)                                  # reap the launched process (a zombie would look alive on some systems)
    except subprocess.TimeoutExpired:
        pass
    pids = tree + ([p.pid] if p.poll() is None else [])
    left = wait_until_dead(pids, 10.0)
    if left:
        for pid in left:
            _kill_pid(pid)
        left = wait_until_dead(left, 10.0)
    if left:
        raise CleanupError(f"[{label}] processes still alive after the kill: {left} (launched pid {p.pid}, tree {tree})")
    return tree


def run_process(argv, env, timeout, label=""):
    """Run argv; returns (returncode, stdout, stderr). On timeout the whole tree is killed (and verified dead) and WorkerHung is raised."""
    env = dict(env)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    kw = dict(env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
    if WINDOWS:
        # suspended: nothing runs (and so nothing can be spawned) until the process is inside the job
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | _CREATE_SUSPENDED
    else:
        kw["start_new_session"] = True
    started = time.monotonic()
    p = subprocess.Popen(argv, **kw)
    job = None
    if WINDOWS:
        try:
            job = _make_job_for(p.pid)
            _resume_process(p.pid)
        except BaseException:
            _kill_tree(p, job)
            p.wait(timeout=10)
            raise
    out = err = ""
    hung = False
    tree = []
    try:
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            hung = True
    finally:
        try:
            tree = _verified_cleanup(p, job, label)             # nothing may outlive the test, not even a grandchild
        finally:
            if hung:
                try:
                    out, err = p.communicate(timeout=10)        # the tree is dead, the pipes are closed: collect what was written
                except Exception:
                    for s_ in (p.stdout, p.stderr):
                        try:
                            s_.close()
                        except Exception:
                            pass
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
    elapsed = time.monotonic() - started
    if hung:
        raise WorkerHung(f"worker hung for {timeout}s and was killed (elapsed {elapsed:.1f}s) [{label}] launched pid {p.pid}, "
                         f"descendants at kill {tree} argv={argv[1:]}\n"
                         f"--- stdout tail ---\n{_tail(out)}\n--- stderr tail ---\n{_tail(err)}", p.pid, out, err, elapsed, tree)
    return p.returncode, out, err


def run_isolated(target, timeout, *args, label=""):
    """Run 'module:function' (importable from this directory or the backend) in a child process with a hard timeout."""
    here = os.path.dirname(os.path.abspath(__file__))
    code = ("import importlib,sys; m,f=sys.argv[1].split(':'); r=getattr(importlib.import_module(m),f)(*sys.argv[2:]); "
            "sys.exit(0 if r in (None,0) else 1)")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([os.path.dirname(here), here, os.environ.get("PYTHONPATH", "")])}
    return run_process([sys.executable, "-c", code, target, *map(str, args)], env, timeout, label=label or target)


def selftest_tree(seconds="3600"):
    """Child-side helper for the cleanup tests: spawns a grandchild sleeper, reports both pids, then sleeps (an unexpected hang)."""
    gc = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({float(seconds)})"])
    print(f"PIDS {os.getpid()} {gc.pid}", flush=True)
    sys.stderr.write("about to hang\n")
    sys.stderr.flush()
    time.sleep(float(seconds))


def selftest_sleep(seconds="3600"):
    print(f"PIDS {os.getpid()}", flush=True)
    time.sleep(float(seconds))


# ------------------------------------------------------------------------------------------------ in-process watchdog
class TestTimeout(AssertionError):
    __test__ = False


def watchdog_mode():
    return "interrupting" if (hasattr(signal, "SIGALRM") and threading.current_thread() is threading.main_thread()) else "diagnostic"


def _stacks(exclude=()):
    """Formatted stacks of every Python thread: 'Thread <name> (ident=N)' headers, newest frame last."""
    import traceback
    names = {t.ident: t.name for t in threading.enumerate()}
    parts = []
    for tid, fr in sys._current_frames().items():
        if tid in exclude:
            continue
        parts.append(f"Thread {names.get(tid, '?')} (ident={tid}):\n" + "".join(traceback.format_stack(fr, limit=25)))
    return "\n".join(parts)


def watchdog(seconds=TEST_TIMEOUT):
    """Decorator. See the module docstring for the exact guarantee of each mode."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            mode = watchdog_mode()
            started = time.monotonic()
            if mode == "interrupting":
                def fire(signum, frame):
                    raise TestTimeout(f"{fn.__name__} exceeded {seconds}s (watchdog, interrupting). Where it was waiting:\n{_stacks()}")
                old_handler = signal.signal(signal.SIGALRM, fire)
                outer_remaining, _ = signal.setitimer(signal.ITIMER_REAL, seconds)      # nested watchdogs: remember the outer deadline
                try:
                    return fn(*a, **kw)
                finally:
                    signal.setitimer(signal.ITIMER_REAL, 0)
                    signal.signal(signal.SIGALRM, old_handler)
                    if outer_remaining:
                        signal.setitimer(signal.ITIMER_REAL, max(0.001, outer_remaining - (time.monotonic() - started)))
            # diagnostic mode: a private timer thread per call (no shared state, so nesting is safe) snapshots all stacks at the deadline
            box = {}

            def capture():
                box["stacks"] = _stacks(exclude=(threading.get_ident(),))
                box["at"] = time.monotonic() - started
            timer = threading.Timer(seconds, capture)
            timer.daemon = True
            timer.start()
            try:
                result = fn(*a, **kw)
            finally:
                timer.cancel()
                timer.join(5)
                elapsed = time.monotonic() - started
            if elapsed > seconds:
                trace = box.get("stacks") or "(the timer thread did not run: the interpreter was blocked in a call that holds the GIL)"
                raise TestTimeout(f"{fn.__name__} took {elapsed:.1f}s, over the {seconds}s limit (watchdog, diagnostic mode: the body "
                                  f"was NOT interrupted, it returned late).\nStacks of all threads captured at the {seconds}s deadline:\n{trace}")
            return result
        return wrapper
    return deco


def bound_all(namespace, seconds=TEST_TIMEOUT):
    """Apply watchdog() to every test_* function of a module: bound_all(globals())."""
    for name, obj in list(namespace.items()):
        if name.startswith("test_") and callable(obj) and not getattr(obj, "__wrapped__", None):
            namespace[name] = watchdog(seconds)(obj)
