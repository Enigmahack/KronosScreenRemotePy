"""Writes the Nautilus blank Combi / Drum Kit / Wave Sequence into Resources/Conversion from a real Nautilus .PCG.
Each is the instrument's own initialised object: the most common body that reads as Init across the whole file
(1,408 identical Combis, 160 Drum Kits, 222 Wave Sequences in the file this was taken from). Run:
python research/extract_factory_blanks.py <nautilus.pcg>"""
import collections
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import Objects.object_body as ob
from Data.librarian_sysex import OBJ_COMBI, OBJ_DRUM_KIT, OBJ_WAVE_SEQ
from Data.pcg_file import MODEL_NAUTILUS, open_pcg

OUT = {OBJ_COMBI: "NautilusInitCombination.bin", OBJ_DRUM_KIT: "NautilusInitDrumKit.bin",
       OBJ_WAVE_SEQ: "NautilusInitWaveSequence.bin"}

data = pathlib.Path(sys.argv[1]).read_bytes()
assert data[4] == MODEL_NAUTILUS, "not a Nautilus PCG"
pcg = open_pcg(data)
for obj_type, name in OUT.items():
    c = collections.Counter(o.body for o in pcg.objects if o.obj_type == obj_type and ob.is_init(obj_type, o.body))
    body, n = c.most_common(1)[0]
    (ROOT / "Resources" / "Conversion" / name).write_bytes(body)
    print(f"{name}: {len(body)} bytes, {n} identical copies")
