"""Kronos -> Nautilus object conversion. UI-free: no Qt imports, usable from tests and a CLI."""
from Conversion.result import ConversionResult
from Conversion.profiles import ProgramKind

__all__ = ["ConversionResult", "ProgramKind"]
