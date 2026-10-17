from collections.abc import AsyncIterator
from collections.abc import Callable
from urllib.parse import quote

import httpx
from loguru import logger

from kiwix_manager.asgi import Receive
from kiwix_manager.asgi import Scope
from kiwix_manager.asgi import Send
from kiwix_manager.auth import is_owner
from kiwix_manager.auth import login_url

HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
    }
)
MANAGE_PATH = "/_manage/"


def _decode_headers(scope: Scope) -> dict[str, str]:
    return {name.decode("latin-1").lower(): value.decode("latin-1") for name, value in scope["headers"]}


async def _send_simple(
    send: Send, status: int, body: str, extra_headers: list[tuple[bytes, bytes]] | None = None
) -> None:
    payload = body.encode()
    headers = [
        (b"content-type", b"text/html; charset=utf-8"),
        (b"content-length", str(len(payload)).encode()),
        (b"cache-control", b"no-store"),
        *(extra_headers or []),
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": payload})


def _page(title: str, message: str) -> str:
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{title}</title><link rel='stylesheet' href='/_manage/static/manage.css'></head>"
        f"<body><main class='notice'><h1>{title}</h1><p>{message}</p></main></body></html>"
    )


class ReaderProxy:
    """Forwards reader traffic to kiwix-serve, enforcing the owner-only / public setting."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        upstream: str,
        is_public: Callable[[], bool],
        is_running: Callable[[], bool],
        library_is_empty: Callable[[], bool],
    ) -> None:
        self._client = client
        self._upstream = upstream
        self._is_public = is_public
        self._is_running = is_running
        self._library_is_empty = library_is_empty

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await send({"type": "websocket.close", "code": 1003})
            return
        headers = _decode_headers(scope)
        method: str = scope["method"]
        path: str = scope["path"]
        query = scope["query_string"].decode("latin-1")
        owner = is_owner(headers)
        if not owner and not self._is_public():
            if method in ("GET", "HEAD"):
                location = login_url(headers, path, query)
                await _send_simple(send, 302, "", [(b"location", location.encode())])
            else:
                await _send_simple(send, 401, _page("Login required", "This library is private."))
            return
        if path == "/" and self._library_is_empty():
            if owner:
                await _send_simple(send, 302, "", [(b"location", MANAGE_PATH.encode())])
            else:
                await _send_simple(send, 200, _page("Kiwix", "This library has no content yet."))
            return
        if not self._is_running():
            await _send_simple(send, 503, _page("Starting", "The library server is starting. Try again shortly."))
            return
        await self._forward(scope, receive, send, headers, method, path, query)

    async def _forward(
        self, scope: Scope, receive: Receive, send: Send, headers: dict[str, str], method: str, path: str, query: str
    ) -> None:
        raw_path: bytes = scope.get("raw_path") or quote(path).encode("ascii")
        url = self._upstream + raw_path.decode("latin-1")
        if query:
            url = f"{url}?{query}"
        forward_headers = [
            (name, value)
            for name, value in headers.items()
            if name not in HOP_BY_HOP and not name.startswith("x-openhost-")
        ]
        request = self._client.build_request(
            method, url, headers=forward_headers, content=_body(receive) if method not in ("GET", "HEAD") else None
        )
        try:
            response = await self._client.send(request, stream=True)
        except httpx.HTTPError as error:
            logger.warning("kiwix-serve request failed: {}", error)
            await _send_simple(send, 502, _page("Unavailable", "The library server did not respond."))
            return
        try:
            response_headers = [
                (name, value)
                for name, value in response.headers.raw
                if name.decode("latin-1").lower() not in HOP_BY_HOP
            ]
            await send({"type": "http.response.start", "status": response.status_code, "headers": response_headers})
            async for chunk in response.aiter_raw():
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b""})
        finally:
            await response.aclose()


async def _body(receive: Receive) -> AsyncIterator[bytes]:
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return
        body: bytes = message.get("body", b"")
        if body:
            yield body
        if not message.get("more_body", False):
            return
