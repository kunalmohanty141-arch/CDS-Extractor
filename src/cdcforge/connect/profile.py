"""F-01 — connection profiles and credential storage.

The password never appears in the profile file, never in the SQLite store, and
never in the audit log. It lives in the OS keyring — Windows Credential Manager
here — and is read on demand.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path

try:  # pragma: no cover - exercised by environment, not tests
    import keyring
except ImportError:  # pragma: no cover
    keyring = None  # type: ignore[assignment]

try:  # pragma: no cover
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

KEYRING_SERVICE = "cdc-forge"

DEFAULT_PROFILE_DIR = Path.home() / ".cdc-forge" / "profiles"


class SystemRole(str, Enum):
    """Client role, from T000-CCCATEGORY (F-03)."""

    DEVELOPMENT = "D"
    QUALITY = "Q"
    TEST = "T"
    CUSTOMISING = "C"
    PRODUCTION = "P"
    UNKNOWN = "UNKNOWN"

    @property
    def is_productive(self) -> bool:
        """Unknown counts as productive.

        Fail safe. If the tool cannot establish what it is connected to, the
        expensive mistake is writing to production, not refusing to write to a
        sandbox.
        """
        return self in (SystemRole.PRODUCTION, SystemRole.UNKNOWN)

    @property
    def label(self) -> str:
        return {
            SystemRole.DEVELOPMENT: "Development",
            SystemRole.QUALITY: "Quality assurance",
            SystemRole.TEST: "Test",
            SystemRole.CUSTOMISING: "Customising",
            SystemRole.PRODUCTION: "PRODUCTION",
            SystemRole.UNKNOWN: "UNKNOWN (treated as production)",
        }[self]

    @classmethod
    def parse(cls, value: str | None) -> "SystemRole":
        if not value:
            return cls.UNKNOWN
        try:
            return cls(value.strip().upper()[:1])
        except ValueError:
            return cls.UNKNOWN


class CredentialError(Exception):
    """No usable credential for this profile."""


def _restrict(path: Path) -> None:
    """Make a profile readable by its owner only, where the OS supports it.

    No password lives here, so this is not about secrets — it is about a file
    that names a host, a client and a *user* who has development authorisation
    on it. That is a starting point for somebody, and it defaults to
    world-readable otherwise.

    POSIX only, and failure is ignored: Windows ACLs are not chmod, and a
    profile that saved but could not be locked down is better than no profile.
    """
    with contextlib.suppress(OSError, NotImplementedError):
        path.chmod(0o600)


@dataclass
class ConnectionProfile:
    """One system. Shape follows §3.2 of the specification."""

    profile_id: str
    host: str
    client: str
    username: str
    description: str = ""
    port: int = 44300
    protocol: str = "https"
    language: str = "EN"
    auth_method: str = "basic"
    verify_ssl: bool = True
    ca_bundle_path: str = ""
    tls_override_reason: str = ""
    """Why certificate verification is off, in the operator's own words.

    Required whenever ``verify_ssl`` is false, and the requirement is the
    point. Authentication here is HTTP Basic — the password crosses the wire
    on every single request — so an unverified connection is not a cosmetic
    warning, it is *anyone on the path can read the password*.

    Self-signed certificates really are the norm on sandboxes, so refusing
    outright would make the tool useless where it is most used. But
    ``verify_ssl: false`` on its own is one word in a file that gets copied
    from colleague to colleague, and it copies silently. Making it two keys —
    the switch and a stated reason — means nobody turns it off without saying
    so, and the reason travels with the profile to whoever reads it next.

    The honest fix is still ``ca_bundle_path`` pointing at the issuing CA.
    """
    system_role: str = SystemRole.UNKNOWN.value
    system_id: str = ""
    """Three-character SID.

    Configuration rather than discovery: the ICM stamps ``sap-system`` on
    *unauthenticated* responses but not on authenticated ones, and
    ``/sap/bc/adt/core/systeminformation`` does not exist on every release. It
    is also what the production-guard override requires the operator to type,
    so it has to be known before the first write regardless.
    """

    timeout_seconds: int = 60
    max_retries: int = 3

    # Never persisted. Populated only when a caller passes a password directly.
    _password: str | None = field(default=None, repr=False, compare=False)

    # -- derived ----------------------------------------------------------
    @property
    def base_url(self) -> str:
        return f"{self.protocol}://{self.host}:{self.port}"

    @property
    def role(self) -> SystemRole:
        return SystemRole.parse(self.system_role)

    @property
    def keyring_key(self) -> str:
        return f"{self.profile_id}:{self.client}:{self.username}"

    @property
    def verify(self) -> bool | str:
        """The value requests' ``verify`` parameter should receive."""
        if not self.verify_ssl:
            return False
        return self.ca_bundle_path or True

    @property
    def tls_is_unverified(self) -> bool:
        return not self.verify_ssl

    def check_tls(self) -> None:
        """Refuse an unverified connection that nobody has justified.

        Raises before the first request, and therefore before the password is
        sent — which is the whole point, since Basic auth puts it on the wire
        every time.
        """
        if self.verify_ssl or self.tls_override_reason.strip():
            return
        raise CredentialError(
            f"profile {self.profile_id!r} disables TLS certificate "
            f"verification but gives no reason.\n"
            f"  Authentication is HTTP Basic, so the password for "
            f"{self.username!r} crosses the wire on every request. Without "
            f"verification, anyone on the network path can read it.\n"
            f"  Fix it properly:  set ca_bundle_path to the CA that issued "
            f"{self.host}'s certificate.\n"
            f"  Or accept it knowingly:  add a line to the profile —\n"
            f"      tls_override_reason: \"self-signed cert on an isolated "
            f"sandbox\"\n"
            f"  Every audit record from this profile is stamped "
            f"ssl_verified=false either way."
        )

    def tls_warning(self) -> str:
        """A line worth printing on every connect, or empty when verified."""
        if self.verify_ssl:
            return ""
        return (
            f"TLS NOT VERIFIED for {self.host} — the password for "
            f"{self.username!r} is sent on every request over a connection "
            f"nobody has authenticated. Reason on file: "
            f"{self.tls_override_reason.strip() or '(none)'}"
        )

    # -- credentials -------------------------------------------------------
    def password(self) -> str:
        """Fetch the password: explicit value, then keyring, then environment.

        The environment fallback exists for CI and headless runs. It is last on
        purpose — an environment variable is visible to every process the user
        runs, and the keyring is not.
        """
        if self._password:
            return self._password

        if keyring is not None:
            stored = keyring.get_password(KEYRING_SERVICE, self.keyring_key)
            if stored:
                return stored

        from_env = os.environ.get("CDC_FORGE_PASSWORD")
        if from_env:
            return from_env

        raise CredentialError(
            f"no password stored for {self.keyring_key!r}. Run "
            f"'cdc-forge login --profile {self.profile_id}' to store one in the "
            f"OS keyring, or set CDC_FORGE_PASSWORD for this session."
        )

    def store_password(self, password: str) -> None:
        if keyring is None:  # pragma: no cover
            raise CredentialError(
                "the 'keyring' package is not installed, so the password cannot "
                "be stored securely. Install it rather than falling back to a "
                "plaintext file."
            )
        keyring.set_password(KEYRING_SERVICE, self.keyring_key, password)

    # -- persistence -------------------------------------------------------
    def to_dict(self) -> dict:
        data = {k: v for k, v in asdict(self).items() if not k.startswith("_")}
        return data

    def save(self, directory: Path | None = None) -> Path:
        directory = directory or DEFAULT_PROFILE_DIR
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.profile_id}.yaml"
        payload = self.to_dict()
        if yaml is not None:
            path.write_text(
                yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
            )
        else:  # pragma: no cover
            import json

            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        _restrict(path)
        return path

    @classmethod
    def load(cls, profile_id: str, directory: Path | None = None) -> "ConnectionProfile":
        directory = directory or DEFAULT_PROFILE_DIR
        path = directory / f"{profile_id}.yaml"
        if not path.is_file():
            raise FileNotFoundError(
                f"no profile {profile_id!r} in {directory}. Create one with "
                f"'cdc-forge profile add'."
            )
        return cls.from_file(path)

    @classmethod
    def from_file(cls, path: Path) -> "ConnectionProfile":
        text = path.read_text(encoding="utf-8")
        if yaml is not None:
            raw = yaml.safe_load(text)
        else:  # pragma: no cover
            import json

            raw = json.loads(text)
        if not isinstance(raw, dict):
            raise ValueError(f"{path} does not contain a profile mapping")
        raw.pop("password", None)  # refuse to honour a password written by hand
        known = {f for f in cls.__dataclass_fields__ if not f.startswith("_")}
        unknown = set(raw) - known
        if unknown:
            raise ValueError(f"{path}: unknown profile keys {sorted(unknown)}")
        return cls(**raw)

    @classmethod
    def list_profiles(cls, directory: Path | None = None) -> list[str]:
        directory = directory or DEFAULT_PROFILE_DIR
        if not directory.is_dir():
            return []
        return sorted(p.stem for p in directory.glob("*.yaml"))
