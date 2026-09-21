"""Exercise the actual entrypoint tail without starting a public room."""
from pathlib import Path
import subprocess
import tempfile
import time

source=Path('docker-entrypoint.sh').read_text()
tail=source[source.index('cleanup() {'):]
with tempfile.TemporaryDirectory() as tmp:
    for child, signal in [('exit 7',False),('sleep 30',True)]:
        script='#!/bin/bash\nset -euo pipefail\nEDEN_PID=""\nLOG_FILE="$1"\nCMD=(bash -c '+repr(child)+')\n'+tail
        path=Path(tmp)/'entrypoint.sh'
        path.write_text(script)
        p=subprocess.Popen(['bash',str(path),str(Path(tmp)/'room.log')],stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        if signal:
            time.sleep(0.2)
            p.terminate()
        output,_=p.communicate(timeout=3)
        assert b'Eden Room Server stopped.' in output,output
        assert p.returncode==(143 if signal else 7),p.returncode
print('normal exit and SIGTERM flush/exit: passed')
