# Persona: Show preparer

Prepares an instrument for a performance, offline in advance. Assembles set lists, combis, banks and
the samples they depend on, often for a specific unit, then loads it and verifies. Nothing may be lost
or reordered silently, because the first time the mistake surfaces may be on stage.

Data they bring: their own PCG backups, a previous show's set list, a second unit (maybe a different
model) as the target, a USB stick or FTP share with samples.

## Scenarios

| ID | Task | Tier |
|---|---|---|
| SP1 | Pull the whole library, reorder set list slots, push; slot order must match exactly after a re-pull | live |
| SP2 | Build a bank from existing programs/combis with Auto-Fill; check no slot is clobbered | offline + live |
| SP3 | Back up, then restore from backup after a deliberately wrong push | offline + live |
| SP4 | Move a show from a Kronos to a Nautilus (or reverse); report everything that could not convert | offline fixtures |
| SP5 | Verify/commit staging (red/green): stage changes, discard some, commit the rest; unit matches the staged result | live |
| SP6 | File Manager: copy a sample set to the unit, rename a folder, delete an old one; unit volumes protected | live (fake FTP offline) |
| SP7 | Connection drops mid-push or mid-pull (cable pulled, unit powered off); state must be recoverable | offline fault injection + live |
| SP8 | Open a library last used on another app version; settings and caches from older files load without damage | offline |
| SP9 | Two saved connections (two units); never write to the wrong one | offline |

## Invariants

- After push then re-pull, content equals what was staged, same order and count.
- A backup always exists before a destructive write, and restore reproduces it byte for byte.
- The target unit shown in the UI is the unit the bytes go to.
- Partial failure surfaces as a visible, specific error with the recovery path; never a green status.
