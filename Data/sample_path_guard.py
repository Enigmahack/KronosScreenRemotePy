"""Path-traversal guard for sample-editor local paths — port of C#'s
Core/Sample/SamplePathGuard.cs.

Every local path in the sample editor is derived from a NAME that came out of
a file: a .KSC's plain entry lines, a .KMP zone's 12-byte filename field, an
FTP listing. None of those are validated by the formats themselves, and a
corrupt or hand-edited manifest could steer a pull/save outside the workspace
it's supposed to stay inside (a rooted name, or "..").

Defense in depth, not an exploit fix — the input is the user's own Kronos —
so this raises rather than introducing a new failure category; callers catch
it and route into their own failures list, same as an unreachable file.
"""
from __future__ import annotations

import os


def ensure_under(root: str, candidate: str, for_display: str) -> str:
    full_root = os.path.abspath(root)
    full_candidate = os.path.abspath(candidate)
    prefix = full_root.rstrip(os.sep) + os.sep
    if not full_candidate.startswith(prefix):
        raise IOError(f"'{for_display}' resolves outside the collection folder")
    return full_candidate
