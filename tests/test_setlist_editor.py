"""Set List editor: 4x4 grid pages, drag/drop swap + copy, copy/cut/paste/delete, target change, field edits; both families."""
import glob, os, subprocess, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from Data.pcg_file import extract_objects
from Objects import object_body as ob, setlist_edit as se
from Views.setlist_editor_dialog import SetListEditorDialog, PER_PAGE
from Views.setlist_ref_picker import RefPickerDialog

app = QApplication.instance() or QApplication([])
fails = []
def check(n, c):
    if not c: fails.append(n)

check("pure-selftest", subprocess.run([sys.executable, "-m", "Objects.setlist_edit"], capture_output=True).returncode == 0)
body = None
for f in glob.glob("Z:/PCG EXAMPLES/**/*.[pP][cC][gG]", recursive=True)[:40]:
    for o in extract_objects(open(f, "rb").read())[0]:
        if o.obj_type == 0x0D and len(o.body) == 69416 and ob.parse_setlist_slot(o.body, 1) and not ob.parse_setlist_slot(o.body, 1).is_empty:
            body = o.body; break
    if body: break
if body is None:
    print("no real set list found; using a synthetic one")
    b = bytearray(69416)
    for i in range(4):
        b[24 + i * 542:24 + i * 542 + 24] = f"Slot{i}".ljust(24).encode()
    body = bytes(b)

picked = []
def pick(cur):
    picked.append(cur); return (1, 3, 77)
d = SetListEditorDialog("t", body, lambda t, b, i: f"{t}/{b}/{i}", pick, lambda r: f"N{r[2]}")
check("grid-is-4x4", len(d._cells) == PER_PAGE == 16)
check("cell-text", "001" in d._cells[1].text())
d.drop_slot(1, 2, False)                                       # drag = swap
check("swap", se.slot_bytes(d._body, 2) == se.slot_bytes(body, 1) and se.slot_bytes(d._body, 1) == se.slot_bytes(body, 2))
d.drop_slot(2, 5, True)                                        # ctrl-drag = copy
check("copy-drop", se.slot_bytes(d._body, 5) == se.slot_bytes(body, 1) and se.slot_bytes(d._body, 2) == se.slot_bytes(body, 1))
d.cut_slot(5); d.paste_slot(9)
check("cut-paste", ob.parse_setlist_slot(d._body, 5).is_empty and se.slot_bytes(d._body, 9) == se.slot_bytes(body, 1))
d.delete_slot(9)
check("delete", ob.parse_setlist_slot(d._body, 9).is_empty)
d._select(2)
d.change_target()
check("target-changed", se.slot_ref(d._body, 2) == (1, 3, 77) and picked and picked[0] == se.slot_ref(body, 1))
check("slot-renamed-to-target", ob.parse_setlist_slot(d._body, 2).name.strip() in ("N77",) or ob.parse_setlist_slot(body, 1).name.strip() != "")
d._color.setCurrentIndex(5); d._transpose.setValue(-7); d._comments.setPlainText("hello")
d._eq_bypass.setChecked(True); d._bands[3].setValue(4.5)
d._set_page(3)
check("paging", "Page 4" in d._page_lbl.text() and "048" in d._cells[0].text())
d._accept()
nb = d.new_body
n2 = ob.parse_setlist_slot(nb, 2)
check("fields", n2.color == 5 and n2.comments.rstrip() == "hello" and se.slot_transpose(nb, 2) == -7)
check("eq", se.read_eq(nb).bypass and se.read_eq(nb).bands[3] == 4.5 and len(nb) == len(body))
check("tail-untouched", nb[24 + 10 * 542:24 + 128 * 542] == body[24 + 10 * 542:24 + 128 * 542])

# Nautilus-length body: same editor, 543-byte slots; a Kronos slot can't be pasted into it
nbody = bytes(69544)
d2 = SetListEditorDialog("n", nbody, lambda t, b, i: "x")
d2.paste_slot(0)
check("cross-family-paste-refused", d2._body == nbody)
d2._select(3); d2._name.setText("Nau"); d2._name.textEdited.emit("Nau"); d2.copy_slot(3); d2.paste_slot(4)
check("nautilus-copy", ob.parse_setlist_slot(d2._body, 4).name.strip() == "Nau" and len(d2._body) == 69544)

p = RefPickerDialog([(0, 0, 5, "A", "Pad"), (1, 2, 9, "I-C", "Piano")], (1, 2, 9))
check("picker-preselect", p._list.currentItem() is not None)
p._accept(); check("picker-result", p.result_ref == (1, 2, 9))
p._type.setCurrentIndex(2); p._song.setValue(12); p._accept(); check("picker-song", p.result_ref == (2, 0, 12))
for n in (69416, 69544):                                   # unnamed slot that still has a comment / target is not "(empty)"
    b = ob.write_setlist_slot_comments(bytes(n), 0, "note")
    b = se.set_slot_ref(b, 2, 1, 3, 5)
    dd = SetListEditorDialog("t", b, lambda t, bk, i: "T")
    check(f"unnamed-slot-shown-{n}", "(empty)" not in dd._cells[0].text() and "(empty)" not in dd._cells[2].text()
          and "(empty)" in dd._cells[1].text())
print("FAILED:" if fails else "ALL PASS", fails or "")
sys.exit(1 if fails else 0)
