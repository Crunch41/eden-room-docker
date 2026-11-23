#!/usr/bin/env python3
"""The stats page's script must parse: a syntax error leaves the page stuck on "Loading…",
and the image tests only check that the page is served. Needs Node.js (node --check)."""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

page = (Path(__file__).resolve().parent.parent / "stats" / "index.html").read_text(encoding="utf-8")
scripts = re.findall(r"<script>(.*?)</script>", page, re.S)
assert scripts, "no <script> in stats/index.html"
with tempfile.TemporaryDirectory() as tmp:
    for i, script in enumerate(scripts):
        path = Path(tmp) / f"script{i}.js"
        path.write_text(script, encoding="utf-8")
        result = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
        if result.returncode != 0:
            sys.exit(f"stats/index.html script {i} doesn't parse:\n{result.stderr}")
print(f"stats page: {len(scripts)} script(s) parse")
