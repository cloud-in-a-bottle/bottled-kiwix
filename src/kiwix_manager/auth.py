from collections.abc import Mapping
from urllib.parse import quote

OWNER_HEADER = "x-openhost-is-owner"


def is_owner(headers: Mapping[str, str]) -> bool:
    # The router strips any client-supplied X-OpenHost-* header and sets this one only for a valid owner session.
    return headers.get(OWNER_HEADER) == "true"


def _proto(headers: Mapping[str, str]) -> str:
    proto = headers.get("x-forwarded-proto", "https")
    return proto if proto in ("http", "https") else "https"


def _host(headers: Mapping[str, str]) -> str:
    return headers.get("x-forwarded-host") or headers.get("host", "")


def own_origin(headers: Mapping[str, str]) -> str:
    return f"{_proto(headers)}://{_host(headers)}"


def login_url(headers: Mapping[str, str], path: str, query: str) -> str:
    """The router's login page on the zone this request arrived on, returning to the requested URL afterwards.

    Apps live on ``<app>.<zone>``, so the zone is the request host minus its first label.
    """
    proto = _proto(headers)
    host = _host(headers)
    zone = host.split(".", 1)[1] if "." in host else host
    next_url = f"{proto}://{host}{path}"
    if query:
        next_url = f"{next_url}?{query}"
    return f"{proto}://{zone}/login?next={quote(next_url, safe='')}"


def is_same_origin_request(headers: Mapping[str, str]) -> bool:
    """CSRF protection for state-changing requests.

    The owner's session cookie is scoped to the whole zone, so other apps' subdomains are same-site. Requiring the exact
    Origin of this app (or a same-origin Sec-Fetch-Site) keeps them, and every other site, out.
    """
    origin = headers.get("origin")
    if origin is not None:
        return origin == own_origin(headers)
    return headers.get("sec-fetch-site") == "same-origin"
