#!/usr/bin/env python3
"""Fail if the repository contains something that looks like a real secret. Heuristic, not a substitute for a proper scanner (gitleaks etc).
Usage: python scripts/scan_secrets.py [root]. Exit 1 on findings. Deliberately fake test values are allow-listed by marker."""
import os
import re
import sys

ROOT = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", ".pytest_cache"}
SKIP_EXT = {".png", ".jpg", ".zip", ".pyc", ".db", ".woff", ".woff2", ".ico"}
ALLOW = ("LEAKCHECK", "LEAKVALUE", "FAKE", "EXAMPLE", "changeme", "boot-secret-123", "dewako_test_pw", "topsecretvalue", "SECRETSECRET",
         "AKIA12345678", "HTTPSECRET", "AKIAHTTPLEAK")
PATTERNS = {
    "private key block": re.compile(r"-----BEGIN (RSA |EC |OPENSSH |)PRIVATE KEY-----"),
    "AWS access key id": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "Fernet/base64 32-byte key literal": re.compile(r"""["'][A-Za-z0-9_-]{43}=["']"""),
    "assigned secret": re.compile(r"""(?i)\b(api[_-]?secret|secret[_-]?key|encryption[_-]?key|password)\s*=\s*["'][^"'\s]{16,}["']"""),
    "github token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
}
bad = []
for d, dirs, files in os.walk(ROOT):
    dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
    for f in files:
        if os.path.splitext(f)[1] in SKIP_EXT:
            continue
        if f == ".env":
            bad.append((os.path.join(d, f), 0, ".env file must not be committed"))
        p = os.path.join(d, f)
        try:
            lines = open(p, errors="ignore").read().splitlines()
        except OSError:
            continue
        for n, line in enumerate(lines, 1):
            if any(a in line for a in ALLOW):
                continue
            for name, rx in PATTERNS.items():
                if rx.search(line):
                    bad.append((p, n, name))
for p, n, name in bad:
    print(f"{os.path.relpath(p, ROOT)}:{n}: {name}")
print(f"secret scan: {len(bad)} finding(s)")
sys.exit(1 if bad else 0)
