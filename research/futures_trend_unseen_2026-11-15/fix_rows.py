"""Drop rows whose close time is not open + 5m - 1 (exchange-outage artefacts), as done for BTC/ETH/SOL. Records every dropped row."""
import csv, io, json, sys, zipfile
src, dst, rep = sys.argv[1:4]
dropped, kept = [], 0
with zipfile.ZipFile(src) as zi, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zo:
    for name in sorted(zi.namelist()):
        if not name.endswith(".csv"): continue
        rows = list(csv.reader(io.StringIO(zi.read(name).decode())))
        out = []
        for r in rows:
            if not r or not r[0].isdigit(): out.append(r); continue
            o, c = int(r[0]), int(r[6]); unit = 1000 if o > 10**14 else 1
            if c != o + 300_000 * unit - 1:
                dropped.append(dict(file=name, open_ms=o // unit, close_ms=c // unit, duration_s=(c - o) / unit / 1000)); continue
            out.append(r); kept += 1
        b = io.StringIO(); csv.writer(b, lineterminator="\n").writerows(out); zo.writestr(name, b.getvalue())
json.dump(dropped, open(rep, "w"), indent=1)
print(f"{src}: kept {kept} rows, dropped {len(dropped)}")
