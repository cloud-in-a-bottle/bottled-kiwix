import asyncio
import os
import shutil
import socket
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import attr
import httpx
import pytest

from tests.conftest import TEST_ZIM_ID
from tests.conftest import TEST_ZIM_SHA256
from tests.conftest import FileServer

HOST = "kiwix.example.test"
OWNER = {"X-OpenHost-Is-Owner": "true", "X-Forwarded-Host": HOST, "X-Forwarded-Proto": "https"}
OWNER_WRITE = {**OWNER, "Origin": f"https://{HOST}"}
ANONYMOUS = {"X-Forwarded-Host": HOST, "X-Forwarded-Proto": "https"}
OTHER_ID = "11111111-2222-3333-4444-555555555555"


def _entry(book_id: str, meta4_url: str, size: int) -> str:
    return f"""<entry>
    <id>urn:uuid:{book_id}</id>
    <title>Ray Charles</title>
    <summary>Wikipedia articles about Ray Charles</summary>
    <language>eng</language>
    <name>wikipedia_en_ray-charles</name>
    <flavour>mini</flavour>
    <category>wikipedia</category>
    <articleCount>340</articleCount>
    <mediaCount>21</mediaCount>
    <dc:issued>2026-08-02T00:00:00Z</dc:issued>
    <link rel="http://opds-spec.org/acquisition/open-access" type="application/x-zim" href="{meta4_url}"
          length="{size}" />
  </entry>"""


@attr.s(auto_attribs=True, frozen=True)
class Stack:
    client: httpx.AsyncClient
    zim_dir: Path


@pytest.fixture
async def stack(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    file_server: FileServer,
    test_zim: Path,
    kiwix_serve_bin: str,
    kiwix_manage_bin: str,
) -> AsyncIterator[Stack]:
    reserve_bytes = getattr(request, "param", 0)
    payload = test_zim.read_bytes()
    file_server.behaviour.payload = payload
    meta4_url = f"{file_server.url}/zim/{test_zim.name}.meta4"
    rogue_meta4_url = "https://evil.example/zim/x.zim.meta4"
    entry = _entry(TEST_ZIM_ID, meta4_url, len(payload))
    feed = (
        '<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom" '
        'xmlns:dc="http://purl.org/dc/terms/"><totalResults>1</totalResults><startIndex>0</startIndex>'
        f"{entry}</feed>"
    )
    routes = file_server.behaviour.routes
    routes["/catalog/v2/entries"] = ("application/atom+xml", feed.encode())
    # One preset ("Wikipedia: top 100") resolves to the test book; the rest are missing from this fake catalog.
    preset_entry = entry.replace("<name>wikipedia_en_ray-charles</name>", "<name>wikipedia_en_100</name>").replace(
        "<flavour>mini</flavour>", "<flavour></flavour>"
    )
    routes["/catalog/v2/entries?name=wikipedia_en_100&count=50"] = (
        "application/atom+xml",
        feed.replace(entry, preset_entry).encode(),
    )
    # The real single-entry endpoint omits its namespace declarations; reproduce that.
    routes[f"/catalog/v2/entry/{TEST_ZIM_ID}"] = ("application/atom+xml", f'<?xml version="1.0"?>\n{entry}'.encode())
    routes[f"/catalog/v2/entry/{OTHER_ID}"] = (
        "application/atom+xml",
        _entry(OTHER_ID, rogue_meta4_url, 10).replace("<dc:issued>2026-08-02T00:00:00Z</dc:issued>", "").encode(),
    )
    routes[f"/zim/{test_zim.name}.meta4"] = (
        "application/metalink4+xml",
        f"""<?xml version="1.0"?><metalink xmlns="urn:ietf:params:xml:ns:metalink">
        <file name="{test_zim.name}"><size>{len(payload)}</size>
        <hash type="sha-256">{TEST_ZIM_SHA256}</hash>
        <url priority="1">{file_server.url}/mirror/{test_zim.name}</url></file></metalink>""".encode(),
    )
    port = _free_port()
    env = {
        **os.environ,
        "BOTTLE_APP_DATA_DIR": str(tmp_path / "data"),
        "BOTTLE_APP_ARCHIVE_DIR": str(tmp_path / "archive"),
        "KIWIX_SERVE_BIN": kiwix_serve_bin,
        "KIWIX_MANAGE_BIN": kiwix_manage_bin,
        "KIWIX_SERVE_PORT": str(_free_port()),
        "KIWIX_CATALOG_URL": file_server.url,
        "KIWIX_META4_URL_PREFIXES": f"{file_server.url}/zim/",
        "KIWIX_ALLOW_INSECURE_MIRRORS": "1",
        "KIWIX_RESERVE_BYTES": str(reserve_bytes),
    }
    log = (tmp_path / "server.log").open("w")
    # Run the real server process, as the container does.
    process = subprocess.Popen(
        [sys.executable, "-m", "hypercorn", "kiwix_manager.app:app", "--workers", "0", "--bind", f"127.0.0.1:{port}"],
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            await _wait_until_healthy(client, process)
            yield Stack(client=client, zim_dir=tmp_path / "archive" / "zims")
    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
        log.close()
        print((tmp_path / "server.log").read_text())


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


async def _wait_until_healthy(client: httpx.AsyncClient, process: subprocess.Popen[bytes]) -> None:
    for _ in range(200):
        assert process.poll() is None, "server exited during startup"
        try:
            healthy = (await client.get("/_manage/healthz")).status_code == 200
        except httpx.TransportError:
            healthy = False
        if healthy:
            # kiwix-serve's process is up; give it a moment to bind its port.
            for _ in range(100):
                try:
                    response = await client.get("/catalog/v2/entries", headers=OWNER)
                except httpx.HTTPError:
                    response = None
                if response is not None and response.status_code == 200:
                    return
                await asyncio.sleep(0.1)
        await asyncio.sleep(0.1)
    raise AssertionError("kiwix-serve did not become healthy")


async def _wait_for_book(client: httpx.AsyncClient, present: bool) -> dict[str, object]:
    for _ in range(200):
        state: dict[str, object] = (await client.get("/_manage/api/state", headers=OWNER)).json()
        books = state["books"]
        assert isinstance(books, list)
        if bool(books) == present:
            return state
        await asyncio.sleep(0.1)
    raise AssertionError(f"book presence never became {present}: {state}")


async def _get_reader(client: httpx.AsyncClient, path: str, headers: dict[str, str]) -> httpx.Response:
    # No retries: once the manager reports a library change, kiwix-serve must already reflect it.
    return await client.get(path, headers=headers, follow_redirects=True)


async def test_manager_requires_owner(stack: Stack) -> None:
    client = stack.client
    page = await client.get("/_manage/", headers=ANONYMOUS)
    assert page.status_code == 302
    assert page.headers["location"].startswith("https://example.test/login?next=")
    assert (await client.get("/_manage/api/state", headers=ANONYMOUS)).status_code == 401
    assert (await client.post("/_manage/api/settings", json={"public": True}, headers=ANONYMOUS)).status_code == 401
    owner_page = await client.get("/_manage/", headers=OWNER)
    assert owner_page.status_code == 200
    assert "Kiwix library" in owner_page.text
    assert "frame-ancestors 'none'" in owner_page.headers["content-security-policy"]


async def test_state_changes_require_same_origin(stack: Stack) -> None:
    client = stack.client
    for headers in (OWNER, {**OWNER, "Origin": "https://other.example.test"}):
        response = await client.post("/_manage/api/settings", json={"public": True}, headers=headers)
        assert response.status_code == 403
    state = (await client.get("/_manage/api/state", headers=OWNER)).json()
    assert state["public"] is False


async def test_empty_library_redirects_owner_to_manager(stack: Stack) -> None:
    client = stack.client
    response = await client.get("/", headers=OWNER)
    assert response.status_code == 302
    assert response.headers["location"] == "/_manage/"
    anonymous = await client.get("/", headers=ANONYMOUS)
    assert anonymous.status_code == 302
    assert anonymous.headers["location"].startswith("https://example.test/login")


async def test_download_read_share_and_delete(stack: Stack, test_zim: Path) -> None:
    client = stack.client
    catalog = (await client.get("/_manage/api/catalog?q=ray", headers=OWNER)).json()
    assert [view["status"] for view in catalog["entries"]] == ["available"]

    started = await client.post("/_manage/api/downloads", json={"book_id": TEST_ZIM_ID}, headers=OWNER_WRITE)
    assert started.status_code == 201, started.text
    state = await _wait_for_book(client, present=True)
    books = state["books"]
    assert isinstance(books, list)
    assert books[0]["id"] == TEST_ZIM_ID
    assert (stack.zim_dir / test_zim.name).exists()

    catalog = (await client.get("/_manage/api/catalog?q=ray", headers=OWNER)).json()
    assert [view["status"] for view in catalog["entries"]] == ["installed"]
    again = await client.post("/_manage/api/downloads", json={"book_id": TEST_ZIM_ID}, headers=OWNER_WRITE)
    assert again.status_code == 400

    article = await _get_reader(client, f"/content/{test_zim.stem}", OWNER)
    assert article.status_code == 200
    assert "Ray Charles" in article.text
    alias = await _get_reader(client, "/content/wikipedia_en_ray-charles_mini", OWNER)
    assert alias.status_code == 200

    # Private by default: anonymous readers are sent to the login page.
    anonymous = await client.get(f"/content/{test_zim.stem}", headers=ANONYMOUS)
    assert anonymous.status_code == 302
    assert "/login?next=" in anonymous.headers["location"]

    shared = await client.post("/_manage/api/settings", json={"public": True}, headers=OWNER_WRITE)
    assert shared.status_code == 201
    public_article = await _get_reader(client, f"/content/{test_zim.stem}", ANONYMOUS)
    assert public_article.status_code == 200
    assert (await client.get("/_manage/api/state", headers=ANONYMOUS)).status_code == 401
    search = await client.get(f"/search?content={test_zim.stem}&pattern=piano", headers=ANONYMOUS)
    assert search.status_code == 200

    deleted = await client.delete(f"/_manage/api/books/{TEST_ZIM_ID}", headers=OWNER_WRITE)
    assert deleted.status_code == 204
    state = (await client.get("/_manage/api/state", headers=OWNER)).json()
    assert state["books"] == []
    assert not (stack.zim_dir / test_zim.name).exists()
    gone = await client.get(f"/content/{test_zim.stem}", headers=OWNER, follow_redirects=True)
    assert gone.status_code == 404


async def test_rejects_unknown_and_untrusted_downloads(stack: Stack) -> None:
    client = stack.client
    unknown = await client.post(
        "/_manage/api/downloads", json={"book_id": "99999999-9999-9999-9999-999999999999"}, headers=OWNER_WRITE
    )
    assert unknown.status_code == 400
    rogue = await client.post("/_manage/api/downloads", json={"book_id": OTHER_ID}, headers=OWNER_WRITE)
    assert rogue.status_code == 400
    assert "unexpected location" in rogue.json()["detail"]
    malformed = await client.post("/_manage/api/downloads", json={"book_id": "../../etc"}, headers=OWNER_WRITE)
    assert malformed.status_code == 400


async def test_rescan_picks_up_dropped_in_files(stack: Stack, test_zim: Path) -> None:
    client = stack.client
    shutil.copy(test_zim, stack.zim_dir / "dropped_in.zim")
    response = await client.post("/_manage/api/rescan", headers=OWNER_WRITE)
    assert response.status_code == 201
    assert [book["filename"] for book in response.json()["books"]] == ["dropped_in.zim"]
    article = await _get_reader(client, "/content/dropped_in", OWNER)
    assert article.status_code == 200


async def test_retry_and_dismiss_downloads(stack: Stack, file_server: FileServer) -> None:
    client = stack.client
    file_server.behaviour.status_override = 404  # every mirror request fails permanently
    started = await client.post("/_manage/api/downloads", json={"book_id": TEST_ZIM_ID}, headers=OWNER_WRITE)
    assert started.status_code == 201
    job_id = started.json()["id"]
    job = await _wait_for_job(client, job_id, {"failed"})
    assert "HTTP 404" in job["error"]

    file_server.behaviour.status_override = None
    retried = await client.post(f"/_manage/api/downloads/{job_id}/retry", headers=OWNER_WRITE)
    assert retried.status_code == 201
    await _wait_for_job(client, job_id, {"completed"})
    assert (await client.delete(f"/_manage/api/downloads/{job_id}", headers=OWNER_WRITE)).status_code == 204
    state = (await client.get("/_manage/api/state", headers=OWNER)).json()
    assert state["downloads"] == []
    assert [book["id"] for book in state["books"]] == [TEST_ZIM_ID]
    missing = await client.post("/_manage/api/downloads/nope/retry", headers=OWNER_WRITE)
    assert missing.status_code == 400


# A reserve larger than any disk, so every download looks like it will not fit.
@pytest.mark.parametrize("stack", [10**18], indirect=True)
async def test_insufficient_space_can_be_overridden(stack: Stack) -> None:
    client = stack.client
    refused = await client.post("/_manage/api/downloads", json={"book_id": TEST_ZIM_ID}, headers=OWNER_WRITE)
    assert refused.status_code == 409
    assert refused.json()["extra"]["code"] == "insufficient_space"
    assert (await client.get("/_manage/api/state", headers=OWNER)).json()["downloads"] == []
    forced = await client.post(
        "/_manage/api/downloads", json={"book_id": TEST_ZIM_ID, "ignore_space_check": True}, headers=OWNER_WRITE
    )
    assert forced.status_code == 201
    # The download itself then stops at once, because the disk is below the reserve.
    job = await _wait_for_job(client, forced.json()["id"], {"failed", "completed"})
    assert job["status"] == "failed"
    assert "disk is nearly full" in str(job["error"])


async def _wait_for_job(client: httpx.AsyncClient, job_id: str, statuses: set[str]) -> dict[str, object]:
    for _ in range(300):
        state = (await client.get("/_manage/api/state", headers=OWNER)).json()
        job = next(job for job in state["downloads"] if job["id"] == job_id)
        if job["status"] in statuses:
            return job
        await asyncio.sleep(0.1)
    raise AssertionError(f"job never reached {statuses}: {job}")


async def test_preset_buttons(stack: Stack) -> None:
    client = stack.client
    presets = (await client.get("/_manage/api/presets", headers=OWNER)).json()
    by_key = {preset["key"]: preset for preset in presets}
    assert by_key["wikipedia-top"]["entry"]["id"] == TEST_ZIM_ID
    assert by_key["wikipedia-top"]["status"] == "available"
    # Books the catalog does not list are reported as unavailable rather than failing the whole list.
    assert by_key["wikipedia-maxi"]["entry"] is None
    assert (await client.get("/_manage/api/presets", headers=ANONYMOUS)).status_code == 401

    started = await client.post("/_manage/api/downloads", json={"book_id": TEST_ZIM_ID}, headers=OWNER_WRITE)
    assert started.status_code == 201
    await _wait_for_job(client, started.json()["id"], {"completed"})
    presets = (await client.get("/_manage/api/presets", headers=OWNER)).json()
    assert {preset["key"]: preset["status"] for preset in presets}["wikipedia-top"] == "installed"
