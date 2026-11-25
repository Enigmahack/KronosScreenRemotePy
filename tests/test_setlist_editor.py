"""Set List editor: dialog edits on a real set list body land in the bytes; order moves whole slots; pure helpers."""
import glob, os, subprocess, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from Data.pcg_file import extract_objects
from Objects import object_body as ob, setlist_edit as se
from Views.setlist_editor_dialog import SetListEditorDialog

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
    print("no real set list found; skipping dialog checks")
else:
    d = SetListEditorDialog("t", body, lambda b, i: "x")
    d._list.setCurrentRow(1)
    s1 = ob.parse_setlist_slot(body, 1)
    d._name.setText("Edited"); d._name.textEdited.emit("Edited")
    d._color.setCurrentIndex(5)
    d._transpose.setValue(-7)
    d._comments.setPlainText("hello")
    d._eq_bypass.setChecked(True); d._eq_trim.setValue(12); d._bands[3].setValue(4.5)
    d._shift(1)                       # slot 1 -> position 2
    d._accept()
    nb = d.new_body
    n2 = ob.parse_setlist_slot(nb, 2)
    check("moved-with-its-fields", n2.name.strip() == "Edited" and n2.color == 5 and n2.comments.rstrip() == "hello"
          and se.slot_transpose(nb, 2) == -7 and n2.type == s1.type and n2.index == s1.index and n2.bank == s1.bank)
    check("neighbour-shifted-up", ob.parse_setlist_slot(nb, 1).name == ob.parse_setlist_slot(body, 2).name)
    e = se.read_eq(nb)
    check("eq-written", e.bypass and e.input_trim == 12 and e.bands[3] == 4.5 and len(nb) == len(body))
    check("untouched-tail", nb[24 + 3 * 542:24 + 128 * 542] == body[24 + 3 * 542:24 + 128 * 542])
print("FAILED:" if fails else "ALL PASS", fails or "")
sys.exit(1 if fails else 0)
