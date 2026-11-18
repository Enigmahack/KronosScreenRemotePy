"""File Manager drive selector on every platform: Linux mount parsing (pure), volume labels, which drive contains a
folder, and that list_local_drives() answers on the host OS. Platform-specific branches that cannot run here are
covered through their pure helpers."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import Tools.file_manager as fm

fails = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


MOUNTS = r"""sysfs /sys sysfs rw 0 0
/dev/nvme0n1p2 / ext4 rw 0 0
/dev/nvme0n1p1 /boot/efi vfat rw 0 0
tmpfs /run tmpfs rw 0 0
/dev/sdb1 /media/dave/My\040Backup exfat rw 0 0
/dev/sdc1 /mnt/data ext4 rw 0 0
nas:/share /mnt/nas nfs4 rw 0 0
/dev/loop3 /snap/core/1 squashfs ro 0 0
/dev/sdb1 /media/dave/My\040Backup exfat rw 0 0
"""
got = fm.parse_linux_mounts(MOUNTS, {"/dev/sdb1": "Backup Drive"})
check("root + user disks kept", [m for m, _ in got] == ["/", "/media/dave/My Backup", "/mnt/data", "/mnt/nas"])
check("system / pseudo mounts dropped", not any(m in ("/sys", "/run", "/boot/efi", "/snap/core/1") for m, _ in got))
check("label preferred, else mount name", dict(got)["/media/dave/My Backup"] == "Backup Drive" and dict(got)["/mnt/data"] == "data")
check("escaped space decoded, duplicates collapsed", len(got) == 4)
check("udev label escape", fm._unescape_label(r"My\x20Disk") == "My Disk")
check("garbage lines ignored", fm.parse_linux_mounts("junk\n\n", {}) == [])

if os.name == "nt":
    check("windows: path under its drive", fm._is_under("C:\\Users\\x", "C:\\") and not fm._is_under("Z:\\a", "C:\\"))
    check("windows: case-insensitive", fm._is_under("c:\\users", "C:\\"))
else:
    check("posix: nested mount wins by prefix", fm._is_under("/media/x/y", "/media/x") and not fm._is_under("/media/xy", "/media/x"))
    check("posix: everything is under /", fm._is_under("/a/b", "/"))

drives = fm.list_local_drives()
check("host lists at least one drive", len(drives) >= 1 and all(r and t for r, t in drives))
if os.name != "nt":
    check("posix: root and home offered", drives[0][0] == "/" and drives[-1][1].startswith("~"))

print("FAILED: %s" % fails if fails else "ALL PASS")
sys.exit(1 if fails else 0)
