"""The FTP contract (tests/ftp_contract.py: the instruments' measured FTP behaviour) against the shared fake
(tests/ftp_fake.py), plus the fake's own fault model through the real _FtpWorker. Fails when someone makes the fake
more forgiving than the real server. For a live unit: KRONOS_FTP_CONTRACT=host,user,pass,base python tests/ftp_contract.py"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("KRONOS_DATA_DIR", tempfile.mkdtemp(prefix="kr_data_"))
from pathlib import Path

from ftp_contract import fake_connection, run_contract
from ftp_fake import FakeFtpWorker, FtpFakeServer

fails = run_contract(fake_connection(), "/SSD2/CLAUDETEST_K", verbose=False)


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


with tempfile.TemporaryDirectory(prefix="kr_ftpfake_") as tmp:
    src = Path(tmp) / "x.bin"
    src.write_bytes(b"NEW")
    w = FakeFtpWorker({"/SSD2/a/x.bin": b"OLD"})
    w.upload(str(src), "/SSD2/a/x.bin")
    check("worker upload stages then replaces through RNTO", w.files == {"/SSD2/a/x.bin": b"NEW"})
    check("worker list_dir falls back from MLSD to LIST", [e.name for e in w.list_dir("/SSD2/a")] == ["x.bin"]
          and w.server.calls("MLSD") and w.server.calls("LIST"))
    check("worker list_dir of a missing folder is empty", w.list_dir("/SSD2/nope") == [])

    # a one-off drop: the worker's NOOP probe fails on the dead control connection and it reconnects
    w = FakeFtpWorker({"/SSD2/a/x.bin": b"OLD"})
    w.list_dir("/SSD2/a")
    w.server.fail_at, w.server.dead = w.ops, False     # the NOOP probe
    check("blip on the NOOP probe is absorbed by a reconnect", [e.name for e in w.list_dir("/SSD2/a")] == ["x.bin"]
          and w.connections == 2)
    w.server.fail_at = w.ops + 3                        # NOOP, MLSD (500), NOOP, LIST
    try:
        w.list_dir("/SSD2/a")
        check("blip on LIST surfaces", False)
    except ConnectionError:
        check("blip on LIST surfaces", True)
    check("after a blip the next call reconnects", [e.name for e in w.list_dir("/SSD2/a")] == ["x.bin"]
          and w.connections == 3)
    w = FakeFtpWorker({"/SSD2/a/x.bin": b"OLD"}, fail_at=0)
    try:
        w.list_dir("/SSD2/a")
        check("dead unit: connect fails", False)
    except ConnectionError:
        check("dead unit: connect fails", True)

    # partial STOR leaves half the bytes at the staged name
    w = FakeFtpWorker({"/SSD2/a/x.bin": b"OLD"})
    w.list_dir("/SSD2/a")
    w.server.fail_at = w.ops + 1          # NOOP, then STOR
    big = Path(tmp) / "big.bin"
    big.write_bytes(b"z" * 100)
    try:
        w.upload(str(big), "/SSD2/a/y.bin.0123abcd.part")
    except ConnectionError:
        pass
    check("dropped STOR leaves half a .part", w.files.get("/SSD2/a/y.bin.0123abcd.part") == b"z" * 50)

    # the configurable non-default modes
    s = FtpFakeServer({"/SSD2/a/x.bin": b"1"}, case_sensitive=False)
    check("case-insensitive mode resolves case variants", s.is_file("/ssd2/A/X.BIN"))
    w = FakeFtpWorker({"/SSD2/a/x.bin": b"1", "/SSD2/a/y.bin": b"2"}, overwrite=False)
    try:
        w.rename("/SSD2/a/y.bin", "/SSD2/a/x.bin")
        check("overwrite=False refuses RNTO over a file", False)
    except Exception as ex:
        check("overwrite=False refuses RNTO over a file", str(ex) == "550 Rename failed.")

print(f"{len(fails)} failure(s)" + ("; " + "; ".join(fails[:5]) if fails else ""))
sys.exit(1 if fails else 0)
