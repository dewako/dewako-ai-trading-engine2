#!/usr/bin/env python3
"""Build a Dewako strategy package ZIP from a folder.

Folder layout:  strategy/rules.json  parameters.json  metadata.json  [tests/*.json]  [README.md]
Usage: make_strategy_package.py <folder> <out.zip> --name "Gold Scalp" --version 1.0 --instruments XAUUSD --timeframes 1m,5m [--author me]
It only hashes and zips data files; it executes nothing. The importer re-validates everything.
"""
import argparse, hashlib, json, pathlib, zipfile

ap = argparse.ArgumentParser()
ap.add_argument("folder"); ap.add_argument("out")
ap.add_argument("--name", required=True); ap.add_argument("--version", required=True)
ap.add_argument("--instruments", required=True); ap.add_argument("--timeframes", required=True)
ap.add_argument("--author", default=""); ap.add_argument("--description", default="")
a = ap.parse_args()
root = pathlib.Path(a.folder)
files = {}
for p in sorted(root.rglob("*")):
    if p.is_file() and p.name != "manifest.json":
        files[p.relative_to(root).as_posix()] = p.read_bytes()
manifest = {"schema_version": 1, "name": a.name, "version": a.version, "runtime": "declarative-rules", "entry": "strategy/rules.json",
            "description": a.description, "author": a.author, "instruments": a.instruments.split(","), "timeframes": a.timeframes.split(","),
            "dependencies": [], "requested_capabilities": [], "files": {k: hashlib.sha256(v).hexdigest() for k, v in files.items()}}
with zipfile.ZipFile(a.out, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("manifest.json", json.dumps(manifest, indent=2))
    for k, v in files.items():
        z.writestr(k, v)
print(f"wrote {a.out} ({len(files)+1} files)")
