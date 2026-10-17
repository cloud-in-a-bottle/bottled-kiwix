import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

import attr
from loguru import logger

from kiwix_manager.jsonio import atomic_write_text
from kiwix_manager.xmlutil import children
from kiwix_manager.xmlutil import parse_int
from kiwix_manager.xmlutil import parse_xml

EMPTY_LIBRARY = '<?xml version="1.0" encoding="UTF-8"?>\n<library version="20110515">\n</library>\n'
KIWIX_MANAGE_TIMEOUT_SECONDS = 600


class LibraryError(RuntimeError):
    pass


@attr.s(auto_attribs=True, frozen=True)
class Book:
    id: str
    path: Path
    name: str
    title: str
    description: str
    language: str
    flavour: str
    date: str
    size_bytes: int
    article_count: int
    media_count: int

    @property
    def content_name(self) -> str:
        """The kiwix-serve URL component for this book (``/content/<content_name>``), which is the file stem."""
        return self.path.stem


@attr.s(auto_attribs=True, frozen=True)
class SkippedFile:
    name: str
    reason: str


@attr.s(auto_attribs=True, frozen=True)
class ReconcileResult:
    added: tuple[str, ...]
    removed: tuple[str, ...]
    skipped: tuple[SkippedFile, ...]


def parse_library(text: str, library_dir: Path) -> tuple[Book, ...]:
    root = parse_xml(text)
    books: list[Book] = []
    for element in children(root, "book"):
        raw_path = element.get("path")
        book_id = element.get("id")
        if not raw_path or not book_id:
            continue
        # kiwix-manage stores paths relative to the library file.
        path = (library_dir / raw_path).resolve()
        books.append(
            Book(
                id=book_id,
                path=path,
                name=element.get("name", ""),
                title=element.get("title", "") or path.stem,
                description=element.get("description", ""),
                language=element.get("language", ""),
                flavour=element.get("flavour", ""),
                date=element.get("date", ""),
                # kiwix-manage records the size in KiB.
                size_bytes=parse_int(element.get("size")) * 1024,
                article_count=parse_int(element.get("articleCount")),
                media_count=parse_int(element.get("mediaCount")),
            )
        )
    return tuple(books)


class Library:
    """The kiwix-serve library file, kept in sync with the ZIM files on disk.

    The ZIM directory is the source of truth: reconcile() adds every ``*.zim`` file there to the library and drops
    entries whose file is gone. That lets the owner drop ZIM files in by other means (rsync, a file manager) as well as
    through the downloader. Every change is made on a copy and atomically renamed into place, because kiwix-serve
    watches the file (``--monitorLibrary``) and must never observe a half-written library.
    """

    def __init__(
        self,
        library_path: Path,
        zim_dir: Path,
        kiwix_manage_bin: str,
        wait_for_server: Callable[[frozenset[str]], None] = lambda _: None,
    ) -> None:
        self._library_path = library_path
        self._wait_for_server = wait_for_server
        self._zim_dir = zim_dir
        self._kiwix_manage_bin = kiwix_manage_bin
        self._lock = threading.Lock()
        self._books: tuple[Book, ...] = ()
        self._skipped: tuple[SkippedFile, ...] = ()

    def initialize(self) -> None:
        self._zim_dir.mkdir(parents=True, exist_ok=True)
        if not self._library_path.exists():
            atomic_write_text(self._library_path, EMPTY_LIBRARY)
        with self._lock:
            self._books = self._read()

    def books(self) -> tuple[Book, ...]:
        return self._books

    def skipped_files(self) -> tuple[SkippedFile, ...]:
        """ZIM files in the directory that are not in the library, and why."""
        return self._skipped

    def _read(self) -> tuple[Book, ...]:
        return parse_library(self._library_path.read_text(), self._library_path.parent.resolve())

    def _manage(self, library: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self._kiwix_manage_bin, str(library), *args],
            capture_output=True,
            text=True,
            timeout=KIWIX_MANAGE_TIMEOUT_SECONDS,
            check=False,
        )

    def _probe_id(self, path: Path, scratch_dir: Path) -> str | None:
        """The book id of a ZIM file, or None if kiwix-manage cannot read it."""
        scratch = scratch_dir / "probe.xml"
        scratch.unlink(missing_ok=True)
        result = self._manage(scratch, "add", str(path))
        if result.returncode != 0:
            logger.warning("Skipping invalid ZIM {}: {}", path, (result.stderr or result.stdout).strip())
            return None
        books = parse_library(scratch.read_text(), scratch_dir.resolve())
        return books[0].id if len(books) == 1 else None

    def reconcile(self) -> ReconcileResult:
        with self._lock:
            current = self._read()
            on_disk = {path.resolve() for path in self._zim_dir.glob("*.zim") if path.is_file()}
            known = {book.path for book in current}
            stale = [book for book in current if book.path not in on_disk]
            new = sorted(on_disk - known)
            skipped: dict[str, str] = {}
            added: list[str] = []
            removed: list[str] = []
            if stale or new:
                with tempfile.TemporaryDirectory(dir=self._library_path.parent, prefix=".reconcile.") as work:
                    work_dir = Path(work)
                    # kiwix-manage stores paths relative to the library file, so the working copy must sit next to
                    # the real one for those paths to stay valid after the rename.
                    tmp = self._library_path.with_name(f".{self._library_path.name}.{work_dir.name}.tmp")
                    shutil.copyfile(self._library_path, tmp)
                    try:
                        for book in stale:
                            result = self._manage(tmp, "remove", book.id)
                            if result.returncode != 0:
                                raise LibraryError(f"kiwix-manage could not remove {book.id}: {result.stderr.strip()}")
                            removed.append(book.path.name)
                        # The library is keyed by book id, so a second file with an id already present would silently
                        # replace the first entry. Keep the first one and report the other.
                        owners = {book.id: book.path.name for book in current if book.path in on_disk}
                        for path in new:
                            book_id = self._probe_id(path, work_dir)
                            if book_id is None:
                                skipped[path.name] = "not a valid ZIM file"
                                continue
                            if book_id in owners:
                                skipped[path.name] = f"the same book as {owners[book_id]}"
                                continue
                            result = self._manage(tmp, "add", str(path))
                            if result.returncode != 0:
                                skipped[path.name] = "not a valid ZIM file"
                                continue
                            owners[book_id] = path.name
                            added.append(path.name)
                        if added or removed:
                            self._replace_library(tmp)
                    finally:
                        tmp.unlink(missing_ok=True)
            books = self._read()
            if added or removed:
                # Deliberately under the lock: library changes are serialized, and each one is only reported once
                # the reader serves it (normally about a second; bounded by the wait's timeout). Readers of books()
                # are not blocked.
                self._wait_for_server(frozenset(book.id for book in books))
                logger.info("Library updated: added {} removed {}", added, removed)
            self._books = books
            self._skipped = tuple(SkippedFile(name=name, reason=reason) for name, reason in sorted(skipped.items()))
            return ReconcileResult(added=tuple(added), removed=tuple(removed), skipped=self._skipped)

    def _replace_library(self, new_library: Path) -> None:
        # kiwix-serve notices changes by comparing whole-second modification times, so a change made within the same
        # second as the previous one would be missed. Make sure the new file's mtime is at least a second later.
        previous_mtime = self._library_path.stat().st_mtime
        mtime = max(time.time(), previous_mtime + 1)
        os.utime(new_library, (mtime, mtime))
        os.replace(new_library, self._library_path)

    def delete(self, book_id: str) -> Book:
        with self._lock:
            book = next((book for book in self._read() if book.id == book_id), None)
            if book is None:
                raise LibraryError(f"no book with id {book_id}")
            if book.path.parent != self._zim_dir.resolve():
                raise LibraryError("refusing to delete a file outside the ZIM directory")
            book.path.unlink(missing_ok=True)
        self.reconcile()
        return book
