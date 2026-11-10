"""Check actual copy independence and offline display isolation on disposable bytes."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import tempfile

from logos_desktop import runtime_boundary, runtime_copies, runtime_trials


PROBE = r'''
import errno
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import time

job = json.loads(Path('/trial/probe.json').read_text())
prefix = Path(os.environ['WINEPREFIX'])
info = prefix.stat()
assert [info.st_dev, info.st_ino] == job['copy_identity']
assert not Path(job['host_canary']).exists()
assert not Path('/run/dbus/system_bus_socket').exists()
assert not Path('/run/user').exists()
assert not Path('/etc/ssl/private').exists()
assert 'DBUS_SESSION_BUS_ADDRESS' not in os.environ
denials = []
for kind in (socket.SOCK_DGRAM, socket.SOCK_STREAM):
    with socket.socket(socket.AF_INET, kind) as connection:
        try:
            connection.connect(('198.51.100.53', 53))
        except OSError as error:
            assert error.errno == errno.ENETUNREACH
            denials.append(error.errno)
        else:
            raise RuntimeError('Unexpected network route')
with socket.socket() as connection:
    connection.settimeout(1)
    assert connection.connect_ex(('127.0.0.1', job['port'])) != 0
for path in Path('/proc/self/fd').iterdir():
    try:
        target = os.readlink(path)
    except FileNotFoundError:
        continue
    assert target != job['host_canary']
    assert not target.startswith('socket:')
url = 'http://127.0.0.1:' + str(job['port']) + '/trial-canary'
attempts = []
for command in (['xdg-open', url], ['gio', 'open', url]):
    result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, close_fds=True, timeout=5)
    attempts.append(result.returncode)
time.sleep(0.5)
assert Path('/trial/url-denials').read_text().splitlines()
with sqlite3.connect(prefix / 'database') as database:
    assert database.execute('SELECT value FROM trial_canary').fetchone() == ('original database',)
    database.execute('UPDATE trial_canary SET value = ?', ('private database mutation',))
database.close()
Path('/trial/result.json').write_text(json.dumps({'network_denials': denials, 'url_denials': attempts,
                                                'copy_identity': job['copy_identity']}))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    parent = runtime_copies.checked_directory(args.parent)
    with tempfile.TemporaryDirectory(prefix="trial-boundary-", dir=parent) as temporary:
        root = Path(temporary)
        source = root / "source"
        source.mkdir()
        with sqlite3.connect(source / "database") as database:
            database.execute("CREATE TABLE trial_canary (value TEXT NOT NULL)")
            database.execute("INSERT INTO trial_canary VALUES (?)", ("original database",))
        database.close()
        canary = root / "host-canary"
        canary.write_text("host unchanged")
        before = runtime_copies.manifest(source)
        receipt = runtime_copies.copy(source, root / "copy")
        private = root / "private"
        runtime_boundary.prepare_private(private)
        boundary = runtime_boundary.inspect((source,))
        # The display-only check never executes this placeholder as a runner.
        placeholder = root / "unused-runner"
        placeholder.write_bytes(b"not executed")
        worker = Path(runtime_trials.__file__).with_name("runtime_trial_worker.py")
        arguments = dict(source=source, copied=root / "copy", private=private,
                         runner=placeholder, worker=worker)
        runtime_boundary.probe(runtime_boundary.command(boundary, **arguments, check=True))
        probe = root / "probe.py"
        probe.write_text(PROBE)
        copied = (root / "copy").stat()
        with socket.socket() as listener, canary.open("r+b") as inherited:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(0.1)
            os.set_inheritable(listener.fileno(), True)
            os.set_inheritable(inherited.fileno(), True)
            runtime_trials.write(private / "probe.json", {
                "copy_identity": [copied.st_dev, copied.st_ino], "host_canary": str(canary),
                "port": listener.getsockname()[1],
            })
            arguments["worker"] = probe
            command = runtime_boundary.command(boundary, **arguments)
            executed = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.PIPE, close_fds=True, check=False, timeout=30)
            if executed.returncode:
                raise RuntimeError(executed.stderr[-8192:].decode("utf-8", errors="replace"))
            try:
                peer, _ = listener.accept()
            except TimeoutError:
                pass
            else:
                peer.close()
                raise RuntimeError("Host listener received a namespace launch")
        assert runtime_copies.manifest(source) == before
        assert canary.read_text() == "host unchanged"
        with sqlite3.connect((source / "database").as_uri() + "?mode=ro", uri=True) as database:
            assert database.execute("SELECT value FROM trial_canary").fetchone() == ("original database",)
        database.close()
        with sqlite3.connect((root / "copy/database").as_uri() + "?mode=ro", uri=True) as database:
            assert database.execute("SELECT value FROM trial_canary").fetchone() == ("private database mutation",)
        database.close()
        result = json.loads((private / "result.json").read_text())
        result.update(copy=receipt, source_unchanged=True, host_canary_unchanged=True,
                      copied_sqlite_change_verified=True,
                      boundary_revision=runtime_boundary.REVISION,
                      display_dependencies=dict(boundary.dependencies),
                      worker_sha256=hashlib.sha256(worker.read_bytes()).hexdigest(),
                      scope="Synthetic bytes and actual private display, no Wine or Logos execution")
        runtime_trials.write(args.report, result)


if __name__ == "__main__":
    main()
