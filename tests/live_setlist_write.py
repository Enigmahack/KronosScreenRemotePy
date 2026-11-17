"""Live Set List write round trip against a real unit. WRITES to the instrument, so run it only on a scratch Set List number.
  export : live_setlist_write.py <src-host> [--nautilus] --number N --export OUT.bin   (read-only: dump + convert + rename)
  write  : live_setlist_write.py <dst-host> [--nautilus] --number M --write IN.bin     (backup, verify backup, write, Store,
                                                                                      re-read and compare)
  restore: live_setlist_write.py <dst-host> [--nautilus] --restore BACKUP.syx          (replay the backup, Store, re-read)
`--nautilus` names the unit that host IS. Store commits the whole Set List bank (all 128), unsaved front-panel edits included."""
import os
import sys
import tempfile
import time
from types import SimpleNamespace

os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_livesl_w_")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtCore import QCoreApplication

app = QCoreApplication([])
from Conversion.push import _verify_backup, restore_from_backup
from Conversion.setlist import convert_setlist
from Core.sysex_service import SysExService
import Core.kronos_sysex as ksx
from Data.librarian_sysex import OBJ_SET_LIST

args = sys.argv[1:]
host = args[0]
nautilus = "--nautilus" in args


def opt(name):
    return args[args.index(name) + 1] if name in args else None


svc = SysExService()
svc.start(host)
t = time.time()
while time.time() - t < 12 and not svc.can_dump:
    app.processEvents(); time.sleep(0.05)
if not svc.can_dump:
    print("MIDI bridge not reachable on", host)
    sys.exit(2)
if nautilus:
    svc.set_device_family(True)
number = int(opt("--number") or 0)


def live(n):
    return svc.dump_object_parsed(OBJ_SET_LIST, 0, n, no_response_ms=15000)


code = 0
if opt("--export"):
    d = live(number)
    if d is None:
        print("no reply"); code = 2
    else:
        res = convert_setlist(d.body, not nautilus)
        body = bytearray(res.body)
        body[:24] = b"CONVTEST " + (b"N>K" if nautilus else b"K>N") + b" " * 12
        open(opt("--export"), "wb").write(bytes(body))
        print(f"exported Set List {number} '{ksx._ascii_trim(d.body, 0, 24)}' ({len(d.body)} B) -> {len(body)} B; "
              f"{len(res.warnings)} warning(s)")
elif opt("--write"):
    body = open(opt("--write"), "rb").read()
    pre = live(number)
    if pre is None:
        print("cannot read the target; nothing written"); sys.exit(2)
    print(f"target Set List {number}: '{ksx._ascii_trim(pre.body, 0, 24)}' version={pre.version} len={len(pre.body)}")
    if len(pre.body) != len(body):
        print(f"REFUSED: body is {len(body)} B, the unit's Set List is {len(pre.body)} B"); sys.exit(2)
    from Data.librarian_sysex import object_version
    print(f"library fallback version {object_version(OBJ_SET_LIST, body)} vs live {pre.version}")
    backup = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "librarian_backups",
                          time.strftime("%Y%m%d_%H%M%S") + f"_setlist{number:03d}.syx")
    os.makedirs(os.path.dirname(backup), exist_ok=True)
    bk = [SimpleNamespace(obj=OBJ_SET_LIST, bank=0, index=number, version=pre.version, body=pre.body)]
    svc.backup_objects(bk, backup)
    problem = _verify_backup(backup, bk, codec=svc._codec)
    if problem:
        print("backup not usable:", problem); sys.exit(2)
    print("backup verified:", backup)
    op = SimpleNamespace(obj=OBJ_SET_LIST, bank=0, index=number, version=pre.version, body=body)
    r = svc.write_object(op)
    print("write reply:", r)
    if r == 0:
        s = svc.store_bank(OBJ_SET_LIST, 0)
        print("store reply:", s)
        back = live(number)
        print("re-read identical:", back is not None and back.body == body)
        code = 0 if back is not None and back.body == body else 1
    else:
        code = 1
elif opt("--restore"):
    res = restore_from_backup(svc, opt("--restore"), progress=print)
    print("restore ok:", res.ok, res.messages)
    code = 0 if res.ok else 1
svc.stop()
sys.exit(code)
