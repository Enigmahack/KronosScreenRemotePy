"""Settings persistence routing: application code saves through Models.storage.save_settings_async.

1. Architecture guard: no non-test module outside Models/storage.py references the blocking
   `save_settings` (a call OR a passed-in reference) beyond an explicit, counted allowlist.
2. The Sample Editor's production wiring uses save_settings_async, and a recent-files change is written
   off the calling thread from a copy.
3. A burst from two threads: the newest snapshot wins, on disk too.
4. flush_settings waits for a write in progress (and times out honestly).
5. Import/Reset/Export fences: storage flushes queued writes before replacing or copying settings.json.
"""
import os, re, sys, tempfile, threading, time
os.environ["KRONOS_DATA_DIR"] = tempfile.mkdtemp(prefix="kr_settings_routing_")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from unittest.mock import patch

fails = []
def check(name, cond):
    if not cond: fails.append(name)
    print(("ok   " if cond else "FAIL ") + name)


# ── 1. Architecture guard ─────────────────────────────────────────────────────────────────────────────
# `\bsave_settings\b` matches calls, bare references (`cb = storage.save_settings`) and imports, but not
# `self._save_settings(` (preceded by `_`, a word char) or `save_settings_async` (followed by `_`).
BLOCKING = re.compile(r"\bsave_settings\b")

# Relative path -> the most blocking references allowed there, each with its reason.
ALLOWLIST = {
    "Views/main_window.py": 1,            # exit path: flush_settings, then one blocking save
}
EXEMPT = {"Models/storage.py"}            # defines the primitive
SKIP_DIRS = {".venv", "venv", "tests", ".git", "__pycache__", "pipeline", "build", "dist", "node_modules"}


def count_blocking(text):
    """Line numbers referencing the blocking save, from NAME tokens only (comments and strings,
    e.g. a docstring that mentions save_settings, are not code)."""
    import io, tokenize
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError):
        return [n for n, l in enumerate(text.splitlines(), 1) if BLOCKING.search(l.split("#", 1)[0])]
    # Only `<module>.save_settings` and `import ... save_settings` reach storage's primitive; a bare
    # `save_settings` is a local name (e.g. the Sample Editor model's injected-callback parameter).
    hits, prev, stmt_is_import = [], None, False
    for t in toks:
        if t.type in (tokenize.NEWLINE, tokenize.NL) and not (prev and prev.string == "\\"):
            if t.type == tokenize.NEWLINE:
                stmt_is_import = False
        elif t.type == tokenize.NAME and t.string in ("import", "from") and (
                prev is None or prev.type in (tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.NL)
                or prev.string == ";"):
            stmt_is_import = True
        if t.type == tokenize.NAME and t.string == "save_settings" and (
                stmt_is_import or (prev is not None and prev.string == ".")):
            hits.append(t.start[0])
        if t.type not in (tokenize.COMMENT,):
            prev = t
    return hits


# Negative control: the matcher must see what it is meant to see.
check("matcher: call", len(count_blocking("storage.save_settings(x)\n")) == 1)
check("matcher: passed reference", len(count_blocking("cb = Models.storage.save_settings\n")) == 1)
check("matcher: import", len(count_blocking("from Models.storage import save_settings\n")) == 1)
check("matcher: ignores self._save_settings", not count_blocking("self._save_settings(x)\n"))
check("matcher: ignores save_settings_async", not count_blocking("storage.save_settings_async(x)\n"))
check("matcher: ignores comments/strings", not count_blocking('# save_settings(x)\ny = "storage.save_settings(x)"\n'))
check("matcher: multi-line import", len(count_blocking("from Models.storage import (\n    load_settings, save_settings)\n")) == 1)
check("matcher: ignores a parameter named save_settings",
      not count_blocking("def f(self, save_settings=None):\n    self._cb = save_settings\n"))

offenders = []
for dirpath, dirnames, filenames in os.walk(ROOT):
    dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
    for fn in filenames:
        if not fn.endswith(".py"):
            continue
        full = os.path.join(dirpath, fn)
        rel = os.path.relpath(full, ROOT).replace(os.sep, "/")
        if rel in EXEMPT:
            continue
        with open(full, encoding="utf-8", errors="replace") as f:
            hits = count_blocking(f.read())
        if len(hits) > ALLOWLIST.get(rel, 0):
            offenders.append(f"{rel}:{hits}")
check("no blocking save_settings outside the allowlist" + (f" ({offenders})" if offenders else ""),
      not offenders)


# ── 2. Sample Editor wiring + off-thread write ──────────────────────────────────────────────────────
import Models.storage as storage
from Models.app_settings import AppSettings
from Core.sample_editor_model import SampleEditorModel

saved = []          # (thread, object, payload)
def recorder(delay=0.0, field="sample_recent_files"):
    def fake(s):
        if delay: time.sleep(delay)
        saved.append((threading.current_thread(), s, getattr(s, field)))
    return fake

with patch.object(storage, "save_settings", recorder()):
    s = AppSettings()
    s.sample_recent_files = ["a.KSC"]
    m = SampleEditorModel(s, storage.save_settings_async)
    t0 = time.monotonic(); m.clear_recent_files(); dt = time.monotonic() - t0
    check(f"recent-files change returns at once ({dt*1000:.0f} ms)", dt < 0.05)
    check("flush returns True", storage.flush_settings(5))
    check("written off the calling thread", saved and saved[-1][0] is not threading.current_thread())
    check("written from a copy", saved and saved[-1][1] is not s and saved[-1][2] == [])

from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication([])
from Views.sample_editor_window import SampleEditorWindow
w = SampleEditorWindow("", 21, "", "", None)
check("window wires the model to save_settings_async",
      w._model._save_settings is storage.save_settings_async)
w.close(); w.deleteLater(); app.processEvents()


# ── 3. Two-thread burst: newest snapshot wins ───────────────────────────────────────────────────────
saved.clear()
shared = AppSettings()
counter = [0]
order_lock = threading.Lock()
def burst():
    for _ in range(200):
        with order_lock:           # the test's own ordering: the counter tells which change is newest
            counter[0] += 1
            shared.max_fps = counter[0]
            storage.save_settings_async(shared)

real_save = storage.save_settings
with patch.object(storage, "save_settings", lambda s: (saved.append((None, s, s.max_fps)), real_save(s))):
    ts = [threading.Thread(target=burst) for _ in range(2)]
    for t in ts: t.start()
    for t in ts: t.join()
    check("burst flush", storage.flush_settings(10))
values = [v for _, _, v in saved]
check("written values never go backwards", values == sorted(values))
check(f"last write is the newest change ({values[-1] if values else None} == {counter[0]})",
      values and values[-1] == counter[0])
check("disk holds the newest change", storage.load_settings().max_fps == counter[0])


# ── 4. flush_settings waits ──────────────────────────────────────────────────────────────────────────
saved.clear()
with patch.object(storage, "save_settings", recorder(delay=0.3, field="max_fps")):
    shared.max_fps = 42
    t0 = time.monotonic()
    storage.save_settings_async(shared)
    check("short flush times out mid-write", storage.flush_settings(0.01) is False)
    ok = storage.flush_settings(5)
    dt = time.monotonic() - t0
    check(f"flush waits for the write ({dt:.2f} s)", ok and dt >= 0.25 and saved and saved[-1][2] == 42)


# ── 5. Import / Reset / Export fence queued writes ──────────────────────────────────────────────────
events = []
def slow_save(s):
    time.sleep(0.2); events.append(("write", s.max_fps))

tmp = tempfile.mkdtemp(prefix="kr_routing_io_")
src = os.path.join(tmp, "in.json")
real_save(AppSettings(max_fps=77))
import shutil
shutil.copy2(str(storage._path("settings.json")), src)

with patch.object(storage, "save_settings", slow_save):
    shared.max_fps = 1; storage.save_settings_async(shared)
    with patch.object(storage.shutil, "copy2", lambda a, b: events.append(("import",))):
        storage.import_settings(src)
    check("import waits for the queued write", events == [("write", 1), ("import",)])

    events.clear()
    shared.max_fps = 2; storage.save_settings_async(shared)
    storage.reset_all()
    events.append(("reset",))
    storage.flush_settings(5)
    check("reset waits for the queued write", events == [("write", 2), ("reset",)])
    check("nothing recreates settings.json after reset", not storage._path("settings.json").exists())

    events.clear()
    shared.max_fps = 3; storage.save_settings_async(shared)
    with patch.object(storage.shutil, "copy2", lambda a, b: events.append(("copy",))):
        storage.export_settings(AppSettings(max_fps=4), os.path.join(tmp, "out.json"))
    storage.flush_settings(5)
    check("export: queued write, then its own write, then the copy",
          events == [("write", 3), ("write", 4), ("copy",)])

# Settings dialog Import/Reset fence through storage (no reach into the main window).
from Views.settings_window import SettingsWindow
dlg = SettingsWindow(AppSettings())
with patch.object(storage, "flush_settings", wraps=storage.flush_settings) as fl:
    dlg._flush_pending_writes()
    check("settings dialog fences with storage.flush_settings", fl.called)
dlg.deleteLater(); app.processEvents()

print("ALL PASS" if not fails else "FAILED: " + ", ".join(fails))
sys.exit(1 if fails else 0)
