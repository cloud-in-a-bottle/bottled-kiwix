import asyncio
import enum

import attr
from litestar import Response
from litestar import Router
from litestar import delete
from litestar import get
from litestar import post
from litestar.exceptions import ClientException
from litestar.exceptions import HTTPException
from litestar.exceptions import NotAuthorizedException
from litestar.exceptions import NotFoundException
from litestar.exceptions import PermissionDeniedException
from litestar.handlers.base import BaseRouteHandler
from litestar.params import FromPath
from litestar.params import FromQuery
from litestar.status_codes import HTTP_409_CONFLICT
from litestar.status_codes import HTTP_502_BAD_GATEWAY
from loguru import logger

from kiwix_manager.auth import is_owner
from kiwix_manager.auth import is_same_origin_request
from kiwix_manager.catalog import BOOK_ID_RE
from kiwix_manager.catalog import CatalogEntry
from kiwix_manager.catalog import CatalogError
from kiwix_manager.catalog import Facet
from kiwix_manager.downloads import DownloadError
from kiwix_manager.downloads import InsufficientSpaceError
from kiwix_manager.downloads import Job
from kiwix_manager.library import Book
from kiwix_manager.library import LibraryError
from kiwix_manager.library import SkippedFile
from kiwix_manager.metalink import MetalinkError
from kiwix_manager.metalink import parse_metalink
from kiwix_manager.presets import PRESETS
from kiwix_manager.presets import Preset
from kiwix_manager.routes.types import ManagerConnection
from kiwix_manager.routes.types import ManagerRequest
from kiwix_manager.services import Services
from kiwix_manager.settings import Settings

STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def owner_guard(connection: ManagerConnection, _: BaseRouteHandler) -> None:
    if not is_owner(connection.headers):
        raise NotAuthorizedException("Owner login required")
    if connection.scope.get("method") in STATE_CHANGING_METHODS and not is_same_origin_request(connection.headers):
        raise PermissionDeniedException("Cross-origin request refused")


def services_of(request: ManagerRequest) -> Services:
    services: Services = request.app.state.services
    return services


class EntryStatus(enum.StrEnum):
    AVAILABLE = "available"
    INSTALLED = "installed"
    DOWNLOADING = "downloading"
    UPDATE_AVAILABLE = "update_available"


@attr.s(auto_attribs=True, frozen=True)
class BookView:
    id: str
    filename: str
    content_name: str
    name: str
    title: str
    description: str
    language: str
    flavour: str
    date: str
    size_bytes: int
    article_count: int
    media_count: int


@attr.s(auto_attribs=True, frozen=True)
class JobView:
    id: str
    book_id: str
    title: str
    filename: str
    size_bytes: int
    bytes_done: int
    status: str
    error: str
    created_at: float


@attr.s(auto_attribs=True, frozen=True)
class StateView:
    public: bool
    kiwix_running: bool
    free_bytes: int
    reserve_bytes: int
    books: tuple[BookView, ...]
    skipped_files: tuple[SkippedFile, ...]
    downloads: tuple[JobView, ...]


@attr.s(auto_attribs=True, frozen=True)
class EntryView:
    entry: CatalogEntry
    status: EntryStatus


@attr.s(auto_attribs=True, frozen=True)
class CatalogView:
    total: int
    start: int
    entries: tuple[EntryView, ...]


@attr.s(auto_attribs=True, frozen=True)
class DownloadRequest:
    book_id: str
    # Skip the up-front free-space check (for archives backed by S3, where the local disk is not the limit).
    ignore_space_check: bool = False


@attr.s(auto_attribs=True, frozen=True)
class SettingsRequest:
    public: bool


def book_view(book: Book) -> BookView:
    return BookView(
        id=book.id,
        filename=book.path.name,
        content_name=book.content_name,
        name=book.name,
        title=book.title,
        description=book.description,
        language=book.language,
        flavour=book.flavour,
        date=book.date,
        size_bytes=book.size_bytes,
        article_count=book.article_count,
        media_count=book.media_count,
    )


def job_view(job: Job) -> JobView:
    return JobView(
        id=job.id,
        book_id=job.book_id,
        title=job.title,
        filename=job.filename,
        size_bytes=job.size_bytes,
        bytes_done=job.bytes_done,
        status=job.status.value,
        error=job.error,
        created_at=job.created_at,
    )


def entry_status(entry: CatalogEntry, books: tuple[Book, ...], jobs: tuple[Job, ...]) -> EntryStatus:
    if any(job.active and job.book_id == entry.id for job in jobs):
        return EntryStatus.DOWNLOADING
    if any(book.id == entry.id for book in books):
        return EntryStatus.INSTALLED
    same_book = [book for book in books if book.name == entry.name and book.flavour == entry.flavour]
    if same_book and entry.name and entry.issued and all(book.date and book.date < entry.issued for book in same_book):
        return EntryStatus.UPDATE_AVAILABLE
    return EntryStatus.AVAILABLE


def _state(services: Services) -> StateView:
    return StateView(
        public=services.settings.get().public,
        kiwix_running=services.kiwix.running,
        free_bytes=services.downloads.free_space(),
        reserve_bytes=services.config.reserve_bytes,
        books=tuple(book_view(book) for book in sorted(services.library.books(), key=lambda book: book.title.lower())),
        skipped_files=services.library.skipped_files(),
        downloads=tuple(job_view(job) for job in reversed(services.downloads.jobs())),
    )


def _catalog_failure(error: CatalogError) -> HTTPException:
    return HTTPException(status_code=HTTP_502_BAD_GATEWAY, detail=str(error))


@get("/state", sync_to_thread=False)
def state(request: ManagerRequest) -> StateView:
    return _state(services_of(request))


@get("/catalog")
async def catalog(
    request: ManagerRequest,
    q: FromQuery[str] = "",
    lang: FromQuery[str] = "",
    category: FromQuery[str] = "",
    start: FromQuery[int] = 0,
    count: FromQuery[int] = 24,
) -> CatalogView:
    services = services_of(request)
    try:
        page = await services.catalog.search(q.strip(), lang, category, start, count)
    except CatalogError as error:
        raise _catalog_failure(error) from error
    books = services.library.books()
    jobs = services.downloads.jobs()
    return CatalogView(
        total=page.total,
        start=page.start,
        entries=tuple(EntryView(entry=entry, status=entry_status(entry, books, jobs)) for entry in page.entries),
    )


@attr.s(auto_attribs=True, frozen=True)
class PresetView:
    key: str
    label: str
    description: str
    # None when the catalog could not be reached or no longer lists this book.
    entry: CatalogEntry | None
    status: EntryStatus


async def _resolve_preset(services: Services, preset: Preset) -> CatalogEntry | None:
    try:
        entries = await services.catalog.by_name(preset.name)
    except CatalogError as error:
        logger.warning("Could not resolve preset {}: {}", preset.key, error)
        return None
    matching = [entry for entry in entries if entry.flavour == preset.flavour]
    return max(matching, key=lambda entry: entry.issued, default=None)


@get("/presets")
async def presets(request: ManagerRequest) -> tuple[PresetView, ...]:
    services = services_of(request)
    entries = await asyncio.gather(*(_resolve_preset(services, preset) for preset in PRESETS))
    books = services.library.books()
    jobs = services.downloads.jobs()
    views: list[PresetView] = []
    for preset, entry in zip(PRESETS, entries, strict=True):
        status = EntryStatus.AVAILABLE
        if entry is not None:
            status = entry_status(entry, books, jobs)
            # Any installed edition counts: an older release is offered as an update, not a fresh download.
            if status == EntryStatus.AVAILABLE and any(
                book.name == preset.name and book.flavour == preset.flavour for book in books
            ):
                status = EntryStatus.INSTALLED
        views.append(
            PresetView(key=preset.key, label=preset.label, description=preset.description, entry=entry, status=status)
        )
    return tuple(views)


@get("/catalog/languages")
async def languages(request: ManagerRequest) -> tuple[Facet, ...]:
    try:
        return await services_of(request).catalog.languages()
    except CatalogError as error:
        raise _catalog_failure(error) from error


@get("/catalog/categories")
async def categories(request: ManagerRequest) -> tuple[Facet, ...]:
    try:
        return await services_of(request).catalog.categories()
    except CatalogError as error:
        raise _catalog_failure(error) from error


@get("/catalog/illustration/{book_id:str}")
async def illustration(request: ManagerRequest, book_id: FromPath[str]) -> Response[bytes]:
    if not BOOK_ID_RE.match(book_id):
        raise NotFoundException()
    try:
        image = await services_of(request).catalog.illustration(book_id)
    except CatalogError as error:
        raise NotFoundException() from error
    return Response(
        content=image.content,
        media_type=image.content_type,
        headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"},
    )


@post("/downloads")
async def start_download(request: ManagerRequest, data: DownloadRequest) -> JobView:
    services = services_of(request)
    config = services.config
    try:
        entry = await services.catalog.entry(data.book_id)
    except CatalogError as error:
        raise ClientException(str(error)) from error
    if any(book.id == entry.id for book in services.library.books()):
        raise ClientException(f"{entry.title} is already in the library")
    if not entry.meta4_url.startswith(config.meta4_url_prefixes):
        raise ClientException(f"Refusing download from unexpected location {entry.meta4_url}")
    try:
        response = await services.catalog_http.get(entry.meta4_url, follow_redirects=False)
    except Exception as error:
        raise HTTPException(
            status_code=HTTP_502_BAD_GATEWAY, detail=f"Could not fetch download info: {error}"
        ) from error
    if response.status_code != 200:
        raise HTTPException(
            status_code=HTTP_502_BAD_GATEWAY, detail=f"Download info request returned HTTP {response.status_code}"
        )
    try:
        metalink = parse_metalink(response.content, config.allow_insecure_mirrors)
    except MetalinkError as error:
        raise HTTPException(status_code=HTTP_502_BAD_GATEWAY, detail=f"Bad download info: {error}") from error
    try:
        job = await asyncio.to_thread(
            services.downloads.enqueue,
            book_id=entry.id,
            title=entry.title if not entry.flavour else f"{entry.title} ({entry.flavour})",
            filename=metalink.filename,
            size_bytes=metalink.size,
            sha256=metalink.sha256,
            urls=metalink.urls,
            ignore_space_check=data.ignore_space_check,
        )
    except InsufficientSpaceError as error:
        raise HTTPException(
            status_code=HTTP_409_CONFLICT, detail=str(error), extra={"code": "insufficient_space"}
        ) from error
    except DownloadError as error:
        raise ClientException(str(error)) from error
    return job_view(job)


@post("/downloads/{job_id:str}/cancel")
async def cancel_download(request: ManagerRequest, job_id: FromPath[str]) -> JobView:
    try:
        return job_view(await asyncio.to_thread(services_of(request).downloads.cancel, job_id))
    except DownloadError as error:
        raise ClientException(str(error)) from error


@post("/downloads/{job_id:str}/retry")
async def retry_download(request: ManagerRequest, job_id: FromPath[str]) -> JobView:
    try:
        return job_view(await asyncio.to_thread(services_of(request).downloads.retry, job_id))
    except DownloadError as error:
        raise ClientException(str(error)) from error


@delete("/downloads/{job_id:str}", status_code=204)
async def dismiss_download(request: ManagerRequest, job_id: FromPath[str]) -> None:
    try:
        await asyncio.to_thread(services_of(request).downloads.dismiss, job_id)
    except DownloadError as error:
        raise ClientException(str(error)) from error


@delete("/books/{book_id:str}", status_code=204)
async def delete_book(request: ManagerRequest, book_id: FromPath[str]) -> None:
    try:
        await asyncio.to_thread(services_of(request).library.delete, book_id)
    except LibraryError as error:
        raise ClientException(str(error)) from error


@post("/rescan")
async def rescan(request: ManagerRequest) -> StateView:
    services = services_of(request)
    try:
        await asyncio.to_thread(services.library.reconcile)
    except LibraryError as error:
        raise ClientException(str(error)) from error
    return _state(services)


@post("/settings")
async def update_settings(request: ManagerRequest, data: SettingsRequest) -> StateView:
    services = services_of(request)
    await asyncio.to_thread(services.settings.update, Settings(public=data.public))
    return _state(services)


api_router = Router(
    path="/_manage/api",
    guards=[owner_guard],
    route_handlers=[
        state,
        catalog,
        languages,
        categories,
        illustration,
        presets,
        start_download,
        cancel_download,
        retry_download,
        dismiss_download,
        delete_book,
        rescan,
        update_settings,
    ],
)
