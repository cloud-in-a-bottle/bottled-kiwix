import hashlib
import os
import shutil
import threading
import urllib.request
from collections.abc import Callable
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path

import attr
import pytest

TEST_ZIM_URL = "https://download.kiwix.org/zim/wikipedia/wikipedia_en_ray-charles_mini_2026-08.zim"
TEST_ZIM_SHA256 = "9aa68a83cf9a7d9449724b6c722f797e2a307e290a34b083df717028cd42330d"
TEST_ZIM_ID = "baef616d-c83f-1b8a-4331-1534faf9b910"
CACHE_DIR = Path(os.environ.get("KIWIX_TEST_CACHE", Path.home() / ".cache" / "bottled-kiwix-tests"))


BINARY_ENV = {"kiwix-manage": "KIWIX_MANAGE_BIN", "kiwix-serve": "KIWIX_SERVE_BIN"}


def _binary(name: str) -> str | None:
    configured = os.environ.get(BINARY_ENV[name])
    if configured and Path(configured).exists():
        return configured
    return shutil.which(name)


@pytest.fixture(scope="session")
def kiwix_manage_bin() -> str:
    binary = _binary("kiwix-manage")
    if binary is None:
        pytest.skip("kiwix-manage is not installed (set KIWIX_MANAGE_BIN)")
    return binary


@pytest.fixture(scope="session")
def kiwix_serve_bin() -> str:
    binary = _binary("kiwix-serve")
    if binary is None:
        pytest.skip("kiwix-serve is not installed (set KIWIX_SERVE_BIN)")
    return binary


@pytest.fixture(scope="session")
def test_zim() -> Path:
    """A real 760 KB ZIM file (Wikipedia articles about Ray Charles), downloaded once and cached."""
    path = CACHE_DIR / Path(TEST_ZIM_URL).name
    if not path.exists():
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            request = urllib.request.Request(TEST_ZIM_URL, headers={"User-Agent": "bottled-kiwix-tests"})
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read()
        except OSError as error:
            pytest.skip(f"could not download the test ZIM: {error}")
        assert hashlib.sha256(payload).hexdigest() == TEST_ZIM_SHA256
        path.write_bytes(payload)
    return path


@attr.s(auto_attribs=True)
class FileServerBehaviour:
    payload: bytes = b""
    # Status code to answer every request with instead of serving the payload.
    status_override: int | None = None
    ignore_range: bool = False
    # Close the connection after sending this many bytes of the body (once), to simulate a dropped connection.
    drop_after: int | None = None
    # Extra routes: exact path -> (content type, body).
    routes: dict[str, tuple[str, bytes]] = attr.Factory(dict)
    requests: list[tuple[str, str | None]] = attr.Factory(list)
    # Optional gate the server waits on before streaming each chunk, so tests can observe in-progress downloads.
    gate: threading.Event | None = None


@attr.s(auto_attribs=True, frozen=True)
class FileServer:
    url: str
    behaviour: FileServerBehaviour


def _make_handler(behaviour: FileServerBehaviour) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            range_header = self.headers.get("Range")
            behaviour.requests.append((self.path, range_header))
            # Routes can be keyed by the exact path and query, or by the path alone.
            path = self.path if self.path in behaviour.routes else self.path.split("?", 1)[0]
            if path in behaviour.routes:
                content_type, body = behaviour.routes[path]
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if behaviour.status_override is not None:
                self.send_response(behaviour.status_override)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            payload = behaviour.payload
            start = 0
            if range_header and not behaviour.ignore_range:
                start = int(range_header.removeprefix("bytes=").split("-", 1)[0])
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{len(payload) - 1}/{len(payload)}")
            else:
                self.send_response(200)
            body = payload[start:]
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            limit = behaviour.drop_after
            if limit is not None:
                behaviour.drop_after = None
                self.wfile.write(body[:limit])
                self.wfile.flush()
                self.close_connection = True
                return
            for offset in range(0, len(body), 64 * 1024):
                if behaviour.gate is not None:
                    behaviour.gate.wait(timeout=30)
                self.wfile.write(body[offset : offset + 64 * 1024])

    return Handler


@pytest.fixture
def make_file_server() -> Iterator[Callable[[], FileServer]]:
    servers: list[tuple[ThreadingHTTPServer, FileServerBehaviour]] = []

    def make() -> FileServer:
        behaviour = FileServerBehaviour()
        server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(behaviour))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append((server, behaviour))
        return FileServer(url=f"http://127.0.0.1:{server.server_address[1]}", behaviour=behaviour)

    try:
        yield make
    finally:
        for server, behaviour in servers:
            if behaviour.gate is not None:
                behaviour.gate.set()
            server.shutdown()
            server.server_close()


@pytest.fixture
def file_server(make_file_server: Callable[[], FileServer]) -> FileServer:
    return make_file_server()
