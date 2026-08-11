"""CDC Forge — ABAP CDS Change Data Capture readiness validator and generator.

Stage 1 (this package as it stands) is the *offline core*: parser, rule engine,
generator and verdict model. Per the build specification these modules must have
**zero** SAP dependency — they take DDL text and metadata fixtures in, and
produce verdicts and DDL out. That is what makes the hard logic testable
offline and demoable without a system.
"""

from cdcforge.model import (
    Assessment,
    Outcome,
    RuleResult,
    Severity,
    SourceRef,
    Verdict,
)

__version__ = "0.1.0"

__all__ = [
    "Assessment",
    "Outcome",
    "RuleResult",
    "Severity",
    "SourceRef",
    "Verdict",
    "__version__",
]
