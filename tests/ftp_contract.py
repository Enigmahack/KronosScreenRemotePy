"""The instruments' FTP server behaviour as executable facts, run against tests/ftp_fake.py by default and against a
real unit when KRONOS_FTP_CONTRACT=host,user,pass,basefolder is set, so the fake can't silently drift from the real
server. Every fact was first measured on a Kronos and a Nautilus (2026-11-13).

  python tests/ftp_contract.py                                    # against the fake
  KRONOS_FTP_CONTRACT=192.168.100.15,kronos,kronos,/SSD2/CLAUDETEST_K python tests/ftp_contract.py
  (Git Bash on Windows: export MSYS_NO_PATHCONV=1 first, or the leading-slash folder is rewritten)

Live mode works only inside a fresh CONTRACT_<hex> folder under basefolder and removes it (and confirms it is gone).
`run_contract(conn, base)` takes any ftplib.FTP-like connection; it returns the list of failed facts.
"""
from __future__ import annotations

import ftplib
import io
import os
import re
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _stor(f, path, data):
    return f.storbinary(f"STOR {path}", io.BytesIO(data))


def _retr(f, path):
    b = bytearray()
    f.retrbinary(f"RETR {path}", b.extend)
    return bytes(b)


def _ls(f, path):
    lines = []
    f.dir(path, lines.append)
    return lines


def _names(f, path):
    return [ln.split(None, 8)[8] for ln in _ls(f, path)]


def _perm(fn, *a):
    """The error_perm text a call raises, or None if it succeeded."""
    try:
        fn(*a)
    except ftplib.error_perm as ex:
        return str(ex)
    return None


def run_contract(f, base: str, verbose: bool = True):
    fails = []
    S = f"{base.rstrip('/')}/CONTRACT_{uuid.uuid4().hex[:6]}"

    def fact(name, got, want):
        ok = got == want
        if verbose:
            print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"   got {got!r}, want {want!r}"))
        if not ok:
            fails.append(name)

    try:
        fact("MLSD is not supported", _perm(lambda: list(f.mlsd(base))), "500 Unknown command.")
        fact("MKD returns the created path", f.mkd(S), S)
        fact("MKD of an existing folder", _perm(f.mkd, S), "550 Create directory operation failed.")
        fact("LIST of an empty folder", _ls(f, S), [])
        fact("LIST of a missing folder is an empty success, not 550", _ls(f, f"{S}/NOFOLDER"), [])
        fact("LIST of a missing nested folder", _ls(f, f"{S}/NOFOLDER/DEEP"), [])

        _stor(f, f"{S}/a.txt", b"lower")
        line = _ls(f, S)
        fact("LIST line format (vsftpd ls -l)", line and line[0].split()[0:5] + [line[0].split(None, 8)[8]],
             ["-rw-rw-rw-", "1", "500", "500", "5", "a.txt"])
        fact("LIST line spacing", line and re.match(r"(.*?) [A-Z][a-z]{2} ", line[0]).group(1),
             "-rw-rw-rw-    1 500      500             5")
        fact("RETR of a case variant fails (case-sensitive)", _perm(_retr, f, f"{S}/A.TXT"), "550 Failed to open file.")
        fact("SIZE of a case variant fails", _perm(f.size, f"{S}/A.TXT"), "550 Could not get file size.")
        fact("SIZE of a file", f.size(f"{S}/a.txt"), 5)
        _stor(f, f"{S}/A.TXT", b"UPPER!")
        fact("case variants coexist", _names(f, S), ["A.TXT", "a.txt"])
        fact("each case variant keeps its own bytes", (_retr(f, f"{S}/a.txt"), _retr(f, f"{S}/A.TXT")),
             (b"lower", b"UPPER!"))
        fact("LIST of a case-variant folder is empty", _ls(f, S.rsplit("/", 1)[0] + "/" + S.rsplit("/", 1)[1].lower()), [])
        fact("CWD to a case-variant folder fails",
             _perm(f.cwd, S.rsplit("/", 1)[0] + "/" + S.rsplit("/", 1)[1].lower()), "550 Failed to change directory.")
        f.cwd("/")

        f.rename(f"{S}/a.txt", f"{S}/b.txt")
        _stor(f, f"{S}/B.TXT", b"UPPER-B")
        fact("RNTO case-only onto an existing case variant replaces it", f.rename(f"{S}/b.txt", f"{S}/B.TXT"),
             "250 Rename successful.")
        fact("... and the replaced file is gone", (_names(f, S), _retr(f, f"{S}/B.TXT")), (["A.TXT", "B.TXT"], b"lower"))
        _stor(f, f"{S}/c.txt", b"ccc")
        fact("RNTO over an existing file succeeds", f.rename(f"{S}/c.txt", f"{S}/A.TXT"), "250 Rename successful.")
        fact("... and silently replaces it", (_names(f, S), _retr(f, f"{S}/A.TXT")), (["A.TXT", "B.TXT"], b"ccc"))
        fact("RNTO onto itself succeeds", f.rename(f"{S}/A.TXT", f"{S}/A.TXT"), "250 Rename successful.")
        _stor(f, f"{S}/A.TXT", b"over")
        fact("STOR over an existing file overwrites", _retr(f, f"{S}/A.TXT"), b"over")

        fact("LIST of a file path is that file's line", _names(f, f"{S}/A.TXT"), ["A.TXT"])
        fact("LIST with a '*' glob", _names(f, f"{S}/*.TXT"), ["A.TXT", "B.TXT"])
        fact("LIST with a '?' glob", _names(f, f"{S}/?.TXT"), ["A.TXT", "B.TXT"])
        fact("RETR missing", _perm(_retr, f, f"{S}/NOPE"), "550 Failed to open file.")
        fact("SIZE missing", _perm(f.size, f"{S}/NOPE"), "550 Could not get file size.")
        fact("DELE missing", _perm(f.delete, f"{S}/NOPE"), "550 Delete operation failed.")
        fact("RNFR missing", _perm(f.rename, f"{S}/NOPE", f"{S}/X"), "550 RNFR command failed.")
        fact("RNTO into a missing folder", _perm(f.rename, f"{S}/A.TXT", f"{S}/NOFOLDER/A.TXT"), "550 Rename failed.")
        fact("... leaves the source in place", _names(f, S), ["A.TXT", "B.TXT"])
        fact("STOR into a missing folder", _perm(_stor, f, f"{S}/NOFOLDER/x", b"x"), "553 Could not create file.")
        fact("RMD missing", _perm(f.rmd, f"{S}/NOFOLDER"), "550 Remove directory operation failed.")
        fact("MKD with a missing parent", _perm(f.mkd, f"{S}/NOFOLDER/DEEP"), "550 Create directory operation failed.")
        fact("MKD over a file name", _perm(f.mkd, f"{S}/A.TXT"), "550 Create directory operation failed.")
        fact("CWD missing", _perm(f.cwd, f"{S}/NOFOLDER"), "550 Failed to change directory.")

        f.mkd(f"{S}/SUB")
        _stor(f, f"{S}/SUB/in.txt", b"in")
        fact("RMD of a non-empty folder", _perm(f.rmd, f"{S}/SUB"), "550 Remove directory operation failed.")
        fact("DELE of a folder", _perm(f.delete, f"{S}/SUB"), "550 Delete operation failed.")
        fact("RMD of a file", _perm(f.rmd, f"{S}/A.TXT"), "550 Remove directory operation failed.")
        fact("RETR of a folder", _perm(_retr, f, f"{S}/SUB"), "550 Failed to open file.")
        fact("SIZE of a folder", _perm(f.size, f"{S}/SUB"), "550 Could not get file size.")
        fact("STOR onto a folder name", _perm(_stor, f, f"{S}/SUB", b"x"), "553 Could not create file.")
        fact("RNTO a file onto a folder", _perm(f.rename, f"{S}/A.TXT", f"{S}/SUB"), "550 Rename failed.")
        f.mkd(f"{S}/D")
        f.mkd(f"{S}/D/E")
        fact("RNTO a folder onto an existing folder", _perm(f.rename, f"{S}/D/E", f"{S}/SUB"), "550 Rename failed.")
        fact("RNTO a folder onto a file", _perm(f.rename, f"{S}/D", f"{S}/A.TXT"), "550 Rename failed.")
        fact("RNTO a folder into its own subfolder", _perm(f.rename, f"{S}/D", f"{S}/D/E/D"), "550 Rename failed.")
        fact("RNTO a folder moves its subtree", (f.rename(f"{S}/D", f"{S}/D2"), _names(f, f"{S}/D2")),
             ("250 Rename successful.", ["E"]))

        f.mkd(f"{S}/H")
        _stor(f, f"{S}/H/.dot", b"d")
        fact("LIST hides dotfiles", _ls(f, f"{S}/H"), [])
        fact("LIST -a shows them (with . and ..)", _names(f, f"-a {S}/H"), [".", "..", ".dot"])
        fact("RMD of a folder holding only a dotfile", _perm(f.rmd, f"{S}/H"), "550 Remove directory operation failed.")
        _stor(f, f"{S}/H/plain", b"p")
        fact("RNTO over a hidden dotfile replaces it", (f.rename(f"{S}/H/plain", f"{S}/H/.dot"), _retr(f, f"{S}/H/.dot")),
             ("250 Rename successful.", b"p"))

        f.mkd(f"{S}/sp dir")
        _stor(f, f"{S}/sp dir/x y.txt", b"xy")
        fact("names with spaces", _names(f, f"{S}/sp dir"), ["x y.txt"])
        f.mkd(f"{S}/br[1]")
        _stor(f, f"{S}/br[1]/in.txt", b"b")
        fact("'[' in a folder name is literal", _names(f, f"{S}/br[1]"), ["in.txt"])
        _stor(f, f"{S}/D.KSF.0123abcd.part", b"staged")
        fact("staged .part then RNTO", (f.rename(f"{S}/D.KSF.0123abcd.part", f"{S}/D.KSF"), _retr(f, f"{S}/D.KSF")),
             ("250 Rename successful.", b"staged"))
        fact("relative / '//' / '..' paths resolve from /", (_names(f, S.lstrip("/")) == _names(f, S),
                                                            _names(f, S.replace("/", "//", 1)) == _names(f, S),
                                                            _names(f, f"{S}/D2/..") == _names(f, S)), (True, True, True))
        fact("final listing (byte-order sort)", _names(f, S),
             ["A.TXT", "B.TXT", "D.KSF", "D2", "H", "SUB", "br[1]", "sp dir"])
    finally:
        _wipe(f, S)
        gone = S.rsplit("/", 1)[1] not in [ln.split(None, 8)[8] for ln in _ls(f, f"-a {base}")]
        fact("scratch folder removed", gone, True)
    return fails


def _wipe(f, p):
    try:
        lines = _ls(f, f"-a {p}")
    except Exception as ex:
        print("cleanup: cannot list", p, ex)
        return
    for ln in lines:
        name = ln.split(None, 8)[8]
        if name in (".", ".."):
            continue
        child = f"{p}/{name}"
        try:
            _wipe(f, child) if ln.startswith("d") else f.delete(child)
        except Exception as ex:
            print("cleanup:", child, ex)
    try:
        f.rmd(p)
    except Exception as ex:
        print("cleanup: rmd", p, ex)


def fake_connection(base="/SSD2/CLAUDETEST_K", **server_kw):
    from ftp_fake import FakeFTP, FtpFakeServer
    return FakeFTP(FtpFakeServer(dirs=[base], volumes=["SSD1", "SSD2"], **server_kw))


def main():
    spec = os.environ.get("KRONOS_FTP_CONTRACT")
    if spec:
        host, user, pw, base = spec.split(",", 3)
        f = ftplib.FTP()
        f.connect(host, 21, timeout=15)
        f.login(user, pw)
        f.set_pasv(True)
        print(f"live: {host} {f.getwelcome()}  base {base}")
    else:
        base = "/SSD2/CLAUDETEST_K"
        f = fake_connection(base)
        print("fake: tests/ftp_fake.py")
    try:
        fails = run_contract(f, base)
    finally:
        try:
            f.quit()
        except Exception:
            pass
    print(f"\n{len(fails)} failed fact(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
