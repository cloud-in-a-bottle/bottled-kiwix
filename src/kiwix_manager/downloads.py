import enum
import hashlib
import os
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import attr
import httpx
from loguru import logger

from kiwix_manager.jsonio import atomic_write_json
from kiwix_manager.jsonio import read_json

CHUNK_BYTES = 1024 * 1024
PERSIST_INTERVAL_SECONDS = 5.0
SPACE_CHECK_INTERVAL_BYTES = 256 * 1024 * 1024
ATTEMPTS_PER_MIRROR = 3
RETRY_DELAY_SECONDS = 5.0
HTTP_TIMEOUT = httpx.Timeout(connect=30.0, read=120.0, write=60.0, pool=30.0)
# Wikimedia's mirror rejects generic client user agents.
USER_AGENT = "bottled-kiwix/0.1 (Cloud in a Bottle Kiwix app; https://github.com/cloud-in-a-bottle/bottled-kiwix)"


class JobStatus(enum.StrEnum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE_STATUSES = frozenset({JobStatus.QUEUED, JobStatus.DOWNLOADING, JobStatus.VERIFYING})


@attr.s(auto_attribs=True, frozen=True)
class Job:
    id: str
    book_id: str
    title: str
    filename: str
    size_bytes: int
    sha256: str
    urls: tuple[str, ...]
    status: JobStatus
    bytes_done: int = 0
    error: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES


class DownloadError(RuntimeError):
    pass


class DiskSpaceError(DownloadError):
    pass


class InsufficientSpaceError(DownloadError):
    pass


class MirrorRejected(DownloadError):
    """The mirror answered with a client error; retrying the same mirror will not help."""


class DownloadCancelled(Exception):
    pass


def job_to_json(job: Job) -> dict[str, object]:
    return {**attr.asdict(job), "urls": list(job.urls), "status": job.status.value}


def job_from_json(raw: dict[str, object]) -> Job:
    urls = raw["urls"]
    assert isinstance(urls, list)
    values = {**raw, "urls": tuple(str(url) for url in urls), "status": JobStatus(str(raw["status"]))}
    return Job(**values)  # type: ignore[arg-type]


def free_bytes(path: Path) -> int:
    stat = os.statvfs(path)
    return stat.f_bavail * stat.f_frsize


class DownloadManager:
    """Downloads ZIM files one at a time in a background thread.

    Jobs are persisted so that a restart resumes them from the partial file (HTTP range requests). The SHA-256 from the
    Kiwix metalink is checked before a file is moved into the ZIM directory, so a corrupt or tampered mirror can never
    produce a library entry.
    """

    def __init__(
        self,
        state_path: Path,
        partial_dir: Path,
        zim_dir: Path,
        reserve_bytes: int,
        on_completed: Callable[[], object],
        local_dir: Path,
        space_probe: Callable[[Path], int] = free_bytes,
    ) -> None:
        self._state_path = state_path
        self._partial_dir = partial_dir
        self._zim_dir = zim_dir
        self._reserve_bytes = reserve_bytes
        self._on_completed = on_completed
        self._space_probe = space_probe
        self._space_dirs = (zim_dir, local_dir)
        self._lock = threading.Lock()
        self._wakeup = threading.Condition(self._lock)
        self._jobs: dict[str, Job] = {}
        self._current_id: str | None = None
        self._cancel_current = threading.Event()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    # ----- state -----

    def _persist_locked(self) -> None:
        atomic_write_json(self._state_path, [job_to_json(job) for job in self._jobs.values()])

    def _load(self) -> None:
        if not self._state_path.exists():
            return
        for raw in read_json(self._state_path):
            job = job_from_json(raw)
            if job.status in (JobStatus.DOWNLOADING, JobStatus.VERIFYING):
                # Interrupted by a restart: resume from the partial file.
                job = attr.evolve(job, status=JobStatus.QUEUED)
            self._jobs[job.id] = job

    def _update(self, job_id: str, persist: bool = True, **changes: object) -> Job:
        with self._lock:
            job = attr.evolve(self._jobs[job_id], updated_at=time.time(), **changes)  # type: ignore[arg-type]
            self._jobs[job_id] = job
            if persist:
                self._persist_locked()
            return job

    def jobs(self) -> tuple[Job, ...]:
        with self._lock:
            return tuple(sorted(self._jobs.values(), key=lambda job: job.created_at))

    def free_space(self) -> int:
        """Free space on the instance's local disk.

        The archive directory is a JuiceFS mount that reports a fixed 1 PiB capacity whatever is underneath, so it
        cannot answer this. With the default local object store the ZIM bytes land on the same disk as app data, so the
        app data directory's filesystem is the real limit. With an S3 store local usage stays roughly flat, which is
        why the up-front check can be overridden; the check during a download only guards the local disk itself.
        """
        return min(self._space_probe(path) for path in self._space_dirs)

    def _partial_path(self, filename: str) -> Path:
        return self._partial_dir / f"{filename}.part"

    def _remaining_locked(self, exclude: str | None = None) -> int:
        total = 0
        for job in self._jobs.values():
            if job.active and job.id != exclude:
                total += job.size_bytes - self._partial_size(job.filename)
        return total

    def _partial_size(self, filename: str) -> int:
        try:
            return self._partial_path(filename).stat().st_size
        except FileNotFoundError:
            return 0

    # ----- lifecycle -----

    def start(self) -> None:
        self._partial_dir.mkdir(parents=True, exist_ok=True)
        self._zim_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._load()
            self._persist_locked()
        self._thread = threading.Thread(target=self._run, name="zim-downloader", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        self._cancel_current.set()
        with self._lock:
            self._wakeup.notify_all()
        if self._thread is not None:
            # The worker may be blocked on a network read; it is a daemon thread and its state is always resumable.
            self._thread.join(timeout=5)

    # ----- commands -----

    def enqueue(
        self,
        book_id: str,
        title: str,
        filename: str,
        size_bytes: int,
        sha256: str,
        urls: tuple[str, ...],
        ignore_space_check: bool = False,
    ) -> Job:
        with self._lock:
            for job in self._jobs.values():
                if job.active and (job.book_id == book_id or job.filename == filename):
                    raise DownloadError(f"{title} is already being downloaded")
            if (self._zim_dir / filename).exists():
                raise DownloadError(f"{filename} is already in the library")
            needed = size_bytes - self._partial_size(filename) + self._remaining_locked() + self._reserve_bytes
            available = self.free_space()
            if needed > available and not ignore_space_check:
                raise InsufficientSpaceError(
                    f"This needs {_gib(needed)} (including queued downloads and a {_gib(self._reserve_bytes)} safety "
                    f"margin) but this instance's disk has {_gib(available)} free."
                )
            now = time.time()
            job = Job(
                id=uuid.uuid4().hex,
                book_id=book_id,
                title=title,
                filename=filename,
                size_bytes=size_bytes,
                sha256=sha256,
                urls=urls,
                status=JobStatus.QUEUED,
                bytes_done=self._partial_size(filename),
                created_at=now,
                updated_at=now,
            )
            self._jobs[job.id] = job
            self._persist_locked()
            self._wakeup.notify_all()
            return job

    def cancel(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise DownloadError("no such download")
            if not job.active:
                raise DownloadError("download is not in progress")
            if self._current_id == job_id:
                self._cancel_current.set()
            job = attr.evolve(job, status=JobStatus.CANCELLED, error="", updated_at=time.time())
            self._jobs[job_id] = job
            self._persist_locked()
        if self._current_id != job_id:
            self._partial_path(job.filename).unlink(missing_ok=True)
        return job

    def retry(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise DownloadError("no such download")
            if job.status not in (JobStatus.FAILED, JobStatus.CANCELLED):
                raise DownloadError("only failed or cancelled downloads can be retried")
            if (self._zim_dir / job.filename).exists():
                raise DownloadError(f"{job.filename} is already in the library")
            for other in self._jobs.values():
                if other.active and other.filename == job.filename:
                    raise DownloadError(f"{job.title} is already being downloaded")
            job = attr.evolve(job, status=JobStatus.QUEUED, error="", updated_at=time.time())
            self._jobs[job_id] = job
            self._persist_locked()
            self._wakeup.notify_all()
            return job

    def dismiss(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise DownloadError("no such download")
            if job.active:
                raise DownloadError("cancel the download before dismissing it")
            del self._jobs[job_id]
            self._persist_locked()
        if job.status == JobStatus.FAILED:
            # A failed job keeps its partial file so a retry can resume; dismissing it gives up on that.
            self._partial_path(job.filename).unlink(missing_ok=True)

    # ----- worker -----

    def _next_job(self) -> Job | None:
        with self._lock:
            while not self._stopping.is_set():
                queued = [job for job in self._jobs.values() if job.status == JobStatus.QUEUED]
                if queued:
                    job = min(queued, key=lambda job: job.created_at)
                    self._current_id = job.id
                    self._cancel_current.clear()
                    return job
                self._wakeup.wait()
            return None

    def _run(self) -> None:
        with httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
            while (job := self._next_job()) is not None:
                try:
                    self._download(client, job)
                except DownloadCancelled:
                    if not self._stopping.is_set():
                        self._partial_path(job.filename).unlink(missing_ok=True)
                        logger.info("Download of {} cancelled", job.filename)
                except Exception as error:  # noqa: BLE001 - any failure must be reported on the job, not kill the worker
                    logger.exception("Download of {} failed", job.filename)
                    with self._lock:
                        still_wanted = self._jobs.get(job.id) is not None and self._jobs[job.id].active
                    if still_wanted:
                        self._update(job.id, status=JobStatus.FAILED, error=str(error))
                finally:
                    with self._lock:
                        self._current_id = None

    def _check_cancel(self) -> None:
        if self._cancel_current.is_set():
            raise DownloadCancelled()

    def _sleep(self, seconds: float) -> None:
        if self._cancel_current.wait(seconds):
            raise DownloadCancelled()

    def _check_space(self) -> None:
        available = self.free_space()
        if available < self._reserve_bytes:
            raise DiskSpaceError(
                f"Stopped because the disk is nearly full ({_gib(available)} free, {_gib(self._reserve_bytes)} is "
                "always kept free). Free up space and resume."
            )

    def _download(self, client: httpx.Client, job: Job) -> None:
        partial = self._partial_path(job.filename)
        target = self._zim_dir / job.filename
        progress = _Progress()
        existing_size = self._partial_size(job.filename)
        if existing_size > job.size_bytes:
            partial.unlink()
            existing_size = 0
        job = self._update(job.id, status=JobStatus.DOWNLOADING, error="", bytes_done=existing_size)
        if existing_size:
            # Resuming: the hash must cover the bytes already on disk.
            with partial.open("rb") as existing:
                while block := existing.read(CHUNK_BYTES):
                    self._check_cancel()
                    progress.add(block)
            logger.info("Resuming {} at {} bytes", job.filename, progress.done)
        self._check_space()

        last_error = "no mirrors"
        for url in job.urls:
            for attempt in range(1, ATTEMPTS_PER_MIRROR + 1):
                if progress.done == job.size_bytes:
                    break
                try:
                    self._fetch(client, job, url, progress)
                except (httpx.HTTPError, DownloadError, OSError) as error:
                    if isinstance(error, DiskSpaceError):
                        raise
                    last_error = f"{url}: {error}"
                    logger.warning("Download attempt {} from {} failed: {}", attempt, url, error)
                    # Drop any bytes written past the last fully hashed block so the file matches the hash state.
                    if partial.exists():
                        os.truncate(partial, progress.done)
                    self._update(job.id, bytes_done=progress.done)
                    if isinstance(error, MirrorRejected):
                        break
                    if attempt < ATTEMPTS_PER_MIRROR:
                        self._sleep(RETRY_DELAY_SECONDS)
            if progress.done == job.size_bytes:
                break
        self._check_cancel()
        if progress.done != job.size_bytes:
            raise DownloadError(f"All mirrors failed. Last error: {last_error}")

        self._update(job.id, status=JobStatus.VERIFYING, bytes_done=progress.done)
        digest = progress.hasher.hexdigest()
        if digest != job.sha256:
            partial.unlink(missing_ok=True)
            raise DownloadError(
                f"Checksum mismatch (expected {job.sha256}, got {digest}); the partial file was deleted"
            )
        self._check_cancel()
        os.replace(partial, target)
        logger.info("Downloaded and verified {}", job.filename)
        self._on_completed()
        self._update(job.id, status=JobStatus.COMPLETED, bytes_done=progress.done)

    def _fetch(self, client: httpx.Client, job: Job, url: str, progress: "_Progress") -> None:
        headers = {"Range": f"bytes={progress.done}-"} if progress.done else {}
        with client.stream("GET", url, headers=headers) as response:
            if progress.done and response.status_code == 206:
                content_range = response.headers.get("content-range", "")
                if not content_range.startswith(f"bytes {progress.done}-"):
                    raise DownloadError(f"unexpected Content-Range {content_range!r}")
                mode = "ab"
            elif response.status_code == 200:
                # A fresh download, or a mirror that ignored the range request: start over.
                progress.reset()
                mode = "wb"
            elif 400 <= response.status_code < 500 and response.status_code not in (408, 429):
                raise MirrorRejected(f"HTTP {response.status_code}")
            else:
                raise DownloadError(f"HTTP {response.status_code}")
            last_persist = time.monotonic()
            since_space_check = 0
            with self._partial_path(job.filename).open(mode) as output:
                try:
                    for block in response.iter_bytes(CHUNK_BYTES):
                        self._check_cancel()
                        if progress.done + len(block) > job.size_bytes:
                            raise DownloadError("mirror sent more data than expected")
                        output.write(block)
                        progress.add(block)
                        since_space_check += len(block)
                        if since_space_check >= SPACE_CHECK_INTERVAL_BYTES:
                            since_space_check = 0
                            self._check_space()
                        now = time.monotonic()
                        persist = now - last_persist >= PERSIST_INTERVAL_SECONDS
                        if persist:
                            last_persist = now
                        self._update(job.id, persist=persist, bytes_done=progress.done)
                finally:
                    output.flush()
                    os.fsync(output.fileno())
            self._update(job.id, bytes_done=progress.done)


class _Progress:
    """Bytes written to the partial file so far and the running hash over exactly those bytes."""

    def __init__(self) -> None:
        self.done = 0
        self.hasher = hashlib.sha256()

    def add(self, block: bytes) -> None:
        self.hasher.update(block)
        self.done += len(block)

    def reset(self) -> None:
        self.done = 0
        self.hasher = hashlib.sha256()


def _gib(size: int) -> str:
    return f"{size / 1024**3:.1f} GiB"
