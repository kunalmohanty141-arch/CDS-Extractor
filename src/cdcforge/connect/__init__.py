"""Stage 2 — the read-only connector.

This is the only package in the codebase that talks to SAP. The offline core
(`cdcforge.parsing`, `cdcforge.rules`, `cdcforge.generator`) must never import
from here, and this package must never be needed to run them.

Requires `requests`; `keyring` for credential storage. Both are imported lazily
so the offline core stays dependency-free.
"""

from cdcforge.connect.audit import AuditLog, AuditRecord, NullAuditLog
from cdcforge.connect.checkrun import Agreement, CheckRunResult, compare, run_checkrun
from cdcforge.connect.preflight import Check, PreflightReport, Status, run_preflight
from cdcforge.connect.profile import ConnectionProfile, CredentialError, SystemRole
from cdcforge.connect.session import (
    AdtError,
    AdtSession,
    AuthenticationFailed,
    AuthorizationFailed,
    HostUnreachable,
    ProductionGuardViolation,
    ReadOnlyViolation,
    SicfNodeInactive,
    TlsProblem,
)
from cdcforge.connect.source import AdtMetadataSource

__all__ = [
    "AdtError",
    "AdtMetadataSource",
    "AdtSession",
    "Agreement",
    "AuditLog",
    "AuditRecord",
    "AuthenticationFailed",
    "AuthorizationFailed",
    "Check",
    "CheckRunResult",
    "ConnectionProfile",
    "CredentialError",
    "HostUnreachable",
    "NullAuditLog",
    "PreflightReport",
    "ProductionGuardViolation",
    "ReadOnlyViolation",
    "SicfNodeInactive",
    "Status",
    "SystemRole",
    "TlsProblem",
    "compare",
    "run_checkrun",
    "run_preflight",
]
