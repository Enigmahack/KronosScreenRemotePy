"""Fault-injecting in-memory stand-in for Tools.file_manager._FtpWorker, for scenario tests (SP7 etc.).

Every transport call counts as one op. `fail_at=N` makes op N raise ConnectionError; with `dead=True` every
later op raises too (cable pulled), otherwise only op N fails (a blip). `partial=True` makes a failing
upload leave half the bytes at the destination, as a dropped STOR does on a real server.
`overwrite=False` makes rename refuse an existing target with a 550, as some FTP servers do.
"""
import ftplib
import sys, os
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class FaultyFtp:
    def __init__(self, files=None, fail_at=None, dead=True, partial=True, overwrite=True):
        self.files = dict(files or {})
        self.dirs = {"/" + "/".join(f.strip("/").split("/")[:i]) for f in self.files for i in range(1, f.strip("/").count("/") + 1)}
        self.ops = 0
        self.fail_at = fail_at
        self.dead = dead
        self.partial = partial
        self.overwrite = overwrite   # False models a server whose RNTO refuses an existing target (550)
        self.tripped = False

    def _op(self, name):
        i = self.ops
        self.ops += 1
        if self.fail_at is not None and (i == self.fail_at or (self.dead and self.tripped)):
            self.tripped = True
            raise ConnectionError(f"injected fault at op {i} ({name})")

    def file_exists(self, path):
        self._op("file_exists")
        return path in self.files or path in self.dirs

    def mkdir(self, path):
        self._op("mkdir")
        self.dirs.add(path)

    def upload(self, local, remote):
        data = Path(local).read_bytes()
        if remote.rsplit("/", 1)[0] not in self.dirs:
            raise FileNotFoundError(f"550 no such directory for {remote}")
        try:
            self._op("upload")
        except ConnectionError:
            if self.partial:
                self.files[remote] = data[:len(data) // 2]
            raise
        self.files[remote] = data

    def rename(self, old, new):
        self._op("rename")
        if new in self.files and not self.overwrite:
            raise ftplib.error_perm("550 target exists")
        self.files[new] = self.files.pop(old)

    def delete_file(self, path):
        self._op("delete_file")
        del self.files[path]

    def download(self, remote, local):
        data = self.files[remote]
        try:
            self._op("download")
        except ConnectionError:
            if self.partial:
                Path(local).write_bytes(data[:len(data) // 2])
            raise
        Path(local).write_bytes(data)


class _KronosRawFtp:
    """The ftplib.FTP surface _FtpWorker uses, behaving like the Kronos's server: no MLSD (500 Unknown command), so
    listings come from LIST text; STOR/RENAME need an existing parent folder; `overwrite=False` makes RNTO refuse an
    existing target. Same fault model as FaultyFtp, counted per raw call."""

    def __init__(self, owner):
        self.o = owner

    def _op(self, name):
        self.o._op(name)

    def mlsd(self, path):
        self._op("mlsd")
        raise ftplib.error_perm("500 Unknown command")

    def dir(self, path, cb):
        self._op("list")
        o = self.o
        p = path.rstrip("/") or "/"
        if p != "/" and p not in o.dirs:
            raise ftplib.error_perm(f"550 {path}: no such directory")
        prefix = "" if p == "/" else p
        seen = set()
        for d in sorted(o.dirs):
            if d.rsplit("/", 1)[0] == prefix and d != p:
                seen.add(d)
                cb(f"drwxr-xr-x 2 kronos kronos 4096 Jan  1 00:00 {d.rsplit('/', 1)[1]}")
        for f, data in sorted(o.files.items()):
            if f.rsplit("/", 1)[0] == prefix:
                cb(f"-rw-r--r-- 1 kronos kronos {len(data)} Jan  1 00:00 {f.rsplit('/', 1)[1]}")

    def storbinary(self, cmd, fileobj):
        o = self.o
        remote = cmd[len("STOR "):]
        if remote.rsplit("/", 1)[0] not in o.dirs:
            raise ftplib.error_perm(f"550 no such directory for {remote}")
        data = fileobj.read()
        try:
            self._op("stor")
        except ConnectionError:
            if o.partial:
                o.files[remote] = data[:len(data) // 2]
            raise
        o.files[remote] = data

    def retrbinary(self, cmd, cb, blocksize=8192):
        o = self.o
        remote = cmd[len("RETR "):]
        if remote not in o.files:
            raise ftplib.error_perm(f"550 {remote}: no such file")
        data = o.files[remote]
        try:
            self._op("retr")
        except ConnectionError:
            if o.partial:
                cb(data[:len(data) // 2])
            raise
        cb(data)

    def rename(self, old, new):
        o = self.o
        self._op("rename")
        if old not in o.files:
            raise ftplib.error_perm(f"550 {old}: no such file")
        if new.rsplit("/", 1)[0] not in o.dirs:
            raise ftplib.error_perm("550 no such directory")
        if new in o.files and not o.overwrite:
            raise ftplib.error_perm("550 target exists")
        o.files[new] = o.files.pop(old)

    def delete(self, path):
        self._op("dele")
        if path not in self.o.files:
            raise ftplib.error_perm(f"550 {path}: no such file")
        del self.o.files[path]

    def mkd(self, path):
        self._op("mkd")
        if path in self.o.dirs:
            raise ftplib.error_perm("550 exists")
        self.o.dirs.add(path)

    def rmd(self, path):
        self._op("rmd")
        self.o.dirs.discard(path)


def _kronos_like_class():
    from Tools.file_manager import _FtpWorker

    class KronosLikeFtp(_FtpWorker):
        """A REAL _FtpWorker over _KronosRawFtp, so file_exists / list_dir / rename guards are the production code."""

        def __init__(self, files=None, fail_at=None, dead=True, partial=True, overwrite=True):
            super().__init__("h", 21, "u", "p")
            self.files = dict(files or {})
            self.dirs = {"/" + "/".join(f.strip("/").split("/")[:i]) for f in self.files for i in range(1, f.strip("/").count("/") + 1)}
            self.ops, self.fail_at, self.dead, self.partial, self.overwrite, self.tripped = 0, fail_at, dead, partial, overwrite, False
            self._ftp = _KronosRawFtp(self)

        def ensure_connected(self):
            pass

        _op = FaultyFtp._op

    return KronosLikeFtp


KronosLikeFtp = _kronos_like_class()
SERVERS = {"plain": FaultyFtp, "kronos": KronosLikeFtp}
