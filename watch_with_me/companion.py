"""LAN companion URL discovery, shared by the dashboard API and chat.

One resolution rule everywhere the plugin hands someone a link to the
companion page: the explicit ``public_base_url`` setting wins; otherwise the
primary LAN IPv4 is combined with the request's scheme/port (or the plugin
default ``localhost:9119`` when there is no request, e.g. a chat
notification composed on a worker thread).
"""

from __future__ import annotations

import socket
from typing import Any

APP_PATH = "/api/plugins/watch-with-me/app"

#: Default when nothing else is known: the documented standalone fallback
#: (RESEARCH.md §2.3 hard-coded this port; the setting exists to override it).
DEFAULT_PORT = "9119"


def lan_ipv4() -> str | None:
    """The primary LAN IPv4, via a routing-table lookup only.

    A UDP ``connect`` to the documentation prefix sends no packets; it just
    asks the kernel which local address the default route would use. Any
    failure (no route, sandboxed CI) means "unknown", never an exception.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 80))
            host = sock.getsockname()[0]
        return host or None
    except OSError:
        return None


def companion_url(request: Any = None, settings: Any = None) -> str:
    """Resolved URL a phone on the LAN can open to reach the companion page.

    ``settings.public_base_url`` (when set) is used verbatim; the request's
    scheme and explicit port are honoured next; the fallbacks are the
    detected LAN IPv4, then ``localhost``.
    """
    base = (getattr(settings, "public_base_url", "") or "").strip()
    if base.startswith(("http://", "https://")):
        return base.rstrip("/") + APP_PATH
    # Anything else (a bare host, a typo'd scheme) is ignored: a bad override
    # must not produce dead links, so resolution falls through to LAN
    # discovery exactly as when the setting is empty.
    scheme, port = "http", DEFAULT_PORT
    if request is not None:
        url = getattr(request, "url", None)
        req_scheme = (getattr(url, "scheme", "") or "http").lower()
        req_port = getattr(url, "port", None)
        scheme = req_scheme
        port = str(req_port or (443 if scheme == "https" else 80))
    host = lan_ipv4() or "localhost"
    return f"{scheme}://{host}:{port}{APP_PATH}"
