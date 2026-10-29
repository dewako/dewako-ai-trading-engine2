"""Builds frontend/prototype/index.standalone.html: the dashboard with CSS and scripts inlined, so it works when that ONE file is opened
directly (file://) or copied somewhere. For local viewing only: the served /ui route keeps external files because its CSP forbids inline script."""
import pathlib, re
d = pathlib.Path(__file__).resolve().parents[1] / "frontend" / "prototype"
h = (d / "index.html").read_text(encoding="utf-8")
h = re.sub(r'<link rel="stylesheet" href="app.css">', lambda m: "<style>\n" + (d / "app.css").read_text(encoding="utf-8") + "\n</style>", h)
for name in ("activity.js", "packages.js", "app.js"):
    js = (d / name).read_text(encoding="utf-8").replace("</script", "<\\/script")
    h = h.replace(f'<script src="{name}"></script>', "<script>\n" + js + "\n</script>")
assert 'src="' not in h.split("</body>")[0].replace('src="data:', ""), "external reference left"
(d / "index.standalone.html").write_text(h, encoding="utf-8")
print("wrote", d / "index.standalone.html", len(h), "bytes")
