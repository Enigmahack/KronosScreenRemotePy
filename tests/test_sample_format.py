"""Port of C# SampleSelfTests: KSF/KMP/KSC round trips, byte-exact preservation quirks (loop-dup slot, RLP3
offset 5), rejection of garbage, _UserBank refusal and the reference-export writer."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np

from Data import korg_riff_chunk as riff
from Data.ksc_collection import KscCollection
from Data.kmp_multisample import KmpMultisample, KmpZone
from Data.ksf_sample import KsfSample

fails = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def pcm(v):
    return np.array(v, dtype=np.int16)


# ---- KSF round trip
s = KsfSample(name="Test Piano", suffix="-L", sno1=42, sample_rate=48000, channels=1, bits=16,
              sample_start=500, loop_start=1000, loop_end=5000, flags=0x01)
vals = [100, -200, 300, -400, 32767, -32768]
s.set_samples(pcm(vals))
b = s.to_bytes()
r = KsfSample.open(b)
check("ksf-reopen-not-null", r is not None)
if r is not None:
    check("ksf-round-trip-bytes-identical", b == r.to_bytes())
    check("ksf-name", r.name == "Test Piano")
    check("ksf-suffix", r.suffix == "-L")
    check("ksf-sno1", r.sno1 == 42)
    check("ksf-sample-rate", r.sample_rate == 48000)
    check("ksf-frame-count", r.frame_count == 6)
    check("ksf-samples", [int(x) for x in r.samples()] == vals)
    check("ksf-loop-enabled", r.is_loop_enabled)
    check("ksf-sample-start", r.sample_start == 500)
    check("ksf-loop-start", r.loop_start == 1000)
    check("ksf-loop-end", r.loop_end == 5000)

s = KsfSample(flags=0x81, loop_start=999, loop_end=999999)
s.set_samples(pcm([1, 2, 3, 4, 5]))
r = KsfSample.open(s.to_bytes())
check("ksf-loopend-not-auto-recomputed", r is not None and r.loop_end == 999999 and not r.is_loop_enabled)

r = KsfSample.open(KsfSample(name="Corrupt", pcm=b"").to_bytes())
check("ksf-header-only-detected", r is not None and r.is_header_only)

# loop-start duplicate slot: preserved verbatim, resynced only on request
tail = bytearray(16)
riff.write_u32be(tail, 0, 0)
riff.write_u32be(tail, 4, 0xFFFBED64)
riff.write_u32be(tail, 8, 1)               # deliberately NOT a mirror
riff.write_u32be(tail, 12, 84552)
smp1 = riff.encode_name_field("X", "", 16) + bytes(tail)
data = (riff.build_chunk("SMP1", smp1) + riff.build_chunk("SNO1", bytes(4))
        + riff.build_chunk("NAME", riff.encode_name_field("X", "", 24))
        + riff.build_chunk("SMD1", bytes([0, 0, 0xAC, 0x44, 0x81, 0, 1, 0x10]) + bytes([0, 0, 0, 10]) + bytes(20)))
o = KsfSample.open(data)
check("ksf-dup-preserved-not-mirrored", o is not None and o.to_bytes() == data)
if o is not None:
    o.clear_preserved_loop_dup()
    check("ksf-dup-clear-resyncs", riff.read_u32be(o.to_bytes(), 8 + 24) == o.loop_start)

check("ksf-open-garbage-null", KsfSample.open(bytes([1, 2, 3])) is None)
check("ksf-open-wrong-first-chunk-null", KsfSample.open(riff.build_chunk("XXXX", bytes(4))) is None)
check("ksf-open-truncated-no-smd1-null", KsfSample.open(riff.build_chunk("SMP1", bytes(32))) is None)

# ---- KMP
m = KmpMultisample(name="Test MS", mno1=7)
m.zones += [KmpZone(original_key=60, top_key=60, filename="MS007000.KSF"),
            KmpZone(original_key=61, top_key=61, filename="MS007001.KSF"),
            KmpZone(original_key=62, top_key=62, filename="SKIPPEDSAMPLE")]
b = m.to_bytes()
r = KmpMultisample.open(b)
check("kmp-reopen-not-null", r is not None)
if r is not None:
    check("kmp-round-trip-bytes-identical", b == r.to_bytes())
    check("kmp-zone-count", len(r.zones) == 3)
    check("kmp-zone0-origkey", r.zones[0].original_key == 60)
    check("kmp-zone0-topkey", r.zones[0].top_key == 60)
    check("kmp-zone0-filename", r.zones[0].filename == "MS007000.KSF")
    check("kmp-zone2-skipped", r.zones[2].is_skipped)
    check("kmp-next-ksf-filename", m.next_ksf_filename() == "MS007003.KSF")

m = KmpMultisample(mno1=1)
m.zones.append(KmpZone(filename="MS001000.KSF", rlp3=bytes([1, 2, 3, 4, 5, 0xFF])))
r = KmpMultisample.open(m.to_bytes())
check("kmp-rlp3-offset5-forced-zero", r is not None and r.zones[0].rlp3[5] == 0)
check("kmp-rlp3-other-bytes-preserved", r is not None and r.zones[0].rlp3[0] == 1 and r.zones[0].rlp3[4] == 5)
check("kmp-open-garbage-null", KmpMultisample.open(bytes([1, 2, 3])) is None)

# ---- KSC
k = KscCollection(bank_uuid="abc-123", entries=["Foo.KMP", "Bar.KSF"])
text = k.to_bytes("Test.KSC").decode("ascii")
check("ksc-has-header", text.startswith("#KORG Script Version 1.0\r\n#v2\r\n#uuid:abc-123\r\n"))
check("ksc-has-companion-block", "#>User.0.2.Foo.KMP" in text and "#>User.0.2.Bar.KSF" in text)
r = KscCollection.open(text.encode("ascii"))
check("ksc-round-trip-entries", r.entries == k.entries)
check("ksc-round-trip-uuid", r.bank_uuid == "abc-123")


def raises(fn):
    try:
        fn()
        return False
    except (ValueError, RuntimeError, IOError):
        return True


check("ksc-refuses-userbank-write", raises(lambda: KscCollection(entries=["Foo.KMP"]).to_bytes("SomeBank_UserBank.KSC")))
check("ksc-refuses-userbank-write-bare-tobytes",
      raises(lambda: KscCollection(path=r"C:\x\SomeBank_UserBank.KSC", entries=["Foo.KMP"]).to_bytes()))

# ---- _UserBank reference-export
root = tempfile.mkdtemp(prefix="kr_userbank_")
cd = os.path.join(root, "UBTEST")
os.makedirs(cd)


def put(name, data):
    with open(os.path.join(cd, name), "wb") as f:
        f.write(data)


m0 = KmpMultisample(name="First One", suffix="", mno1=0)
m0.zones.append(KmpZone(original_key=60, top_key=72, filename="MS000000.KSF"))
put("MS000000.KMP", m0.to_bytes())
m1 = KmpMultisample(name="Second One", suffix="-L", mno1=5)
m1.zones.append(KmpZone(original_key=60, top_key=72, filename="MS005000.KSF"))
put("MS005000.KMP", m1.to_bytes())
ks = KsfSample(name="A Drum Hit", suffix="", sno1=9)
ks.set_samples(pcm([1, -1, 2, -2]))
put("Hit.KSF", ks.to_bytes())
uuid = "dead1234-0000-4000-8000-000000000000"
k = KscCollection(path=os.path.join(root, "UBTEST.KSC"), bank_uuid=uuid, entries=["MS000000.KMP", "MS005000.KMP", "Hit.KSF"])
text = k.to_user_bank_bytes().decode("ascii")
n0 = riff.encode_name_field("First One", "", 24).decode("ascii")
n1 = riff.encode_name_field("Second One", "-L", 24).decode("ascii")
nd = riff.encode_name_field("A Drum Hit", "", 24).decode("ascii")
check("userbank-header", text.startswith("#KORG Script Version 1.0\r\n#v2\r\n"))
check("userbank-no-plain-uuid-line", "\r\n#uuid:dead1234" not in text)
check("userbank-ms0-positional-not-mno1", f"#>>uuid:{uuid}.MS0.1.0.{n0}\r\n" in text)
check("userbank-ms1-positional-not-mno1-5", f"#>>uuid:{uuid}.MS1.1.0.{n1}\r\n" in text)
check("userbank-ds0", f"#>>uuid:{uuid}.DS0.1.0.{nd}\r\n" in text)
check("userbank-summary-line", f"#>uuid:{uuid}.2.1.UBTEST\r\n" in text)
check("userbank-ends-with-summary", text.rstrip("\r\n").endswith(".2.1.UBTEST"))

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
