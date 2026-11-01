"""Outbound webhook calls with an SSRF guard.

A webhook URL comes from the user, but the request is sent from inside our network
(ECS task or Lambda). Without checks, ``http://169.254.169.254/...`` would read the
instance metadata service and ``http://10.0.0.5/admin`` would reach internal services.

Rules:

- ``https`` only (``http`` only when ``RWS_WEBHOOK_ALLOW_HTTP=true``, for local tests).
- Every address the host resolves to must be public: no private, loopback,
  link-local (incl. 169.254.169.254), multicast, reserved or unspecified ranges, in
  IPv4 or IPv6 (including IPv4-mapped IPv6).
- The check runs when the flow is created (fast feedback) and again right before
  sending. The connection goes to the IP that was checked, so a DNS answer that
  changes between check and connect (DNS rebinding) cannot redirect it.
- Redirects are not followed, and the timeout is short.
"""

from __future__ import annotations

import http.client
import ipaddress
import os
import socket
import ssl
from urllib.parse import SplitResult, urlsplit

from rws.tasks.errors import TaskError

TIMEOUT_SECONDS = 5


class UnsafeWebhookURL(TaskError, ValueError):
    """The URL is not allowed. A ValueError too, so Pydantic reports it as a field error."""


def http_allowed() -> bool:
    return os.environ.get("RWS_WEBHOOK_ALLOW_HTTP", "").lower() in {"1", "true"}


def is_public_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped  # ::ffff:127.0.0.1 is 127.0.0.1
    return ip.is_global and not ip.is_multicast


def check_url(url: str) -> SplitResult:
    """Static checks (no DNS): scheme, host, and IP literals must be public."""
    parts = urlsplit(url)
    allowed = {"https", "http"} if http_allowed() else {"https"}
    if parts.scheme not in allowed:
        raise UnsafeWebhookURL(f"webhook_url must use {' or '.join(sorted(allowed))}")
    if not parts.hostname or parts.username or parts.password:
        raise UnsafeWebhookURL("webhook_url needs a host and no credentials")
    try:
        literal = ipaddress.ip_address(parts.hostname)
    except ValueError:
        if parts.hostname == "localhost" or parts.hostname.endswith(".localhost"):
            raise UnsafeWebhookURL("webhook_url must not point to localhost") from None
        return parts
    if not is_public_ip(str(literal)):
        raise UnsafeWebhookURL(f"webhook_url points to a non-public address: {literal}")
    return parts


def resolve_public_ip(host: str, port: int) -> str:
    """Resolve ``host`` and return an address, only if *every* address is public."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise TaskError(f"webhook host does not resolve: {host}") from exc
    addresses = [str(info[4][0]) for info in infos]
    private = [a for a in addresses if not is_public_ip(a)]
    if private:
        raise UnsafeWebhookURL(f"webhook host resolves to a non-public address: {private[0]}")
    return addresses[0]


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """Connects to an already-checked IP instead of resolving the host again."""

    def __init__(self, host: str, port: int, ip: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._ip = ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Same, for TLS: the certificate is still verified against the host name (SNI)."""

    def __init__(self, host: str, port: int, ip: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip

    def connect(self) -> None:
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)  # type: ignore[attr-defined]


def post_json(url: str, body: bytes, headers: dict[str, str]) -> int:
    """POST ``body`` to a checked webhook URL and return the HTTP status.

    ``http.client`` never follows redirects; a 3xx comes back as its status code.
    Network errors propagate as ``OSError`` (including ``TimeoutError``).
    """
    parts = check_url(url)
    assert parts.hostname  # checked above
    port = parts.port or (443 if parts.scheme == "https" else 80)
    ip = resolve_public_ip(parts.hostname, port)
    connection_class = _PinnedHTTPSConnection if parts.scheme == "https" else _PinnedHTTPConnection
    conn = connection_class(parts.hostname, port, ip, TIMEOUT_SECONDS)
    path = parts.path or "/"
    if parts.query:
        path += f"?{parts.query}"
    try:
        conn.request("POST", path, body=body, headers=headers)
        return conn.getresponse().status
    finally:
        conn.close()
