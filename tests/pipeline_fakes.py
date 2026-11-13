"""Scenario-test FTP fakes. Thin re-exports of tests/ftp_fake.py, the one fake that behaves like the instruments'
real FTP server (measured live; see that module's docstring and tests/ftp_contract.py).

`FaultyFtp(files, fail_at=N, dead=True, partial=True, overwrite=True)` is the REAL _FtpWorker over the fake server:
raw command N (counting connect, NOOP, LIST, STOR, RNFR, RNTO, ...) raises ConnectionError; `dead` makes every later
command and reconnect fail too, otherwise the dropped connection is reconnected by the worker like a real blip;
`partial` leaves half the bytes from a dropped STOR/RETR. `overwrite=False` is a hypothetical server whose RNTO refuses
an existing file (the real instruments replace it), kept so the swap-aside fallback stays covered.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ftp_fake import FakeFtpWorker, FtpFakeServer  # noqa: E402,F401

FaultyFtp = FakeFtpWorker
KronosLikeFtp = FakeFtpWorker
SERVERS = {"instrument": FakeFtpWorker}
