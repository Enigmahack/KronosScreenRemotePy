"""CAL_GET/CAL_SET text serialization — port of C#'s Core/Models.cs CalText.

docs/api.md's CAL_GET/CAL_SET section: the calibration mesh is stored on the
Kronos/Nautilus itself (/korg/rw/HD/ScreenRemote/calibration.txt) so it follows
the instrument rather than the PC that made it. `<text>` is opaque to the
daemon — it just stores and returns it unchanged, checking only that every
character is one of `0-9 - , ; = G M D` and space, up to 4096 bytes. The format
below (`G=<grid> M=<col,row,dx,dy;...> D=<x,y;...>`) is "the Windows client"'s
own format per that doc, i.e. this one — kept byte-for-byte compatible so a
calibration saved by either app reads back correctly in the other.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from Models.models import CalBiasDot, CalMesh

MAX_LENGTH = 4096


def serialize(mesh: CalMesh, dots: List[CalBiasDot]) -> str:
    nodes = []
    for c in range(mesh.cols):
        for r in range(mesh.rows):
            ox, oy = mesh.get_offset(c, r)
            if ox != 0 or oy != 0:
                nodes.append(f"{c},{r},{ox},{oy}")
    dot_text = ";".join(f"{d.nx},{d.ny}" for d in dots)
    return f"G={mesh.cols} M={';'.join(nodes)} D={dot_text}"


def _parse_ints(s: str, count: int) -> Optional[List[int]]:
    parts = s.split(",")
    if len(parts) != count:
        return None
    out = []
    for p in parts:
        try:
            out.append(int(p))
        except ValueError:
            return None
    return out


def parse(text: str) -> Optional[Tuple[CalMesh, List[CalBiasDot]]]:
    """None if the text is malformed; a partly-readable calibration is not
    applied at all (matches C#'s CalText.Parse)."""
    size = 5
    mesh_part = ""
    dot_part = ""
    for field in text.split(" "):
        if not field:
            continue
        if field.startswith("G="):
            try:
                size = int(field[2:])
            except ValueError:
                return None
            if size not in (3, 4, 5):
                return None
        elif field.startswith("M="):
            mesh_part = field[2:]
        elif field.startswith("D="):
            dot_part = field[2:]
        else:
            return None

    mesh = CalMesh(size, size)
    if mesh_part:
        for node in mesh_part.split(";"):
            if not node:
                continue
            v = _parse_ints(node, 4)
            if v is None or not (0 <= v[0] < size) or not (0 <= v[1] < size):
                return None
            mesh.set_offset(v[0], v[1], v[2], v[3])

    dots: List[CalBiasDot] = []
    if dot_part:
        for dot in dot_part.split(";"):
            if not dot:
                continue
            v = _parse_ints(dot, 2)
            if v is None:
                return None
            dots.append(CalBiasDot(v[0], v[1]))

    return mesh, dots
