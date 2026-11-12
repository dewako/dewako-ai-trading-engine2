"""Self-test of the CI skip policy: each rule must turn a report red on its own. Run: python scripts/test_assert_no_skips.py"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))


def report(cases):
    x = ['<testsuites><testsuite name="t">']
    for name, kind, msg in cases:
        body = {"pass": "", "fail": f'<failure message="{msg}"/>', "skip": f'<skipped message="{msg}"/>'}[kind]
        x.append(f'<testcase classname="c" name="{name}">{body}</testcase>')
    x.append("</testsuite></testsuites>")
    fd, p = tempfile.mkstemp(suffix=".xml")
    os.write(fd, "".join(x).encode()); os.close(fd)
    return p


def rc(cases, *args):
    return subprocess.run([sys.executable, os.path.join(HERE, "assert_no_skips.py"), report(cases), *args], capture_output=True).returncode


def listfile(*names):
    fd, p = tempfile.mkstemp(suffix=".txt")
    os.write(fd, ("# comment\n\n" + "\n".join(names) + "\n").encode()); os.close(fd)
    return p


ok = [("a", "pass", ""), ("b", "pass", ""), ("pg", "skip", "PostgreSQL-only test")]
checks = {
    "clean report passes": rc(ok, "--allow-skip", "PostgreSQL-only", "--min-tests", "3") == 0,
    "a failure fails": rc(ok + [("c", "fail", "boom")], "--allow-skip", "PostgreSQL-only") == 1,
    "an unlisted skip fails": rc(ok, "--min-tests", "1") == 1,
    "too few tests fails": rc(ok, "--allow-skip", "PostgreSQL-only", "--min-tests", "4") == 1,
    "a required-file test that is missing fails": rc(ok, "--allow-skip", "PostgreSQL-only", "--require-file", listfile("a", "zz")) == 1,
    "a required-file test that was skipped fails": rc(ok, "--allow-skip", "PostgreSQL-only", "--require-file", listfile("pg")) == 1,
    "a required-file test that ran passes": rc(ok, "--allow-skip", "PostgreSQL-only", "--require-file", listfile("a", "b")) == 0,
    "an empty required file fails": rc(ok, "--allow-skip", "PostgreSQL-only", "--require-file", listfile()) != 0,
}
for k, v in checks.items():
    print(("ok   " if v else "FAIL ") + k)
sys.exit(0 if all(checks.values()) else 1)
