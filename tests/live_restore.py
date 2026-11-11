"""Live, WRITES: Conversion.push.restore_from_backup of the given .syx backups onto a unit.
usage: live_restore.py <host> [--nautilus] <backup.syx> [...]"""
import os, sys, tempfile, time
os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_liverest_")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtCore import QCoreApplication
app = QCoreApplication([])
from Core.sysex_service import SysExService
from Conversion.push import restore_from_backup
args = [a for a in sys.argv[1:] if not a.startswith("--")]
svc = SysExService(); svc.start(args[0])
t = time.time()
while time.time() - t < 12 and not svc.can_dump:
    app.processEvents(); time.sleep(0.05)
assert svc.can_dump; svc.set_device_family("--nautilus" in sys.argv)
for f in args[1:]:
    r = restore_from_backup(svc, f, progress=lambda m: None)
    print(f, r.ok, r.messages)
svc.stop()
