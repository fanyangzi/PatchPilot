"""Framework adapters used by the source-aware verification engine.

Adapters only interpret measured runner output.  They never turn an absent or
unsupported test result into a pass; callers receive an explicit ``outcome``
and can preserve the result in the verification evidence graph.
"""

from .adapters.base import AdapterResult, TestAdapter, adapter_for_command
from .adapters.pytest import PytestAdapter
from .adapters.vitest import VitestAdapter
from .probes import (
    ProbeCase,
    ProbeClassification,
    ProbeObservation,
    classify_observations,
    generate_boundary_inputs,
    minimize_failure,
)

__all__ = [
    "AdapterResult", "TestAdapter", "PytestAdapter", "VitestAdapter", "adapter_for_command",
    "ProbeCase", "ProbeObservation", "ProbeClassification", "generate_boundary_inputs",
    "minimize_failure", "classify_observations",
]
