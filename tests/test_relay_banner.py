#!/usr/bin/env python3
"""Check the relay mode docker-entrypoint.sh reports in its startup banner, without starting Eden,
for the image's own defaults and for configured, legacy and fallback settings."""
import os
import re
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parent.parent
source = (root / "docker-entrypoint.sh").read_text()
start = source.index('RELAY_MODE_EFFECTIVE="$EDEN_ROOM_RELAY_MODE"')
end = source.index("\n{", start)
decision = source[start:end] + '\nprintf "%s" "$RELAY_MODE_EFFECTIVE"\n'
bash = str(Path(os.environ["ProgramFiles"]) / "Git/bin/bash.exe") if os.name == "nt" else "bash"
relay_variables = ("EDEN_ROOM_RELAY_MODE", "EDEN_ROOM_RELAY_RELIABLE")
# What the Dockerfile's ENV sets; a variable it leaves out is unset in the container.
image = dict(re.findall(r"^\s+(EDEN_ROOM_RELAY_(?:MODE|RELIABLE))=(\S*?)\s*\\?$",
                        (root / "Dockerfile").read_text(), re.M))
assert "EDEN_ROOM_RELAY_MODE" in image, image
base = {key: value for key, value in os.environ.items() if key not in relay_variables}
for settings, expected in [
    (image, "unsequenced"),
    ({"EDEN_ROOM_RELAY_MODE": "reliable", "EDEN_ROOM_RELAY_RELIABLE": "1"}, "reliable"),
    ({"EDEN_ROOM_RELAY_MODE": "", "EDEN_ROOM_RELAY_RELIABLE": "0"}, "unsequenced (compiled-in fallback)"),
    ({}, "unsequenced (compiled-in fallback)"),
    ({"EDEN_ROOM_RELAY_MODE": "", "EDEN_ROOM_RELAY_RELIABLE": "1"}, "reliable (legacy EDEN_ROOM_RELAY_RELIABLE=1)"),
    ({"EDEN_ROOM_RELAY_MODE": "sequenced", "EDEN_ROOM_RELAY_RELIABLE": "0"}, "sequenced"),
]:
    actual = subprocess.check_output([bash], input=decision.encode(), env={**base, **settings}).decode()
    assert actual == expected, (settings, actual)
print("relay banner: image default (unsequenced), configured, legacy and fallback modes passed")
