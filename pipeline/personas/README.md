# Pipeline personas

Two layers, kept separate on purpose.

- `personas/` (user personas): sound-designer.md, show-preparer.md. Scenarios (`SD*`, `SP*`) and invariants.
  Each scenario is turned into a script test where it can be (fixtures, fake FTP, fault injection);
  the exploratory-tester agent covers the rest.
- `.claude/agents/` (production roles): code-reviewer, safety-reviewer, exploratory-tester, ux-reviewer.
  They read `pipeline/out/digest.txt` first, then only the files and logs it points at.

Run order: `python pipeline/run.py` (tier 0) on every change, `--tier 1` before merge,
live-hardware scripts (`tests/live_*.py`) only on request or pre-release.
