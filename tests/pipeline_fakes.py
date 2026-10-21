"""Fault-injecting in-memory stand-in for Tools.file_manager._FtpWorker, for scenario tests (SP7 etc.).

Every transport call counts as one op. `fail_at=N` makes op N raise ConnectionError; with `dead=True` every
later op raises too (cable pulled), otherwise only op N fails (a blip). `partial=True` makes a failing
upload leave half the bytes at the destination, as a dropped STOR does on a real server.
"""
from pathlib import Path


class FaultyFtp:
    def __init__(self, files=None, fail_at=None, dead=True, partial=True):
        self.files = dict(files or {})
        self.dirs = {"/" + "/".join(f.strip("/").split("/")[:i]) for f in self.files for i in range(1, f.strip("/").count("/") + 1)}
        self.ops = 0
        self.fail_at = fail_at
        self.dead = dead
        self.partial = partial
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
