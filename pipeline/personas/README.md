# Pipeline personas

Two layers, kept separate on purpose.

- `personas/` (user personas): sound-designer.md, show-preparer.md. Scenarios (`SD*`, `SP*`) and invariants.
  Each scenario is turned into a script test where it can be (fixtures, fake FTP, fault injection);
  the exploratory-tester agent covers the rest.
- `.claude/agents/` (production roles): code-reviewer, safety-reviewer, exploratory-tester, ux-reviewer.
  They read `pipeline/out/digest.txt` first, then only the files and logs it points at.

Run order: `python pipeline/run.py` (tier 0) on every change, `--tier 1` before merge,
live-hardware scripts (`tests/live_*.py`) only on request or pre-release.

Reviewer agents are not registered until a new Claude Code session starts; in the same session as an edit, run them
as general-purpose agents told to read their definition file first. Measured on the first run (4 recent commits):
each reported in 30-110 s for 70-100k tokens, and the definition tweaks above came from their own process notes.
