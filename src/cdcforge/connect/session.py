"""The ADT session — §3.4, F-01, F-03, F-04.

Session lifecycle, CSRF handling, retry policy, and the two guards that decide
whether a request is allowed to leave at all:

* **READ_ONLY_MODE (F-04)** — a global toggle that makes the tool provably
  incapable of writing. Enforced *here*, at the connector layer, not at the UI
  layer. Defence in depth: a UI bug cannot route around it.
* **Production guard (F-03)** — if the target client is productive, every write
  is blocked. Unknown role counts as productive.

Both guards run before the request is constructed, so a blocked call never
touches the network.

Note on the read-only rule. The specification says READ_ONLY_MODE "blocks every
non-GET". Read literally that would block ``checkruns``, which is a POST that
changes nothing and which the same document calls the single most important
endpoint in the tool — and which the read-only assessment product depends on.
So the switch blocks anything not classified READ in ``endpoints.py``, and
anything ``endpoints.py`` has never heard of. Same promise, correct boundary.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol

from cdcforge.connect import endpoints as ep
from cdcforge.connect.audit import AuditLog, AuditRecord, body_hash
from cdcforge.connect.profile import ConnectionProfile, SystemRole


# ---------------------------------------------------------------------------
# Errors — every one names its cause and what to do about it (F-01)
# ---------------------------------------------------------------------------


class AdtError(Exception):
    """Base class. Carries a cause and a remedy, never a bare stack trace."""

    cause = "unknown"

    def __init__(self, message: str, remedy: str = "", status: int | None = None):
        super().__init__(message)
        self.message = message
        self.remedy = remedy
        self.status = status

    def render(self) -> str:
        out = f"{self.cause}: {self.message}"
        if self.remedy:
            out += f"\n  → {self.remedy}"
        return out


class HostUnreachable(AdtError):
    cause = "host unreachable"


class TlsProblem(AdtError):
    cause = "TLS"


class AuthenticationFailed(AdtError):
    cause = "authentication"


class AuthorizationFailed(AdtError):
    cause = "authorization"


class SicfNodeInactive(AdtError):
    cause = "SICF node inactive"


class ReadOnlyViolation(AdtError):
    cause = "blocked by read-only mode"


class ProductionGuardViolation(AdtError):
    cause = "blocked by production guard"


class AdtHttpError(AdtError):
    cause = "HTTP error"


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


class Transport(Protocol):
    """The slice of ``requests.Session`` this module uses.

    Narrow on purpose: it is what lets the guard logic and the CSRF dance be
    unit-tested without a system, which is the only way any of this could be
    verified before today.
    """

    def request(self, method: str, url: str, **kwargs: Any) -> Any: ...


def _requests_transport(profile: ConnectionProfile, password: str) -> Transport:
    import requests  # imported lazily so the offline core stays dependency-free

    session = requests.Session()
    session.auth = (profile.username, password)
    session.headers.update(
        {
            # No blanket Accept here. ADT negotiates content strictly and
            # answers 406 rather than guessing, and the right type differs per
            # endpoint — text/plain for source, a vendor XML type for object
            # metadata. It is set per request from the endpoint map.
            "sap-client": profile.client,
            "sap-language": profile.language,
            "User-Agent": "cdc-forge/0.1",
        }
    )
    session.verify = profile.verify
    return session


@dataclass
class AdtResponse:
    status: int
    text: str
    headers: dict
    elapsed_ms: int

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class AdtSession:
    """One authenticated conversation with one system."""

    def __init__(
        self,
        profile: ConnectionProfile,
        audit: AuditLog,
        *,
        read_only: bool = True,
        transport: Transport | None = None,
        password: str | None = None,
        sleep=time.sleep,
    ) -> None:
        self.profile = profile
        self.audit = audit
        # Read-only is the default. Turning it off has to be a deliberate act by
        # the caller, not the consequence of forgetting an argument.
        self.read_only = read_only
        self.system_role: SystemRole = profile.role
        self.system_id: str = profile.system_id
        self.csrf_token: str = ""
        self.stateful: bool = False
        self._sleep = sleep
        self._production_override = False

        # Before the transport exists, and therefore before the password can
        # be sent. Basic auth puts it on the wire with every request, so an
        # unverified connection nobody has justified is refused here rather
        # than warned about after the fact.
        if transport is None:
            profile.check_tls()

        self._transport = transport or _requests_transport(
            profile, password if password is not None else profile.password()
        )

    # -- lifecycle ---------------------------------------------------------
    def connect(self) -> AdtResponse:
        """Fetch the CSRF token and prove the node is reachable."""
        response = self._send(
            ep.DISCOVERY.path,
            "GET",
            headers={"X-CSRF-Token": "Fetch"},
            action="connect",
        )
        token = response.headers.get("x-csrf-token") or response.headers.get(
            "X-CSRF-Token"
        )
        if token and token.lower() != "required":
            self.csrf_token = token
        return response

    def logoff(self) -> None:
        """Explicit session drop on exit (§3.4 step 5)."""
        close = getattr(self._transport, "close", None)
        if callable(close):
            close()
        self.csrf_token = ""
        self.audit.record(
            AuditRecord(
                profile_id=self.profile.profile_id,
                action="logoff",
                user=self.profile.username,
                client=self.profile.client,
                system_id=self.system_id,
                ssl_verified=self.profile.verify_ssl,
                read_only=self.read_only,
                outcome="ok",
            )
        )

    def __enter__(self) -> "AdtSession":
        self.connect()
        return self

    def __exit__(self, *exc_info) -> None:
        self.logoff()

    # -- guards ------------------------------------------------------------
    def authorise_production_writes(self, system_id: str, client: str) -> bool:
        """Override the production guard.

        Requires the caller to reproduce the system ID and client exactly —
        clicking OK is not enough (F-03). Returns whether the override took.
        """
        matches = (
            system_id.strip().upper() == (self.system_id or "").upper()
            and client.strip() == self.profile.client
        )
        self._production_override = matches
        self.audit.record(
            AuditRecord(
                profile_id=self.profile.profile_id,
                action="production-override",
                user=self.profile.username,
                client=self.profile.client,
                system_id=self.system_id,
                ssl_verified=self.profile.verify_ssl,
                read_only=self.read_only,
                outcome="granted" if matches else "refused",
                detail=f"typed system_id={system_id!r} client={client!r}",
            )
        )
        return matches

    def _check_guards(self, method: str, path: str) -> None:
        access = ep.classify(method, path)
        if access is ep.Access.READ:
            return

        if self.read_only:
            raise ReadOnlyViolation(
                f"{method} {path} would modify the system, and READ_ONLY_MODE is on",
                remedy="Read-only mode is enforced in the connector, not the UI. "
                "Start the session with read_only=False to allow writes.",
            )

        if self.system_role.is_productive and not self._production_override:
            unknown = self.system_role is SystemRole.UNKNOWN
            raise ProductionGuardViolation(
                f"{method} {path} targets a client whose role is "
                f"{self.system_role.label}"
                + (" (treated as production)" if unknown else ""),
                remedy=(
                    # An unknown role is the common case and has a real fix, so
                    # say what it is rather than offering only the override.
                    "The client role has not been established, and unknown "
                    "counts as production. Run the preflight first — it reads "
                    "T000 and tells the session what the client actually is."
                    if unknown
                    else "Writes to a productive client are blocked. Confirm by "
                    "typing the system ID and client exactly, if this is "
                    "genuinely intended."
                ),
            )

    # -- request -----------------------------------------------------------
    def request(
        self,
        path: str,
        method: str = "GET",
        *,
        body: str | None = None,
        headers: dict | None = None,
        params: dict | None = None,
        action: str = "",
        object_name: str = "",
    ) -> AdtResponse:
        self._check_guards(method, path)
        return self._send(
            path,
            method,
            body=body,
            headers=headers,
            params=params,
            action=action or method.lower(),
            object_name=object_name,
        )

    def get(self, path: str, **kwargs) -> AdtResponse:
        return self.request(path, "GET", **kwargs)

    def post(self, path: str, **kwargs) -> AdtResponse:
        return self.request(path, "POST", **kwargs)

    def put(self, path: str, **kwargs) -> AdtResponse:
        return self.request(path, "PUT", **kwargs)

    def delete(self, path: str, **kwargs) -> AdtResponse:
        return self.request(path, "DELETE", **kwargs)

    # -- internals ---------------------------------------------------------
    def _send(
        self,
        path: str,
        method: str,
        *,
        body: str | None = None,
        headers: dict | None = None,
        params: dict | None = None,
        action: str = "",
        object_name: str = "",
        _csrf_retried: bool = False,
        _attempt: int = 1,
    ) -> AdtResponse:
        url = self.profile.base_url + path
        request_headers = dict(headers or {})
        request_headers.setdefault("Accept", ep.accept_for(method, path))
        endpoint = ep.find(method, path)
        if endpoint is not None and endpoint.content_type and body is not None:
            request_headers.setdefault("Content-Type", endpoint.content_type)
        if self.csrf_token and method.upper() != "GET":
            request_headers.setdefault("X-CSRF-Token", self.csrf_token)
        if self.stateful:
            request_headers.setdefault("X-sap-adt-sessiontype", "stateful")

        started = time.monotonic()
        try:
            raw = self._transport.request(
                method,
                url,
                data=body.encode("utf-8") if isinstance(body, str) else body,
                headers=request_headers,
                params=params,
                timeout=self.profile.timeout_seconds,
            )
        except Exception as exc:
            self._audit(
                action or method.lower(), method, path, object_name,
                None, int((time.monotonic() - started) * 1000), body, "",
                outcome="transport-error", detail=type(exc).__name__,
            )
            raise self._translate_transport_error(exc) from exc

        elapsed_ms = int((time.monotonic() - started) * 1000)
        response = AdtResponse(
            status=raw.status_code,
            text=getattr(raw, "text", "") or "",
            headers={k.lower(): v for k, v in dict(raw.headers).items()},
            elapsed_ms=elapsed_ms,
        )

        # The ICM stamps the system ID on every response. Cheaper and more
        # reliable than any endpoint, and it works even when the request failed.
        if not self.system_id:
            self.system_id = response.headers.get("sap-system", "") or ""

        self._audit(
            action or method.lower(), method, path, object_name,
            response.status, elapsed_ms, body, response.text,
            outcome="ok" if response.ok else "error",
        )

        # CSRF: refresh once and retry (§3.4 step 4).
        if (
            response.status == 403
            and not _csrf_retried
            and "csrf" in response.text.lower()
        ):
            self.connect()
            return self._send(
                path, method, body=body, headers=headers, params=params,
                action=action, object_name=object_name, _csrf_retried=True,
            )

        # Exponential backoff on 5xx only. A 4xx is a statement about the
        # request, and repeating it unchanged just annoys the gateway.
        if 500 <= response.status < 600 and _attempt < self.profile.max_retries:
            self._sleep(min(2 ** (_attempt - 1), 8))
            return self._send(
                path, method, body=body, headers=headers, params=params,
                action=action, object_name=object_name,
                _csrf_retried=_csrf_retried, _attempt=_attempt + 1,
            )

        if not response.ok:
            raise self._translate_http_error(response, path)
        return response

    def _audit(
        self, action, method, path, object_name, status, elapsed,
        request_body, response_body, outcome="", detail="",
    ) -> None:
        self.audit.record(
            AuditRecord(
                profile_id=self.profile.profile_id,
                action=action,
                method=method,
                path=path,
                object=object_name,
                http_status=status,
                elapsed_ms=elapsed,
                request_hash=body_hash(request_body),
                response_hash=body_hash(response_body),
                ssl_verified=self.profile.verify_ssl,
                read_only=self.read_only,
                outcome=outcome,
                detail=detail,
                system_id=self.system_id,
                client=self.profile.client,
                user=self.profile.username,
            )
        )

    def _translate_transport_error(self, exc: Exception) -> AdtError:
        """Name the cause. A stack trace is not a diagnosis (F-01)."""
        name = type(exc).__name__
        text = str(exc).lower()

        if "ssl" in name.lower() or "certificate" in text:
            return TlsProblem(
                f"the TLS handshake with {self.profile.host} failed: {exc}",
                remedy="Self-signed certificates are the norm on dev systems. "
                "Point ca_bundle_path at the corporate root CA, or set "
                "verify_ssl: false knowingly — every audit record is then "
                "stamped ssl_verified=false.",
            )
        if "getaddrinfo" in text or "name or service not known" in text or "nodename" in text:
            return HostUnreachable(
                f"the hostname {self.profile.host} could not be resolved",
                remedy="Check DNS, the hosts file, or whether you need to be on "
                "the corporate VPN.",
            )
        if "timed out" in text or "timeout" in name.lower():
            return HostUnreachable(
                f"no response from {self.profile.host}:{self.profile.port} within "
                f"{self.profile.timeout_seconds}s",
                remedy="Check the port — ADT over HTTPS is usually 443<instance>, "
                "e.g. 44300 for instance 00 — and any firewall between you and it.",
            )
        if "refused" in text:
            return HostUnreachable(
                f"{self.profile.host}:{self.profile.port} refused the connection",
                remedy="The host is reachable but nothing is listening on that "
                "port. Check the ICM HTTPS port with transaction SMICM.",
            )
        return HostUnreachable(f"{name}: {exc}")

    def _translate_http_error(self, response: AdtResponse, path: str) -> AdtError:
        if response.status == 401:
            return AuthenticationFailed(
                f"the system rejected the credentials for user "
                f"{self.profile.username!r} in client {self.profile.client}",
                remedy="Check the user, password and client. Note that ADT "
                "supports Basic auth over HTTPS only — not form-based logon. "
                "Repeated failures will lock the user.",
                status=401,
            )
        if response.status == 403:
            return AuthorizationFailed(
                f"the user is authenticated but not authorised for {path}",
                remedy="Reading CDS sources needs S_DEVELOP with object type "
                "DDLS and activity 03.",
                status=403,
            )
        if response.status == 404 and path.rstrip("/") == ep.ADT_ROOT + "/discovery":
            return SicfNodeInactive(
                f"{ep.SICF_NODE} returned 404",
                remedy="The ADT SICF node is almost certainly inactive. Activate "
                f"{ep.SICF_NODE} and its subnodes in transaction SICF.",
                status=404,
            )
        if response.status == 404:
            return AdtHttpError(
                f"{path} returned 404",
                remedy="The object may not exist, or this endpoint path may "
                "differ on this release. ADT's wire contract is unpublished — "
                "check src/cdcforge/connect/endpoints.py against the target "
                "release.",
                status=404,
            )
        if response.status == 503:
            return SicfNodeInactive(
                f"{path} returned 503",
                remedy="The ICF service is unavailable — usually an inactive "
                "SICF node or a system that is still starting.",
                status=503,
            )
        return AdtHttpError(
            f"{path} returned HTTP {response.status}",
            remedy=(response.text or "")[:300].strip(),
            status=response.status,
        )
