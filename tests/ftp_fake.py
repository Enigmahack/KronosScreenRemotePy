"""One faithful fake of the instruments' FTP server, for every FTP test.

Both the Kronos and the Nautilus run the same vsftpd ("220 Welcome to the Korg Kronos/Nautilus FTP service.",
"215 UNIX Type: L8"). Everything below was measured live on both units on 2026-11-13 (Kronos 192.168.100.15
/SSD2/CLAUDETEST_K, Nautilus 192.168.100.27 /SSD1, throwaway SWEEP folders, removed afterwards); the same facts are
asserted by tests/ftp_contract.py, which can be pointed at a live unit to catch drift (KRONOS_FTP_CONTRACT).

  MLSD                      500 Unknown command.            (callers must fall back to LIST)
  LIST <folder>             vsftpd ls lines, sorted by name (byte order), dotfiles hidden; `LIST -a` shows them
  LIST <missing folder>     226, EMPTY listing - never a 550. A permission problem is indistinguishable from it.
  LIST <file>               that one file's line
  LIST <glob>               `*` and `?` in the last component filter the parent folder
  STOR                      creates or overwrites; into a missing folder / onto a folder: 553 Could not create file.
  RETR / SIZE               missing or a folder: 550 Failed to open file. / 550 Could not get file size.
  DELE                      missing or a folder: 550 Delete operation failed.
  RNFR                      missing: 550 RNFR command failed.
  RNTO                      onto an existing FILE: succeeds and silently replaces it (also onto a case variant's
                            exact name, and onto a hidden dotfile); onto an existing folder, a folder onto a file,
                            a folder into its own subfolder, into a missing folder: 550 Rename failed.
                            Renaming a folder moves its subtree; renaming a path onto itself succeeds.
  MKD                       existing name (file or folder) or missing parent: 550 Create directory operation failed.
  RMD                       missing, non-empty (a hidden dotfile counts), or a file: 550 Remove directory operation failed.
  CWD                       missing: 550 Failed to change directory.
  Names                     CASE-SENSITIVE: a.txt and A.TXT coexist, RETR/SIZE/CWD/LIST of a case variant fail or
                            come back empty. Spaces, '[', '*' in names work. '//', '..', relative paths resolve
                            like a POSIX path from '/'.

Not measured (deliberately not tried outside the scratch folder): writing into '/' itself. The fake refuses
STOR/MKD/RNTO there and RMD/RNFR of a volume, as a chrooted vsftpd with a root-owned '/' would.

Configurable, NOT the measured default:
  case_sensitive=False      a FAT-like volume (case-insensitive, case-preserving)
  overwrite=False           a hypothetical server whose RNTO refuses an existing file (550 Rename failed.), kept so
                            the sample push's swap-aside fallback stays covered
Test hooks: fail_at / dead / partial (a connection drop at raw command N; dead = every later command and reconnect
fails too; partial = a dropped STOR/RETR leaves half the bytes), `fault(cmd, path)` predicate returning an exception to
raise, `delays={cmd: seconds}`, and `log` (one Event per raw command, with thread and start/end times).

`FakeFtpWorker` is the REAL Tools.file_manager._FtpWorker over this server, so staging, the MLSD->LIST fallback,
file_exists, delete_dir and the RLock are the production code.
"""
from __future__ import annotations

import ftplib
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from Tools.file_manager import _FtpWorker as _RealFtpWorker   # captured before any test monkeypatches the module

WELCOME = "220 Welcome to the Korg Kronos FTP service."
_DATE = "Nov 13 13:49"

MSG = {
    "MLSD": "500 Unknown command.",
    "STOR": "553 Could not create file.",
    "RETR": "550 Failed to open file.",
    "SIZE": "550 Could not get file size.",
    "DELE": "550 Delete operation failed.",
    "RNFR": "550 RNFR command failed.",
    "RNTO": "550 Rename failed.",
    "MKD": "550 Create directory operation failed.",
    "RMD": "550 Remove directory operation failed.",
    "CWD": "550 Failed to change directory.",
}
MUTATING = ("STOR", "DELE", "RNFR", "RNTO", "MKD", "RMD")


def norm(path: str, cwd: str = "/") -> str:
    """Resolve like the server does: relative to cwd, '//' collapsed, '.' dropped, '..' clamped at '/'."""
    raw = path if path.startswith("/") else f"{cwd.rstrip('/')}/{path}"
    out: List[str] = []
    for seg in raw.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if out:
                out.pop()
            continue
        out.append(seg)
    return "/" + "/".join(out)


def _parent(p: str) -> str:
    return p.rsplit("/", 1)[0] or "/"


def _base(p: str) -> str:
    return p.rsplit("/", 1)[-1]


def _glob_match(pattern: str, name: str) -> bool:
    """vsftpd's filter: '*' and '?' only (a '[' is literal - 'br[1]' was listed as a folder name)."""
    if not pattern:
        return not name
    if pattern[0] == "*":
        return any(_glob_match(pattern[1:], name[i:]) for i in range(len(name) + 1))
    if name and (pattern[0] == "?" or pattern[0] == name[0]):
        return _glob_match(pattern[1:], name[1:])
    return False


@dataclass
class Event:
    cmd: str
    args: tuple
    worker_thread: bool
    t0: float
    t1: Optional[float] = None
    error: Optional[str] = None


class FtpFakeServer:
    """The instrument's filesystem + its FTP command semantics. `files` maps absolute path -> bytes, `dirs` is the
    set of absolute folder paths ('/' always present); both are live and may be inspected or seeded by tests."""

    def __init__(self, files: Optional[Dict[str, bytes]] = None, dirs: Iterable[str] = (), *,
                 case_sensitive: bool = True, overwrite: bool = True, volumes: Iterable[str] = (),
                 fail_at: Optional[int] = None, dead: bool = True, partial: bool = True,
                 fault: Optional[Callable[[str, str], Optional[BaseException]]] = None,
                 delays: Optional[Dict[str, float]] = None):
        self.case_sensitive = case_sensitive
        self.overwrite = overwrite
        self.files: Dict[str, bytes] = {}
        self.dirs = {"/"}
        for d in list(dirs) + [f"/{v.strip('/')}" for v in volumes]:
            self._add_dir_chain(norm(d))
        for p, data in (files or {}).items():
            p = norm(p)
            self._add_dir_chain(_parent(p))
            self.files[p] = bytes(data)
        self.ops = 0
        self.fail_at, self.dead, self.partial, self.fault = fail_at, dead, partial, fault
        self.tripped = False
        self.delays = dict(delays or {})
        self.log: List[Event] = []
        self._lock = threading.Lock()   # the server is single-threaded per command, like the daemon's vsftpd child

    # -- seeding / inspection -----------------------------------------------------------------------------------
    def _add_dir_chain(self, d: str) -> None:
        while d not in self.dirs:
            self.dirs.add(d)
            d = _parent(d)

    def calls(self, *cmds: str) -> List[Event]:
        """Logged commands, optionally only these command names."""
        return [e for e in self.log if not cmds or e.cmd in cmds]

    def mutations(self) -> List[tuple]:
        return [(e.cmd, *e.args) for e in self.log if e.cmd in MUTATING]

    # -- name resolution ----------------------------------------------------------------------------------------
    def _same(self, a: str, b: str) -> bool:
        return a == b if self.case_sensitive else a.casefold() == b.casefold()

    def _children(self, d: str):
        """(name, path, is_dir) of the entries directly in folder d (actual stored spelling)."""
        for x in list(self.dirs):
            if x != "/" and _parent(x) == d:
                yield _base(x), x, True
        for x in list(self.files):
            if _parent(x) == d:
                yield _base(x), x, False

    def resolve(self, path: str) -> Optional[str]:
        """The stored spelling of an existing file/folder, or None."""
        p = norm(path)
        if self.case_sensitive:
            return p if (p in self.dirs or p in self.files) else None
        cur = "/"
        for seg in [s for s in p.split("/") if s]:
            if cur not in self.dirs:
                return None
            nxt = next((x for n, x, _ in self._children(cur) if self._same(n, seg)), None)
            if nxt is None:
                return None
            cur = nxt
        return cur

    def is_dir(self, path: str) -> bool:
        r = self.resolve(path)
        return r is not None and r in self.dirs

    def is_file(self, path: str) -> bool:
        r = self.resolve(path)
        return r is not None and r in self.files

    def _new_path(self, path: str) -> Optional[str]:
        """Where a new entry named by `path` would be stored (parent's stored spelling + given name), or None when
        the parent folder doesn't exist."""
        p = norm(path)
        par = self.resolve(_parent(p))
        if par is None or par not in self.dirs:
            return None
        return f"{'' if par == '/' else par}/{_base(p)}"

    # -- command plumbing ---------------------------------------------------------------------------------------
    def begin(self, conn: "FakeFTP", cmd: str, *args) -> Event:
        ev = Event(cmd, args, threading.current_thread() is not threading.main_thread(), time.monotonic())
        self.log.append(ev)
        i = self.ops
        self.ops += 1
        if conn is not None and conn.broken:
            ev.error, ev.t1 = "broken", time.monotonic()
            raise ConnectionError(f"connection already dropped ({cmd})")
        if self.fail_at is not None and (i == self.fail_at or (self.dead and self.tripped)):
            self.tripped = True
            if conn is not None:
                conn.broken = True
            ev.error, ev.t1 = "injected", time.monotonic()
            raise ConnectionError(f"injected fault at op {i} ({cmd})")
        if self.fault is not None:
            ex = self.fault(cmd, args[0] if args else "")
            if ex is not None:
                ev.error, ev.t1 = repr(ex), time.monotonic()
                if isinstance(ex, (ConnectionError, EOFError, TimeoutError)) and conn is not None:
                    conn.broken = True
                raise ex
        d = self.delays.get(cmd)
        if d:
            time.sleep(d)
        return ev

    def refuse(self, ev: Event, cmd: str):
        ev.error, ev.t1 = MSG[cmd], time.monotonic()
        raise ftplib.error_perm(MSG[cmd])

    # -- LIST rendering -----------------------------------------------------------------------------------------
    def _line(self, name: str, path: str, is_dir: bool) -> str:
        if is_dir:
            subdirs = sum(1 for _, _, d in self._children(path) if d)
            mode = "drwxrwxr-x" if _parent(path) == "/" else "drwxrwxrwx"
            nlink, size = 2 + subdirs, 4096
        else:
            mode, nlink, size = "-rw-rw-rw-", 1, len(self.files[path])
        return f"{mode} {nlink:4d} {'500':<8} {'500':<8} {size:8d} {_DATE} {name}"

    def listing(self, arg: str) -> List[str]:
        show_all = False
        arg = arg.strip()
        while arg.startswith("-"):
            opt, _, arg = arg.partition(" ")
            show_all = show_all or "a" in opt
            arg = arg.strip()
        target = arg or "/"
        r = self.resolve(target)
        if r is not None and r in self.dirs:
            rows = sorted((n, x, d) for n, x, d in self._children(r))
            out = []
            if show_all:
                out += [self._line(".", r, True), self._line("..", _parent(r), True)]
            out += [self._line(n, x, d) for n, x, d in rows if show_all or not n.startswith(".")]
            return out
        if r is not None:
            return [self._line(_base(r), r, False)]
        p = norm(target)
        par = self.resolve(_parent(p))
        pat = _base(p)
        if par is None or par not in self.dirs or not any(c in pat for c in "*?"):
            return []
        return [self._line(n, x, d) for n, x, d in sorted(self._children(par))
                if (show_all or not n.startswith(".")) and _glob_match(pat, n)]


class FakeFTP:
    """The ftplib.FTP surface (one control connection) over an FtpFakeServer."""

    def __init__(self, server: FtpFakeServer):
        self.s = server
        self.broken = False
        self.cwd_path = "/"
        self._rnfr: Optional[str] = None
        self.welcome = WELCOME

    def getwelcome(self):
        return self.welcome

    def _go(self, cmd, *args):
        return self.s.begin(self, cmd, *args)

    @staticmethod
    def _done(ev):
        ev.t1 = time.monotonic()

    def _abs(self, p):
        return norm(p, self.cwd_path)

    # session
    def voidcmd(self, cmd):
        return self.sendcmd(cmd)

    def sendcmd(self, cmd):
        verb, _, arg = cmd.partition(" ")
        verb = verb.upper()
        ev = self._go(verb, arg)
        s = self.s
        try:
            if verb == "NOOP":
                return "200 NOOP ok."
            if verb == "SYST":
                return "215 UNIX Type: L8"
            if verb == "PWD":
                return f'257 "{self.cwd_path}" is the current directory'
            if verb == "FEAT":
                return "211-Features:\n EPRT\n EPSV\n MDTM\n PASV\n REST STREAM\n SIZE\n TVFS\n UTF8\n211 End"
            if verb == "MDTM":
                if not s.is_file(self._abs(arg)):
                    raise ftplib.error_perm("550 Could not get file modification time.")
                return "213 20261113134900"
            if verb == "MLSD":
                raise ftplib.error_perm(MSG["MLSD"])
            raise ftplib.error_perm("500 Unknown command.")
        finally:
            self._done(ev)

    def quit(self):
        ev = self._go("QUIT")
        self._done(ev)
        self.broken = True
        return "221 Goodbye."

    def close(self):
        self.broken = True

    def set_pasv(self, _on):
        pass

    def pwd(self):
        return self.cwd_path

    def cwd(self, path):
        ev = self._go("CWD", path)
        try:
            r = self.s.resolve(self._abs(path))
            if r is None or r not in self.s.dirs:
                self.s.refuse(ev, "CWD")
            self.cwd_path = r
            return "250 Directory successfully changed."
        finally:
            self._done(ev)

    # listing
    def mlsd(self, path="", facts=()):
        ev = self._go("MLSD", path)
        self._done(ev)
        raise ftplib.error_perm(MSG["MLSD"])
        yield  # pragma: no cover - a generator, like ftplib's: the error surfaces on first iteration

    def dir(self, *args):
        cb = args[-1] if args and callable(args[-1]) else print
        arg = " ".join(a for a in args if isinstance(a, str))
        ev = self._go("LIST", arg)
        try:
            opts, path = "", arg
            while path.startswith("-"):
                o, _, path = path.partition(" ")
                opts += o + " "
            for line in self.s.listing(opts + (self._abs(path) if path else self.cwd_path)):
                cb(line)
            return "226 Directory send OK."
        finally:
            self._done(ev)

    def nlst(self, *args):
        arg = " ".join(args)
        ev = self._go("NLST", arg)
        try:
            p = self._abs(arg) if arg else self.cwd_path
            r = self.s.resolve(p)
            names = [ln.split(None, 8)[8] for ln in self.s.listing(p)]
            if r is not None and r in self.s.dirs:
                return [f"{p.rstrip('/')}/{n}" if arg else n for n in names]
            return [f"{_parent(p).rstrip('/')}/{n}" for n in names]
        finally:
            self._done(ev)

    def size(self, path):
        ev = self._go("SIZE", path)
        try:
            r = self.s.resolve(self._abs(path))
            if r is None or r not in self.s.files:
                self.s.refuse(ev, "SIZE")
            return len(self.s.files[r])
        finally:
            self._done(ev)

    # transfer
    def storbinary(self, cmd, fp, blocksize=8192, callback=None, rest=None):
        path = cmd[len("STOR "):]
        data = fp.read()
        s = self.s
        try:
            ev = self._go("STOR", path)
        except ConnectionError:
            tgt = s._new_path(self._abs(path))
            if s.partial and tgt is not None and tgt not in s.dirs:
                s.files[s.resolve(tgt) or tgt] = data[:len(data) // 2]
            raise
        try:
            p = self._abs(path)
            existing = s.resolve(p)
            if existing is not None:
                if existing in s.dirs:
                    s.refuse(ev, "STOR")
                tgt = existing                   # overwrite in place (keeps the stored spelling on a FAT-like volume)
            else:
                tgt = s._new_path(p)
                if tgt is None or norm(_parent(tgt)) == "/":
                    s.refuse(ev, "STOR")
            s.files[tgt] = bytes(data)
            if callback:
                callback(data)
            return "226 Transfer complete."
        finally:
            self._done(ev)

    def retrbinary(self, cmd, callback, blocksize=8192, rest=None):
        path = cmd[len("RETR "):]
        s = self.s
        r = s.resolve(self._abs(path))
        data = s.files.get(r) if r is not None else None
        try:
            ev = self._go("RETR", path)
        except ConnectionError:
            if s.partial and data:
                callback(data[:len(data) // 2])
            raise
        try:
            if data is None:
                s.refuse(ev, "RETR")
            for i in range(0, len(data), blocksize):
                callback(data[i:i + blocksize])
            return "226 Transfer complete."
        finally:
            self._done(ev)

    # changes
    def delete(self, path):
        ev = self._go("DELE", path)
        try:
            r = self.s.resolve(self._abs(path))
            if r is None or r not in self.s.files:
                self.s.refuse(ev, "DELE")
            del self.s.files[r]
            return "250 Delete operation successful."
        finally:
            self._done(ev)

    def mkd(self, path):
        ev = self._go("MKD", path)
        s = self.s
        try:
            p = self._abs(path)
            tgt = s._new_path(p)
            if s.resolve(p) is not None or tgt is None or _parent(tgt) == "/":
                s.refuse(ev, "MKD")
            s.dirs.add(tgt)
            return tgt
        finally:
            self._done(ev)

    def rmd(self, path):
        ev = self._go("RMD", path)
        s = self.s
        try:
            r = s.resolve(self._abs(path))
            if r is None or r not in s.dirs or r == "/" or _parent(r) == "/" or any(True for _ in s._children(r)):
                s.refuse(ev, "RMD")
            s.dirs.discard(r)
            return "250 Remove directory operation successful."
        finally:
            self._done(ev)

    def rename(self, old, new):
        s = self.s
        ev = self._go("RNFR", old)
        try:
            src = s.resolve(self._abs(old))
            if src is None or src == "/" or _parent(src) == "/":
                s.refuse(ev, "RNFR")
        finally:
            self._done(ev)
        ev = self._go("RNTO", new)
        try:
            p = self._abs(new)
            dst_existing = s.resolve(p)
            tgt = s._new_path(p)
            if tgt is None or _parent(tgt) == "/":
                s.refuse(ev, "RNTO")
            if dst_existing == src and (s.case_sensitive or _base(tgt) == _base(src)):
                return "250 Rename successful."          # onto itself: a no-op
            if src in s.dirs:
                if dst_existing is not None and dst_existing != src:
                    s.refuse(ev, "RNTO")                  # a folder onto a file or onto another folder
                if (tgt + "/").startswith(src + "/") and tgt != src:
                    s.refuse(ev, "RNTO")                  # into its own subfolder
                moved_d = sorted(d for d in s.dirs if d == src or d.startswith(src + "/"))
                moved_f = sorted(f for f in s.files if f.startswith(src + "/"))
                for d in moved_d:
                    s.dirs.discard(d)
                for d in moved_d:
                    s.dirs.add(tgt + d[len(src):])
                for f in moved_f:
                    s.files[tgt + f[len(src):]] = s.files.pop(f)
                return "250 Rename successful."
            if dst_existing is not None and dst_existing != src:
                if dst_existing in s.dirs or not s.overwrite:
                    s.refuse(ev, "RNTO")
                del s.files[dst_existing]                 # RNTO over an existing file replaces it silently
            data = s.files.pop(src)
            s.files[tgt] = data
            return "250 Rename successful."
        finally:
            self._done(ev)


class FakeFtpWorker(_RealFtpWorker):
    """The real _FtpWorker over a FakeFTP connection to `server` (a new one is made from the remaining arguments
    when not given). `files`, `dirs`, `ops`, `log` proxy the server, so it drops in for the old ad-hoc fakes."""

    def __init__(self, files: Optional[Dict[str, bytes]] = None, *, server: Optional[FtpFakeServer] = None,
                 host: str = "fake", port: int = 21, user: str = "u", password: str = "p", **server_kw):
        super().__init__(host, port, user, password)
        self.server = server if server is not None else FtpFakeServer(files, **server_kw)
        self.connections = 0

    def connect(self):
        with self._lock:
            self.server.begin(None, "CONNECT")
            self.connections += 1
            self._ftp = FakeFTP(self.server)

    @classmethod
    def bound_to(cls, server: FtpFakeServer):
        """A class with _FtpWorker's own constructor signature, for `module._FtpWorker = ...` monkeypatching."""

        class Bound(cls):
            def __init__(self, host="fake", port=21, user="u", password="p"):
                super().__init__(server=server, host=host, port=port, user=user, password=password)
        return Bound

    files = property(lambda self: self.server.files)
    dirs = property(lambda self: self.server.dirs)
    ops = property(lambda self: self.server.ops)
    log = property(lambda self: self.server.log)
    tripped = property(lambda self: self.server.tripped)
