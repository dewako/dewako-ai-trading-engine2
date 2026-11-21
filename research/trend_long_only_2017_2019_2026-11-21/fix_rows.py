"""Drop exchange-outage artefact rows from Binance 5m monthly archives and record every one with its reason.
Same rule as ../futures_trend_unseen_2026-11-15/fix_rows.py (close time is not open + 5m - 1 ms), plus a second class found when the
2017-2019 files were imported: an open time that is not on the 5-minute grid (BTC/ETH/BNB, 2018-02-09 09:58:14.789 UTC; the importer
refuses the whole month for it). Nothing is repaired or guessed."""
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
            om, cm = o // unit, c // unit
            reason = "close time is not open + 5m - 1ms" if c != o + 300_000 * unit - 1 else ("open time is not on the 5m grid" if om % 300_000 else "")
            if reason:
                dropped.append(dict(file=name, open_ms=om, close_ms=cm, reason=reason)); continue
            out.append(r); kept += 1
        b = io.StringIO(); csv.writer(b, lineterminator="\n").writerows(out); zo.writestr(name, b.getvalue())
json.dump(dropped, open(rep, "w"), indent=1)
by = {}
for d in dropped: by[d["reason"]] = by.get(d["reason"], 0) + 1
print(f"{src.split('/')[-1]}: kept {kept} rows, dropped {len(dropped)} {by}")
