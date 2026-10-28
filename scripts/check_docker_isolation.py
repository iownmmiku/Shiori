"""Opt-in real local Docker acceptance. Never substitutes a host subprocess."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from shiori.sandbox import availability, prepare, stop


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--require',action='store_true',help='Fail instead of skip if the local engine/image is absent')
    args=parser.parse_args()
    ready=availability()
    if not ready['available']:
        print(json.dumps({'status':'unavailable','message':ready['message']},ensure_ascii=False))
        return 1 if args.require else 0
    with tempfile.TemporaryDirectory(prefix='shiori-isolation-') as folder:
        base=Path(folder);root=base/'workspace';root.mkdir()
        root.chmod(0o777)
        outside=base/'outside-secret.txt';outside.write_text('must-not-be-visible','utf-8')
        # All probes are non-destructive: an isolated root write, TCP to the
        # documentation-only address range, and reading a disposable marker.
        code=f'''from pathlib import Path
import json, socket
checks={{}}
checks['host_file_hidden']=not Path({str(outside)!r}).exists()
try:
    Path('/isolation-probe').write_text('probe')
    checks['root_read_only']=False
except OSError:
    checks['root_read_only']=True
try:
    socket.create_connection(('192.0.2.1',443),timeout=1).close()
    checks['network_disabled']=False
except OSError:
    checks['network_disabled']=True
Path('/workspace/result.json').write_text(json.dumps(checks))
assert all(checks.values()), checks
'''
        command,identity=prepare(root,base/'history','execute_python',code)
        try:
            result=subprocess.run(command,capture_output=True,timeout=30)
            if result.returncode:
                print(json.dumps({'status':'failed','stage':'container_execution','exit_code':result.returncode}))
                return 1
            checks=json.loads((root/'result.json').read_text('utf-8'))
            print(json.dumps({'status':'passed','checks':checks}))
            return 0 if all(checks.values()) else 1
        finally:
            stop(identity)


if __name__=='__main__':raise SystemExit(main())
