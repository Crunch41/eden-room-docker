#!/usr/bin/env python3
"""Run docker-entrypoint.sh's process handling (everything from cleanup() on) with a stand-in room:
the room's exit status must come through, SIGTERM must reach the room, and the log must be flushed."""
import subprocess
import tempfile
import time
from pathlib import Path

source = (Path(__file__).resolve().parent.parent / "docker-entrypoint.sh").read_text()
tail = source[source.index("cleanup() {"):]
with tempfile.TemporaryDirectory() as tmp:
    for child, terminate, status in [("exit 7", False, 7), ("sleep 30", True, 143)]:
        script = Path(tmp) / "entrypoint.sh"
        script.write_text("#!/bin/bash\nset -euo pipefail\nEDEN_PID=\"\"\nLOG_FILE=\"$1\"\n"
                          f"CMD=(bash -c {child!r})\n" + tail)
        process = subprocess.Popen(["bash", str(script), str(Path(tmp) / "room.log")],
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if terminate:
            time.sleep(0.2)
            process.terminate()
        output, _ = process.communicate(timeout=3)
        assert b"Eden Room Server stopped." in output, output
        assert process.returncode == status, process.returncode
print("entrypoint: exit status and SIGTERM handling passed")
