"""Hardware fixture locations, overridable for separate checkouts on any OS."""
import os
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent.parent.parent
SAMPLE_FIXTURES = os.environ.get(
    "KRONOS_SAMPLE_FIXTURES", str(WORKSPACE / "KronosScreenRemote" / "SampleFixtures"))
PCG_EXAMPLES = os.environ.get("KRONOS_PCG_EXAMPLES", str(WORKSPACE / "PCG EXAMPLES"))
