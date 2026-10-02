"""Check the actual wrapper's relay banner decision without starting Eden."""
import os
from pathlib import Path
import subprocess


source = Path("docker-entrypoint.sh").read_text()
start = source.index('RELAY_MODE_EFFECTIVE="$EDEN_ROOM_RELAY_MODE"')
end = source.index("\n{", start)
decision = source[start:end] + '\nprintf "%s" "$RELAY_MODE_EFFECTIVE"\n'
bash = str(Path(os.environ["ProgramFiles"]) / "Git/bin/bash.exe") if os.name == "nt" else "bash"
for mode, legacy, expected in [
    ("reliable", "1", "reliable"),
    ("", "0", "unsequenced (compiled-in fallback)"),
    ("", "1", "reliable (legacy EDEN_ROOM_RELAY_RELIABLE=1)"),
    ("sequenced", "0", "sequenced"),
]:
    actual = subprocess.check_output(
        [bash],
        input=decision.encode(),
        env={**os.environ, "EDEN_ROOM_RELAY_MODE": mode, "EDEN_ROOM_RELAY_RELIABLE": legacy},
    ).decode()
    assert actual == expected, (mode, legacy, actual)
print("relay banner: four configured/legacy/fallback cases passed")
