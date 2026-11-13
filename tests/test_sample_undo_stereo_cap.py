"""Sample Editor undo history under the byte cap, on a stereo pair: mirrored (both-channel) steps and one-channel steps
interleaved, with eviction. Every Undo/Redo press must change something and must land on a state that really existed
(never one half of a mirrored step reverted without the other), the marker lists must stay bounded by the stacks, and
a zone/sample navigation must not leave Undo enabled with nothing behind it. Real files, copied fixture."""
import os, shutil, sys, tempfile, wave
from collections import deque
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("KRONOS_DATA_DIR", tempfile.mkdtemp(prefix="kr_data_"))
import numpy as np

from Core.sample_edit_undo import SampleZoneUndo, ZoneListSnapshot
from Core.sample_editor_model.model import SampleEditorModel
from Data.kmp_multisample import KmpZone

from fixture_paths import SAMPLE_FIXTURES as FIX
fails = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


tmp = tempfile.mkdtemp(prefix="kr_undocap_")
shutil.copytree(os.path.join(FIX, "SMPTEST"), os.path.join(tmp, "S"))
m = SampleEditorModel()
m.open_collection(os.path.join(tmp, "S", "LOOP.KSC"))

N = 4000
t = np.arange(N)
wav = os.path.join(tmp, "pair.wav")
with wave.open(wav, "wb") as w:
    w.setnchannels(2); w.setsampwidth(2); w.setframerate(44100)
    w.writeframes(np.stack([(np.sin(t * 0.05) * 20000).astype(np.int16),
                            (np.sin(t * 0.11) * 5000).astype(np.int16)], 1).tobytes())
node = m.new_stereo_multisample_pair_in_collection("PAIR", m.next_free_mno1(2))
m.select_node(node)
m.import_stereo_audio_as_new_zone_pair(wav, 60, 90)


def select_left():
    n = m._node_for_path(node.multisample_ref[1])
    m.select_node(n.children[1])


select_left()
check("pair in Combine mode", m.has_stereo_pair and m.should_mirror_to_partner and m.is_primary_left_channel)
L, R = m.selected_sample, m.partner_sample


def state():
    return (bytes(L.pcm), bytes(R.pcm))


def set_cap(nbytes):
    for name in ("_sample_undo", "_partner_undo"):         # _partner_undo: the pre-fix model, for the negative control
        st = getattr(m, name, None)
        if st is not None:
            st.byte_cap = nbytes


# Room for only a few steps: mirrored steps and partner-only steps interleaved, so eviction happens mid-history.
set_cap(6 * (len(L.pcm) + len(R.pcm)))
history = {state()}
m.selection_start_frame = m.selection_end_frame = 0
for i in range(12):
    m.apply_gain_adjust(-0.5)                       # mirrored onto R (Combine)
    history.add(state())
    m._cursor_frame = 10
    m.apply_insert_silence(8, False, True)          # R only (partner-only step)
    history.add(state())
m._cursor_frame = -1

check("undo markers bounded by the stacks (one marker per surviving entry)",
      len(m._undo_domains) <= m._sample_undo.undo_count + len(m._zone_undo._undo))
presses, noop, foreign = 0, 0, 0
while m.can_undo and presses < 100:
    before = state()
    m.undo()
    presses += 1
    after = state()
    noop += after == before
    foreign += after not in history
check("undo: some history survived the cap", presses > 0)
check("undo: no press was a no-op (Undo never enabled while doing nothing)", noop == 0)
check("undo: every state existed (no half-reverted stereo step)", foreign == 0)
check("undo: marker lists empty once drained", not m._undo_domains)

presses, noop, foreign = 0, 0, 0
while m.can_redo and presses < 100:
    before = state()
    m.redo()
    presses += 1
    after = state()
    noop += after == before
    foreign += after not in history
check("redo: no press was a no-op", noop == 0)
check("redo: every state existed", foreign == 0)
check("redo: drained", not m.can_redo)

# A partner-only step, then navigate away and back: the sample stacks reset, so Undo must not stay enabled.
m.undo()
m.redo()
m._cursor_frame = 10
m.apply_insert_silence(8, False, True)
m._cursor_frame = -1
check("partner-only step is undoable", m.can_undo)
m.select_node(None)
select_left()
check("navigation: no dead Undo left behind", m.can_undo == bool(m._undo_domains) and not m.can_undo)

# Step ceiling: field edits cost ~nothing against the byte cap (a header-only sample costs 0), so without a step
# ceiling the stack and the marker list would grow without bound.
m.select_node(None)
select_left()
m._sample_undo.max_steps = 5
for i in range(12):
    m.apply_sample_edits(int(L.sample_rate), True, 0, 10 + i, 2000)
check("step ceiling bounds the sample stack", m._sample_undo.undo_count <= 5)
check("markers == stack entries under the ceiling", m._undo_domains.count("Sample") == m._sample_undo.undo_count)

# Zone stack: redo may never push the undo side past the step cap (pinned on the deque state directly - through
# the public API undo+redo is already <= cap, because recording clears redo).
zu = SampleZoneUndo(step_cap=2)
zl = [KmpZone(60, 60, "a")]
zu._undo = deque([ZoneListSnapshot.of(zl), ZoneListSnapshot.of(zl)])
zu._redo = deque([ZoneListSnapshot.of(zl), ZoneListSnapshot.of(zl)])
zu.redo(ZoneListSnapshot.of(zl))
check("zone redo respects the step cap", len(zu._undo) <= 2)
zu = SampleZoneUndo(step_cap=2)
zu._undo = deque([ZoneListSnapshot.of(zl), ZoneListSnapshot.of(zl)])
zu._redo = deque([ZoneListSnapshot.of(zl), ZoneListSnapshot.of(zl)])
zu.undo(ZoneListSnapshot.of(zl))
check("zone undo respects the step cap on the redo side", len(zu._redo) <= 2)

# Guard path: a recorded step whose sample is no longer the selected one must be DROPPED, not parked in the
# opposite stack (a marker-less redo entry would otherwise block the real step beneath it).
m.select_node(None)
select_left()
m.selection_start_frame = m.selection_end_frame = 0
m.apply_gain_adjust(-0.5)
real_depth = m._sample_undo.undo_count
top = m._sample_undo.peek_undo()
check("guard setup: a real step is on the stack", top is not None and real_depth >= 1)
stale = type(top)(top.primary, top.partner, object(), object())        # targets that are no longer selected
m._sample_undo._undo.append(stale)
m._undo_domains.append("Sample")                                       # its marker, as record_before_edit would add
m._refresh_undo_redo_state()
redo_before = m._sample_undo.redo_count
m.undo()
check("guard: stale step dropped, not moved to redo", m._sample_undo.redo_count == redo_before
      and m._sample_undo.undo_count == real_depth)
check("guard: no redo marker pushed for it", len(m._redo_domains) == m._sample_undo.redo_count + len(m._zone_undo._redo))
m.undo()
check("guard: the real step beneath is still reachable", m.can_redo)

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
