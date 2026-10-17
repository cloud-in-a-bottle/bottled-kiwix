import asyncio
import contextlib
import os
import time
from pathlib import Path

import httpx
from loguru import logger

from kiwix_manager.catalog import parse_entry_ids
from kiwix_manager.xmlutil import XmlError

RESTART_DELAY_SECONDS = 2.0
MAX_RESTART_DELAY_SECONDS = 60.0
# A run at least this long counts as healthy and resets the restart backoff.
HEALTHY_RUN_SECONDS = 60.0
# kiwix-serve polls the library file about once a second.
LIBRARY_SYNC_TIMEOUT_SECONDS = 10.0


class KiwixServe:
    """Runs kiwix-serve on loopback and restarts it if it exits."""

    def __init__(self, binary: str, library_path: Path, port: int) -> None:
        self._binary = binary
        self._library_path = library_path
        self._port = port
        self._process: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    def _command(self) -> list[str]:
        return [
            self._binary,
            "--address=127.0.0.1",
            f"--port={self._port}",
            "--monitorLibrary",
            # Stable /content/<name>_<flavour> aliases that survive updating a book to a newer dated release.
            "--nodatealiases",
            "--skipInvalid",
            # Exit with us rather than lingering if the manager dies.
            f"--attachToProcess={os.getpid()}",
            "--library",
            str(self._library_path),
        ]

    async def start(self) -> None:
        self._task = asyncio.create_task(self._supervise())

    async def _supervise(self) -> None:
        delay = RESTART_DELAY_SECONDS
        loop = asyncio.get_running_loop()
        while not self._stopping:
            started = loop.time()
            try:
                self._process = await asyncio.create_subprocess_exec(*self._command())
            except OSError as error:
                self._process = None
                logger.error("Could not start kiwix-serve ({}); retrying in {:.0f}s", error, delay)
            else:
                logger.info("Started kiwix-serve (pid {})", self._process.pid)
                code = await self._process.wait()
                if self._stopping:
                    return
                if loop.time() - started >= HEALTHY_RUN_SECONDS:
                    delay = RESTART_DELAY_SECONDS
                logger.error("kiwix-serve exited with code {}; restarting in {:.0f}s", code, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, MAX_RESTART_DELAY_SECONDS)

    def wait_for_books(self, expected_ids: frozenset[str]) -> None:
        """Block until kiwix-serve serves exactly ``expected_ids``.

        kiwix-serve reloads the library file on its own schedule, so without this a book could be reported as
        installed (or deleted) a moment before the reader actually serves (or stops serving) it.
        """
        if not self.running:
            # Not started yet (or restarting): it loads the current library file when it starts.
            return
        deadline = time.monotonic() + LIBRARY_SYNC_TIMEOUT_SECONDS
        served: frozenset[str] = frozenset()
        last_error = ""
        with httpx.Client(base_url=self.base_url, timeout=5.0) as client:
            while time.monotonic() < deadline:
                try:
                    response = client.get("/catalog/v2/entries", params={"count": "100000"})
                    if response.status_code == 200:
                        served = parse_entry_ids(response.content)
                        if served == expected_ids:
                            return
                except (httpx.HTTPError, XmlError) as error:
                    last_error = str(error)
                    logger.debug("Waiting for kiwix-serve: {}", error)
                time.sleep(0.2)
        logger.warning(
            "kiwix-serve did not pick up the library change in time (serving {}; last error: {})",
            sorted(served),
            last_error or "none",
        )

    async def stop(self) -> None:
        self._stopping = True
        process = self._process
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except TimeoutError:
                process.kill()
                await process.wait()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
