"""The address a request came from, as seen past the deployment's own proxies.

``REMOTE_ADDR`` is the last hop. Behind a reverse proxy (or a server-side
storefront calling the API for a shopper) that is the proxy, so anything keyed
on it — a throttle bucket — lumps every client together. ``X-Forwarded-For``
names the real client, but any client can send one, so it is believed only as
far as hops the deployment trusts.

``settings.TRUSTED_PROXIES`` lists those hops (IPs or CIDRs; env
``TRUSTED_PROXIES``, comma-separated; default empty = trust nobody, use
``REMOTE_ADDR``). When ``REMOTE_ADDR`` is trusted, ``X-Forwarded-For`` is
walked right to left past trusted entries; the first untrusted entry is the
client. A spoofed left-hand prefix is therefore never reached while a real
untrusted hop sits to its right.

Used by ``CouponValidateThrottle``, the OIDC start throttle and the login
throttle (F-087).
"""

from __future__ import annotations

import ipaddress
from functools import lru_cache

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


@lru_cache(maxsize=8)
def _networks(entries: tuple) -> tuple:
    networks = []
    for entry in entries:
        try:
            networks.append(ipaddress.ip_network(str(entry).strip(), strict=False))
        except ValueError as exc:
            raise ImproperlyConfigured(
                f"TRUSTED_PROXIES entry {entry!r} is not an IP address or CIDR."
            ) from exc
    return tuple(networks)


def _trusted(address: str, networks: tuple) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in network for network in networks)


def client_ip(request) -> str:
    """The client's IP address (``""`` when the request carries none)."""
    remote = request.META.get("REMOTE_ADDR", "") or ""
    entries = tuple(e for e in (getattr(settings, "TRUSTED_PROXIES", None) or ()) if str(e).strip())
    networks = _networks(entries)
    if not networks or not _trusted(remote, networks):
        return remote
    hops = [h.strip() for h in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",") if h.strip()]
    for hop in reversed(hops):
        try:
            ipaddress.ip_address(hop)
        except ValueError:
            # A malformed hop: whatever lies left of it is unverifiable.
            return remote
        if not _trusted(hop, networks):
            return hop
    # Every hop is ours (or there is no header): the earliest one we saw.
    return hops[0] if hops else remote
