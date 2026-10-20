#!/usr/bin/env python3
"""Fail a CI job unless a JUnit XML report proves the required tests really executed.

  assert_no_skips.py REPORT.xml [--min-tests N] [--allow-skip "substring"]... [--require test_name]...

* any failure/error                      -> fail
* a skipped test whose message does not contain an --allow-skip substring -> fail (a skipped critical test is not a pass)
* fewer than --min-tests tests           -> fail
* a --require'd test that is absent, skipped, failed or errored -> fail (proves e.g. the PostgreSQL identity test and the two HTTP
  regressions were executed, not silently skipped or deselected)
"""
import argparse
import sys
import xml.etree.ElementTree as ET

ap = argparse.ArgumentParser()
ap.add_argument("report")
ap.add_argument("--min-tests", type=int, default=1)
ap.add_argument("--allow-skip", action="append", default=[])
ap.add_argument("--require", action="append", default=[])
a = ap.parse_args()

root = ET.parse(a.report).getroot()
cases = list(root.iter("testcase"))
status = {}
problems = []
for c in cases:
    name = c.get("name")
    kind = "pass"
    msg = ""
    for child in c:
        if child.tag in ("failure", "error"):
            kind, msg = "fail", child.get("message", "")
        elif child.tag == "skipped":
            kind, msg = "skip", (child.get("message") or "") + (child.text or "")
    status.setdefault(name, []).append(kind)
    if kind == "fail":
        problems.append(f"FAILED  {c.get('classname')}::{name}")
    if kind == "skip" and not any(s in msg for s in a.allow_skip):
        problems.append(f"SKIPPED {c.get('classname')}::{name}: {msg[:120]}")
for req in a.require:
    got = status.get(req)
    if not got:
        problems.append(f"REQUIRED test did not run at all: {req}")
    elif "pass" not in got:
        problems.append(f"REQUIRED test did not pass ({got}): {req}")
n = len(cases)
skipped = sum(k.count("skip") for k in status.values())
print(f"tests={n} skipped={skipped} failed={sum(k.count('fail') for k in status.values())} required_ok={len(a.require) - sum(1 for r in a.require if 'pass' not in status.get(r, []))}/{len(a.require)}")
if n < a.min_tests:
    problems.append(f"only {n} tests ran, expected at least {a.min_tests}")
for p in problems:
    print(p)
sys.exit(1 if problems else 0)
