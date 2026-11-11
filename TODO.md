# To-do

Open work after the cross-family conversion and sample-mapping passes (2026-10-29). Hardware checks that wait for an
instrument are in `CLAUDE.md` ("Kronos live checklist", items 1-12); they are only pointed at here.

## Wave Sequence review (2026-10-29, 309 unique sequences in the PCG corpus, Kronos -> Nautilus)

Converted 131 with the old map, 181 with the new one (+50). Still refused: 128.

| Blocked only by | Sequences | Cause |
|---|---|---|
| EXs11 | 114 | "Legendary Strings": 83 distinct multisamples; this Kronos has no EXs11 installed (Bank11 is empty), so there is no audio to compare. No Nautilus multisample name resembles them (no TempoTrem, Trill, CBass, CelloG...). |
| EXs3 | 7 | 10 multisamples (flute / oboe attacks and glisses, cornet / trombone falls and doits): no Nautilus audio. Same-named Nautilus samples (e.g. "Flute 2-Attack P") are different recordings (residual 0.95-1.00). |
| ROM | 6 | #438 StrEns-Slina REV, #1203 Synth Hit Loop 1 (x7 steps), #1494 Ride Cymbal 03 mp: no Nautilus audio, no reversed or trimmed copy. |
| EXs1 | 1 | #78 Tape Choir: no Nautilus audio. |

- [x] Unmatched Wave Sequence step: plays the default piano, with a warning naming the step (done; all 309 corpus sequences convert).
- [ ] EXs11: needs a Kronos with EXs11 installed (pull `Bank11`, then run the comparison). Low odds of a match.

## Next: Drum Kits and Wave Sequences

The new sample mapping reaches them (better ROM matches, EXs banks, bank-id rewrite), but the edge cases are not handled yet.

- [ ] A Drum Kit zone with no equivalent is switched off; a kit where every zone has none is refused. Decide whether a kit zone
      should get a fallback sample instead (the default piano makes no sense on a drum note) or stay silent and be listed.
- [ ] A Wave Sequence with one unmatched step is refused outright. Decide: skip the step, replace it, or keep refusing.
- [ ] `remap_drum_kit` / `remap_wave_seq` take no `notes`, so a partial multisample in a Wave Sequence step gets no
      "may differ slightly" warning and a Drum Kit never reports one.
- [ ] Reverse: a Wave Sequence step that points at a Kronos REV multisample maps to the forward sample but nothing sets a
      reverse flag (the Program zone has a Reverse bit; check whether a Wave Sequence step or a Drum Kit zone has one).
- [ ] Drum Kits reference drumsamples; 3,189 Kronos drumsamples have no Nautilus match (mostly Kronos-only banks). List which
      factory kits lose notes, and how many.
- [ ] Drum Kits and Wave Sequences are the same size on both instruments, so a wrong-family one cannot be detected at push.
      Look for another marker (bank-id form in the references, ROM index range) or carry the family with the object.
- [ ] The "Every sample in this kit has no equivalent" refusal and the silent-notes warning: check them against real kits.
- [ ] Tests on real kits and sequences from the PCG corpus, both directions, like the Program corpus run (100,352 Programs, no refusals).

## Sample mapping follow-ups

- [ ] Show the "replace these sounds by hand" list in the Librarian. Today it is only logged per object and kept in
      `LibrarianShellWindow._pcg_conv_replaced`; the manual Convert dialog shows warnings but not as a list.
- [ ] Default piano is ROM multisample 0, the left half of a stereo piano, so it is mono. Consider multisample 0 + 1 or a mono piano.
- [x] Nautilus Bank330 (2026-11-11): its 3 files match the unit's md5sum (`/korg/rw/PCM/Bank330`, read-only over SSH), marker
      written, hashed (546 blocks, 0 unreadable) and fingerprinted. It is the Nautilus counterpart of the Kronos third-party bank
      206: 85 new trim matches, and Kronos Bank206 now has 175 of 540 blocks on the Nautilus (9 multisamples exact, 3 partial,
      22 none; before: nothing). `NautilusSampleEvidence.json` is UNCHANGED because `build_conversion_evidence.py` skips banks >= 200
      (third-party). DONE 2026-11-11 (see CLAUDE.md "Third-party packs"): the builder no longer skips them, 12 Bank206 -> Bank330
      entries are in the evidence, and the converter resolves raw bank ids in both directions. Unverified: a Kronos Program's own
      stored id for Bank206 (checklist 13).
- [ ] Third-party packs the Kronos has but the Nautilus does not (206-214, 217, 218 other than 206's 12 multisamples; 22 of 34
      EXs206 multisamples have no Nautilus audio) still refuse / fall back to the default piano. Hash and compare any other
      Nautilus third-party bank the owner installs (the only one seen so far is Bank330 "A2D King of Polysynths").
- [ ] Kronos "...REV" flag (+0x3C bit 0x200) and the Program Zone MS Reverse bit are inferred, not hardware-checked. Other bits of
      that word are unexplained.
- [ ] The Kronos has no match for banks 06, 07, 17, 109, 206-214, 217, 218, 23, 40-42. Confirm none of those are on the Nautilus under
      different audio (e.g. other sample rates); the comparison only finds identical audio (trimmed, same length class).
- [ ] Rotated or re-pitched copies were searched only as trim and reversal; there were no reversed or rotated matches.
- [ ] Write the findings up in `Nautilus Research/MAIN/07_pcg_and_sampling` (EXs bank layout, `EXs<N>` = `Bank<N>`, bank-id encoding,
      REV samples are flagged copies of the forward block, trim matches).
- [ ] Window-level test that conversion-on-entry logs the substitution and layering lines.

## Conversion gaps

- [ ] Nautilus `g(d)` (GM drum) Timbre references in Combis: a Kronos Combi using them cannot be converted faithfully and nothing warns (checklist 11).
- [ ] Arp-pattern transcription exists only in the manual "Convert to Nautilus..." menu; conversion on entry cannot write patterns.
- [ ] Objects moved to Merge before the device family is detected arrive in the Kronos format; the push guard then refuses them.
- [ ] Set Lists are refused across instruments (their user-bank references would be off by one).
- [ ] Kronos Combi/Program with an EXs reference the target lacks: the Combi keeps the Program but its sound is the default piano; report per Combi.

## Needs an instrument

- [ ] Checklist items 9-12: Nautilus -> Kronos conversion, Combi Timbre bank byte, `g(d)`, sample references (REV, EXs1, EXs22, default piano).
- [ ] One Push Only on the Nautilus: convert OTE on entry, auto-fill a Program and a Combi, push, read back, restore from backup.
- [ ] Nautilus daemon: it was 3.1.2 when last seen; 3.1.3 is the newest build (the Kronos already runs it). The dist packages are USB UpdateOS
      packages that edit GRUB, so on the rooted unit prefer copying the 3.1.3 `screenremote` binary after backing up the old one.
- [ ] Older, unconfirmed: footer icon colour and Librarian launch time not visually checked.
