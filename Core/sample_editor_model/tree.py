"""Tree + small containers for the Sample Editor model — port of ViewModels/SampleTreeNode.cs."""
from __future__ import annotations

import os
from typing import Dict, Generic, Iterator, List, Optional, Tuple, TypeVar

from Data.kmp_multisample import KmpMultisample, KmpZone
from Data.ksc_collection import KscCollection

V = TypeVar("V")


class SampleTreeNode:
    """Collection -> Multisample -> Zone. Exactly one of the three refs is non-None. Deliberately lean: a
    .KSC/.KMP/.KSF tree has no local-vs-Kronos diff to render — disk state IS the truth."""

    def __init__(self, label: str, collection_ref: Optional[Tuple[KscCollection, str]] = None,
                 multisample_ref: Optional[Tuple[KmpMultisample, str]] = None,
                 zone_ref: Optional[Tuple[KmpZone, str]] = None):
        self.label = label
        self.collection_ref = collection_ref
        self.multisample_ref = multisample_ref
        self.zone_ref = zone_ref
        self.is_expanded = False
        self.children: List["SampleTreeNode"] = []

    @staticmethod
    def for_collection(label: str, collection: KscCollection, path: str) -> "SampleTreeNode":
        return SampleTreeNode(label, collection_ref=(collection, path))

    @staticmethod
    def for_multisample(label: str, multisample: KmpMultisample, path: str) -> "SampleTreeNode":
        return SampleTreeNode(label, multisample_ref=(multisample, path))

    @staticmethod
    def for_zone(label: str, zone: KmpZone, kmp_path: str) -> "SampleTreeNode":
        return SampleTreeNode(label, zone_ref=(zone, kmp_path))

    def __repr__(self) -> str:       # pragma: no cover - debugging aid
        return f"<SampleTreeNode {self.label!r}>"


def enumerate_nodes(nodes: List[SampleTreeNode]) -> Iterator[SampleTreeNode]:
    for n in nodes:
        yield n
        yield from enumerate_nodes(n.children)


def is_descendant(root: SampleTreeNode, node: Optional[SampleTreeNode]) -> bool:
    if node is None:
        return False
    if root is node:
        return True
    return any(is_descendant(c, node) for c in root.children)


def find_multisample_containing(nodes: List[SampleTreeNode], zone: KmpZone) -> Optional[KmpMultisample]:
    found = find_multisample_and_path_containing(nodes, zone)[0]
    return found


def find_multisample_and_path_containing(nodes: List[SampleTreeNode], zone: KmpZone
                                         ) -> Tuple[Optional[KmpMultisample], Optional[str]]:
    for node in nodes:
        if node.multisample_ref is not None and any(z is zone for z in node.multisample_ref[0].zones):
            return node.multisample_ref
        r = find_multisample_and_path_containing(node.children, zone)
        if r[0] is not None:
            return r
    return None, None


class PathDict(Generic[V]):
    """Case-insensitive path-keyed mapping that remembers each key's ORIGINAL spelling (the registries
    must hand the real path back when saving), like C#'s Dictionary<string,T>(OrdinalIgnoreCase)."""

    def __init__(self) -> None:
        self._d: Dict[str, Tuple[str, V]] = {}

    @staticmethod
    def _k(path: str) -> str:
        return os.path.normcase(os.path.normpath(path))

    def __setitem__(self, path: str, value: V) -> None:
        self._d[self._k(path)] = (path, value)

    def __getitem__(self, path: str) -> V:
        return self._d[self._k(path)][1]

    def __contains__(self, path: str) -> bool:
        return self._k(path) in self._d

    def __len__(self) -> int:
        return len(self._d)

    def get(self, path: str, default=None):
        e = self._d.get(self._k(path))
        return e[1] if e is not None else default

    def pop(self, path: str, default=None):
        e = self._d.pop(self._k(path), None)
        return e[1] if e is not None else default

    def remove(self, path: str) -> None:
        self._d.pop(self._k(path), None)

    def clear(self) -> None:
        self._d.clear()

    def keys(self) -> List[str]:
        return [p for p, _ in self._d.values()]

    def values(self) -> List[V]:
        return [v for _, v in self._d.values()]

    def items(self) -> List[Tuple[str, V]]:
        return list(self._d.values())
