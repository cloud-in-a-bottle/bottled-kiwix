import re
import time
from collections.abc import Callable
from xml.etree.ElementTree import Element

import attr
import httpx

from kiwix_manager.xmlutil import XmlError
from kiwix_manager.xmlutil import child
from kiwix_manager.xmlutil import child_text
from kiwix_manager.xmlutil import children
from kiwix_manager.xmlutil import parse_int
from kiwix_manager.xmlutil import parse_xml

BOOK_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
ACQUISITION_REL = "http://opds-spec.org/acquisition/open-access"
THUMBNAIL_REL = "http://opds-spec.org/image/thumbnail"
DC_NAMESPACE = "http://purl.org/dc/terms/"
OPDS_NAMESPACE = "https://specs.opds.io/opds-1.2"
FACET_CACHE_SECONDS = 3600
MAX_PAGE_SIZE = 100


class CatalogError(RuntimeError):
    pass


@attr.s(auto_attribs=True, frozen=True)
class CatalogEntry:
    id: str
    title: str
    summary: str
    language: str
    name: str
    flavour: str
    category: str
    tags: str
    article_count: int
    media_count: int
    issued: str
    size_bytes: int
    meta4_url: str
    has_illustration: bool


@attr.s(auto_attribs=True, frozen=True)
class CatalogPage:
    total: int
    start: int
    entries: tuple[CatalogEntry, ...]


@attr.s(auto_attribs=True, frozen=True)
class Facet:
    value: str
    label: str
    count: int


@attr.s(auto_attribs=True, frozen=True)
class Illustration:
    content: bytes
    content_type: str


def _parse_entry(element: Element) -> CatalogEntry | None:
    raw_id = child_text(element, "id").removeprefix("urn:uuid:")
    if not BOOK_ID_RE.match(raw_id):
        return None
    meta4_url = ""
    size_bytes = 0
    has_illustration = False
    for link in children(element, "link"):
        rel = link.get("rel")
        if rel == ACQUISITION_REL:
            meta4_url = link.get("href", "")
            size_bytes = parse_int(link.get("length", ""))
        elif rel == THUMBNAIL_REL:
            has_illustration = True
    if not meta4_url:
        return None
    issued = child_text(element, "issued") or child_text(element, "updated")
    return CatalogEntry(
        id=raw_id,
        title=child_text(element, "title"),
        summary=child_text(element, "summary"),
        language=child_text(element, "language"),
        name=child_text(element, "name"),
        flavour=child_text(element, "flavour"),
        category=child_text(element, "category"),
        tags=child_text(element, "tags"),
        article_count=parse_int(child_text(element, "articleCount")),
        media_count=parse_int(child_text(element, "mediaCount")),
        issued=issued[:10],
        size_bytes=size_bytes,
        meta4_url=meta4_url,
        has_illustration=has_illustration,
    )


def parse_entries(text: str | bytes) -> CatalogPage:
    root = parse_xml(text)
    entries = tuple(entry for element in children(root, "entry") if (entry := _parse_entry(element)) is not None)
    return CatalogPage(
        total=parse_int(child_text(root, "totalResults")) if child(root, "totalResults") is not None else len(entries),
        start=parse_int(child_text(root, "startIndex")),
        entries=entries,
    )


def parse_entry_ids(text: str | bytes) -> frozenset[str]:
    """Book ids in a feed, including entries without a download link (as served for a local library)."""
    root = parse_xml(text)
    ids = (child_text(element, "id").removeprefix("urn:uuid:") for element in children(root, "entry"))
    return frozenset(book_id for book_id in ids if book_id)


def parse_facets(text: str | bytes, value_element: str) -> tuple[Facet, ...]:
    root = parse_xml(text)
    facets: list[Facet] = []
    for element in children(root, "entry"):
        label = child_text(element, "title")
        value = child_text(element, value_element) or label
        count_element = child(element, "count")
        count = parse_int((count_element.text or "").strip()) if count_element is not None else 0
        if value:
            facets.append(Facet(value=value, label=label, count=count))
    return tuple(facets)


def _parse[T](parser: Callable[[str | bytes], T], text: str | bytes) -> T:
    try:
        return parser(text)
    except XmlError as error:
        raise CatalogError(f"the Kiwix catalog returned malformed XML: {error}") from error


def _wrap_entry_document(text: str) -> str:
    """The single-entry endpoint omits the namespace declarations its own ``dc:`` elements need, which makes it invalid
    XML. Wrap it in a feed element that declares them."""
    body = re.sub(r"^\s*<\?xml[^>]*\?>", "", text)
    return f'<feed xmlns="http://www.w3.org/2005/Atom" xmlns:dc="{DC_NAMESPACE}" xmlns:opds="{OPDS_NAMESPACE}">{body}</feed>'


class CatalogClient:
    """Read-only client for the Kiwix OPDS v2 catalog."""

    def __init__(self, base_url: str, client: httpx.AsyncClient) -> None:
        self._base_url = base_url
        self._client = client
        self._facet_cache: dict[str, tuple[float, tuple[Facet, ...]]] = {}

    async def _get(self, path: str, params: dict[str, str] | None = None) -> httpx.Response:
        try:
            response = await self._client.get(f"{self._base_url}{path}", params=params, follow_redirects=True)
        except httpx.HTTPError as error:
            raise CatalogError(f"could not reach the Kiwix catalog: {error}") from error
        if response.status_code != 200:
            raise CatalogError(f"the Kiwix catalog returned HTTP {response.status_code} for {path}")
        return response

    async def search(self, query: str, language: str, category: str, start: int, count: int) -> CatalogPage:
        params = {"start": str(max(start, 0)), "count": str(min(max(count, 1), MAX_PAGE_SIZE))}
        if query:
            params["q"] = query
        if language:
            params["lang"] = language
        if category:
            params["category"] = category
        response = await self._get("/catalog/v2/entries", params)
        return _parse(parse_entries, response.content)

    async def entry(self, book_id: str) -> CatalogEntry:
        if not BOOK_ID_RE.match(book_id):
            raise CatalogError("invalid book id")
        response = await self._get(f"/catalog/v2/entry/{book_id}")
        page = _parse(parse_entries, _wrap_entry_document(response.text))
        if not page.entries or page.entries[0].id != book_id:
            raise CatalogError(f"book {book_id} is not in the Kiwix catalog")
        return page.entries[0]

    async def illustration(self, book_id: str) -> Illustration:
        if not BOOK_ID_RE.match(book_id):
            raise CatalogError("invalid book id")
        response = await self._get(f"/catalog/v2/illustration/{book_id}/", {"size": "48"})
        content_type = response.headers.get("content-type", "")
        if not content_type.startswith("image/"):
            raise CatalogError("catalog illustration is not an image")
        return Illustration(content=response.content, content_type=content_type)

    async def _facets(self, path: str, value_element: str) -> tuple[Facet, ...]:
        cached = self._facet_cache.get(path)
        if cached is not None and time.monotonic() - cached[0] < FACET_CACHE_SECONDS:
            return cached[1]
        response = await self._get(path)
        facets = _parse(lambda text: parse_facets(text, value_element), response.content)
        self._facet_cache[path] = (time.monotonic(), facets)
        return facets

    async def languages(self) -> tuple[Facet, ...]:
        return await self._facets("/catalog/v2/languages", "language")

    async def categories(self) -> tuple[Facet, ...]:
        return await self._facets("/catalog/v2/categories", "title")
