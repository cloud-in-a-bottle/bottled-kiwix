import hashlib
import os
import threading
import time
from collections.abc import Callable
from collections.abc import Iterator
from pathlib import Path

import pytest

import kiwix_manager.downloads as downloads_module
from kiwix_manager.downloads import DownloadError
from kiwix_manager.downloads import DownloadManager
from kiwix_manager.downloads import InsufficientSpaceError
from kiwix_manager.downloads import Job
from kiwix_manager.downloads import JobStatus
from tests.conftest import FileServer

PAYLOAD = os.urandom(3 * 1024 * 1024 + 123)
SHA256 = hashlib.sha256(PAYLOAD).hexdigest()
FILENAME = "test_book_2026-01.zim"
BOOK_ID = "00000000-0000-0000-0000-000000000001"
PLENTY = 10**15


class Harness:
    def __init__(self, root: Path, space: Callable[[Path], int] = lambda _: PLENTY, reserve: int = 0) -> None:
        self.root = root
        self.completed = threading.Event()
        self.space = space
        self.reserve = reserve
        self.manager = self._new_manager()

    def _new_manager(self) -> DownloadManager:
        return DownloadManager(
            state_path=self.root / "downloads.json",
            partial_dir=self.root / "partial",
            zim_dir=self.root / "zims",
            reserve_bytes=self.reserve,
            local_dir=self.root,
            on_completed=self.completed.set,
            space_probe=lambda path: self.space(path),
        )

    def restart(self) -> None:
        self.manager.stop()
        self.manager = self._new_manager()
        self.manager.start()

    def enqueue(
        self, urls: tuple[str, ...], sha256: str = SHA256, size: int = len(PAYLOAD), ignore_space_check: bool = False
    ) -> Job:
        return self.manager.enqueue(
            book_id=BOOK_ID,
            title="Test book",
            filename=FILENAME,
            size_bytes=size,
            sha256=sha256,
            urls=urls,
            ignore_space_check=ignore_space_check,
        )

    def wait_for(self, job_id: str, statuses: set[JobStatus], timeout: float = 30) -> Job:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = next(job for job in self.manager.jobs() if job.id == job_id)
            if job.status in statuses:
                return job
            time.sleep(0.05)
        raise AssertionError(f"job did not reach {statuses}; last status {job.status} {job.error}")


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(downloads_module, "RETRY_DELAY_SECONDS", 0.05)


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    instance = Harness(tmp_path)
    instance.manager.start()
    yield instance
    instance.manager.stop()


def test_download_verifies_and_installs(harness: Harness, file_server: FileServer) -> None:
    file_server.behaviour.payload = PAYLOAD
    job = harness.enqueue((f"{file_server.url}/{FILENAME}",))
    done = harness.wait_for(job.id, {JobStatus.COMPLETED, JobStatus.FAILED})
    assert done.status == JobStatus.COMPLETED, done.error
    assert (harness.root / "zims" / FILENAME).read_bytes() == PAYLOAD
    assert not list((harness.root / "partial").iterdir())
    assert harness.completed.is_set()
    assert done.bytes_done == len(PAYLOAD)


def test_checksum_mismatch_fails_and_discards(harness: Harness, file_server: FileServer) -> None:
    file_server.behaviour.payload = PAYLOAD
    job = harness.enqueue((f"{file_server.url}/{FILENAME}",), sha256="0" * 64)
    done = harness.wait_for(job.id, {JobStatus.COMPLETED, JobStatus.FAILED})
    assert done.status == JobStatus.FAILED
    assert "Checksum mismatch" in done.error
    assert not (harness.root / "zims" / FILENAME).exists()
    assert not (harness.root / "partial" / f"{FILENAME}.part").exists()


def test_falls_back_to_next_mirror_on_client_error(harness: Harness, file_server: FileServer) -> None:
    file_server.behaviour.payload = PAYLOAD
    job = harness.enqueue(("http://127.0.0.1:9/unreachable", f"{file_server.url}/{FILENAME}"))
    done = harness.wait_for(job.id, {JobStatus.COMPLETED, JobStatus.FAILED}, timeout=60)
    assert done.status == JobStatus.COMPLETED, done.error


def test_all_mirrors_rejecting_fails_quickly(harness: Harness, file_server: FileServer) -> None:
    file_server.behaviour.status_override = 403
    started = time.monotonic()
    job = harness.enqueue((f"{file_server.url}/a/{FILENAME}", f"{file_server.url}/b/{FILENAME}"))
    done = harness.wait_for(job.id, {JobStatus.COMPLETED, JobStatus.FAILED})
    assert done.status == JobStatus.FAILED
    assert "HTTP 403" in done.error
    # A 403 is permanent, so each mirror is tried once rather than retried with backoff.
    assert time.monotonic() - started < 4
    assert len(file_server.behaviour.requests) == 2


def test_resumes_after_dropped_connection(harness: Harness, file_server: FileServer) -> None:
    file_server.behaviour.payload = PAYLOAD
    file_server.behaviour.drop_after = 1024 * 1024
    job = harness.enqueue((f"{file_server.url}/{FILENAME}",))
    done = harness.wait_for(job.id, {JobStatus.COMPLETED, JobStatus.FAILED}, timeout=60)
    assert done.status == JobStatus.COMPLETED, done.error
    ranges = [requested for _, requested in file_server.behaviour.requests]
    assert ranges[0] is None
    assert ranges[1] == "bytes=1048576-"
    assert (harness.root / "zims" / FILENAME).read_bytes() == PAYLOAD


def test_restarts_when_mirror_ignores_range(harness: Harness, file_server: FileServer) -> None:
    file_server.behaviour.payload = PAYLOAD
    file_server.behaviour.drop_after = 1024 * 1024
    file_server.behaviour.ignore_range = True
    job = harness.enqueue((f"{file_server.url}/{FILENAME}",))
    done = harness.wait_for(job.id, {JobStatus.COMPLETED, JobStatus.FAILED}, timeout=60)
    assert done.status == JobStatus.COMPLETED, done.error
    assert (harness.root / "zims" / FILENAME).read_bytes() == PAYLOAD


def test_resumes_partial_file_after_restart(tmp_path: Path, file_server: FileServer) -> None:
    file_server.behaviour.payload = PAYLOAD
    harness = Harness(tmp_path)
    harness.manager.start()
    gate = threading.Event()
    file_server.behaviour.gate = gate
    try:
        job = harness.enqueue((f"{file_server.url}/{FILENAME}",))
        harness.wait_for(job.id, {JobStatus.DOWNLOADING})
        # Simulate a crash mid-download: write a valid prefix as the partial file and restart the manager.
        gate.set()
        harness.manager.stop()
        file_server.behaviour.gate = None
        (tmp_path / "partial" / f"{FILENAME}.part").write_bytes(PAYLOAD[:2_000_000])
        harness.manager = harness._new_manager()
        harness.manager.start()
        done = harness.wait_for(job.id, {JobStatus.COMPLETED, JobStatus.FAILED}, timeout=60)
        assert done.status == JobStatus.COMPLETED, done.error
        assert ("/" + FILENAME, "bytes=2000000-") in file_server.behaviour.requests
        assert (tmp_path / "zims" / FILENAME).read_bytes() == PAYLOAD
    finally:
        harness.manager.stop()


def test_cancel_removes_partial(harness: Harness, file_server: FileServer) -> None:
    file_server.behaviour.payload = PAYLOAD
    gate = threading.Event()
    file_server.behaviour.gate = gate
    job = harness.enqueue((f"{file_server.url}/{FILENAME}",))
    harness.wait_for(job.id, {JobStatus.DOWNLOADING})
    harness.manager.cancel(job.id)
    gate.set()
    harness.wait_for(job.id, {JobStatus.CANCELLED})
    deadline = time.monotonic() + 10
    while (harness.root / "partial" / f"{FILENAME}.part").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not (harness.root / "partial" / f"{FILENAME}.part").exists()
    assert not (harness.root / "zims" / FILENAME).exists()
    # A cancelled download can be retried from scratch.
    harness.manager.retry(job.id)
    done = harness.wait_for(job.id, {JobStatus.COMPLETED, JobStatus.FAILED})
    assert done.status == JobStatus.COMPLETED, done.error


def test_rejects_duplicates_and_installed(harness: Harness, file_server: FileServer) -> None:
    file_server.behaviour.payload = PAYLOAD
    gate = threading.Event()
    file_server.behaviour.gate = gate
    job = harness.enqueue((f"{file_server.url}/{FILENAME}",))
    with pytest.raises(DownloadError, match="already being downloaded"):
        harness.enqueue((f"{file_server.url}/{FILENAME}",))
    gate.set()
    harness.wait_for(job.id, {JobStatus.COMPLETED})
    with pytest.raises(DownloadError, match="already in the library"):
        harness.enqueue((f"{file_server.url}/{FILENAME}",))


def test_refuses_when_space_is_short(tmp_path: Path) -> None:
    harness = Harness(tmp_path, space=lambda _: len(PAYLOAD) + 100, reserve=1000)
    harness.manager.start()
    try:
        with pytest.raises(InsufficientSpaceError):
            harness.enqueue(("http://127.0.0.1:9/x",))
        assert harness.manager.jobs() == ()
        # The owner can override the check (an S3-backed archive does not consume local disk).
        job = harness.enqueue(("http://127.0.0.1:9/x",), ignore_space_check=True)
        assert job.status == JobStatus.QUEUED
    finally:
        harness.manager.stop()


def test_stops_when_space_runs_out_mid_download(
    tmp_path: Path, file_server: FileServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_server.behaviour.payload = PAYLOAD
    space = {"free": PLENTY}
    harness = Harness(tmp_path, space=lambda _: space["free"], reserve=1000)
    harness.manager.start()
    gate = threading.Event()
    file_server.behaviour.gate = gate
    try:
        job = harness.enqueue((f"{file_server.url}/{FILENAME}",))
        harness.wait_for(job.id, {JobStatus.DOWNLOADING})
        space["free"] = 10
        # The space check runs on an interval; make it run on the next block.
        monkeypatch.setattr(downloads_module, "SPACE_CHECK_INTERVAL_BYTES", 1)
        gate.set()
        done = harness.wait_for(job.id, {JobStatus.COMPLETED, JobStatus.FAILED})
        assert done.status == JobStatus.FAILED
        assert "disk is nearly full" in done.error
        assert not (tmp_path / "zims" / FILENAME).exists()
    finally:
        harness.manager.stop()


def test_dismiss_failed_job_discards_partial(harness: Harness, file_server: FileServer) -> None:
    file_server.behaviour.payload = PAYLOAD
    file_server.behaviour.status_override = 404
    job = harness.enqueue((f"{file_server.url}/{FILENAME}",))
    harness.wait_for(job.id, {JobStatus.FAILED})
    (harness.root / "partial" / f"{FILENAME}.part").write_bytes(b"partial")
    harness.manager.dismiss(job.id)
    assert harness.manager.jobs() == ()
    assert not (harness.root / "partial" / f"{FILENAME}.part").exists()


def test_state_survives_restart(harness: Harness, file_server: FileServer) -> None:
    file_server.behaviour.payload = PAYLOAD
    job = harness.enqueue((f"{file_server.url}/{FILENAME}",))
    harness.wait_for(job.id, {JobStatus.COMPLETED})
    harness.restart()
    jobs = harness.manager.jobs()
    assert [(restored.id, restored.status) for restored in jobs] == [(job.id, JobStatus.COMPLETED)]


def test_resume_skips_mirrors_that_ignore_range(tmp_path: Path, make_file_server: Callable[[], FileServer]) -> None:
    no_range = make_file_server()
    no_range.behaviour.payload = PAYLOAD
    no_range.behaviour.ignore_range = True
    with_range = make_file_server()
    with_range.behaviour.payload = PAYLOAD
    (tmp_path / "partial").mkdir()
    (tmp_path / "partial" / f"{FILENAME}.part").write_bytes(PAYLOAD[:1_500_000])
    harness = Harness(tmp_path)
    harness.manager.start()
    try:
        job = harness.enqueue((f"{no_range.url}/{FILENAME}", f"{with_range.url}/{FILENAME}"))
        assert job.bytes_done == 1_500_000
        done = harness.wait_for(job.id, {JobStatus.COMPLETED, JobStatus.FAILED})
        assert done.status == JobStatus.COMPLETED, done.error
        # The partial file was kept and resumed from the mirror that honours ranges.
        assert with_range.behaviour.requests == [("/" + FILENAME, "bytes=1500000-")]
        assert (tmp_path / "zims" / FILENAME).read_bytes() == PAYLOAD
    finally:
        harness.manager.stop()


def test_library_failure_after_download_is_reported(tmp_path: Path, file_server: FileServer) -> None:
    file_server.behaviour.payload = PAYLOAD

    def broken_library() -> None:
        raise RuntimeError("kiwix-manage exploded")

    manager = DownloadManager(
        state_path=tmp_path / "downloads.json",
        partial_dir=tmp_path / "partial",
        zim_dir=tmp_path / "zims",
        reserve_bytes=0,
        on_completed=broken_library,
        local_dir=tmp_path,
        space_probe=lambda _: PLENTY,
    )
    manager.start()
    try:
        job = manager.enqueue(
            book_id=BOOK_ID,
            title="Test",
            filename=FILENAME,
            size_bytes=len(PAYLOAD),
            sha256=SHA256,
            urls=(f"{file_server.url}/{FILENAME}",),
        )
        deadline = time.monotonic() + 30
        while (current := manager.jobs()[0]).active and time.monotonic() < deadline:
            time.sleep(0.05)
        assert current.status == JobStatus.FAILED
        assert "Rescan files" in current.error
        assert (tmp_path / "zims" / FILENAME).exists()
        assert job.id == current.id
    finally:
        manager.stop()
