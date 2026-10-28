"""Child Python interpreter entry for frozen distributions; no bot startup."""
import contextlib
import io
from pathlib import Path
import runpy
import sys
import traceback


class LimitedWriter(io.TextIOBase):
    def __init__(self, stream, limit=256000):
        self.stream, self.remaining = stream, limit
    def write(self, text):
        if self.remaining:
            raw = str(text).encode('utf-8')[:self.remaining]
            self.stream.write(raw.decode('utf-8', errors='ignore'))
            self.stream.flush()
            self.remaining -= len(raw)
        return len(text)
    def flush(self):
        # TextIOBase.__del__ may flush after execute() closes the owned stream.
        if not self.stream.closed:
            self.stream.flush()


def execute(script, output):
    with Path(output).open('w', encoding='utf-8') as stream:
        writer = LimitedWriter(stream)
        with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
            try:
                runpy.run_path(str(Path(script).resolve()), run_name='__main__')
            except SystemExit as exc:
                return int(exc.code or 0) if isinstance(exc.code, (int, type(None))) else 1
            except BaseException:
                traceback.print_exc()
                return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(execute(sys.argv[1], sys.argv[2]))
