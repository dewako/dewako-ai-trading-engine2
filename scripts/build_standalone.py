"""Rebuild frontend/prototype/index.standalone.html (one file with the CSS and every script inlined) from index.html and its sources.
Run it after editing any dashboard file; the unit test `test_standalone_is_current` fails when it is stale."""
import pathlib
import re

UI = pathlib.Path(__file__).resolve().parents[1] / "frontend" / "prototype"


def build() -> str:
    out = (UI / "index.html").read_text()
    css = (UI / "app.css").read_text()
    out = out.replace('<link rel="stylesheet" href="app.css">', "<style>\n" + css.rstrip("\n") + "\n\n</style>")
    for m in re.findall(r'<script src="([^"]+)"></script>', out):
        out = out.replace(f'<script src="{m}"></script>', "<script>\n" + (UI / m).read_text().rstrip("\n") + "\n\n</script>")
    return out


if __name__ == "__main__":
    (UI / "index.standalone.html").write_text(build())
    print("rebuilt", UI / "index.standalone.html")
