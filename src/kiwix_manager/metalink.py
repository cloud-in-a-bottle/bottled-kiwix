import re
from xml.etree.ElementTree import ParseError

import attr

from kiwix_manager.xmlutil import child_text
from kiwix_manager.xmlutil import children
from kiwix_manager.xmlutil import local_name
from kiwix_manager.xmlutil import parse_xml

ZIM_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}\.zim$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class MetalinkError(ValueError):
    pass


@attr.s(auto_attribs=True, frozen=True)
class Metalink:
    filename: str
    size: int
    sha256: str
    urls: tuple[str, ...]


def _priority(raw: str | None) -> int:
    if raw is None or not raw.isdigit():
        return 1_000_000
    return int(raw)


def parse_metalink(text: str | bytes, allow_insecure: bool) -> Metalink:
    try:
        root = parse_xml(text)
    except ParseError as error:
        raise MetalinkError(f"malformed metalink: {error}") from error
    if local_name(root) != "metalink":
        raise MetalinkError("not a metalink document")
    files = children(root, "file")
    if len(files) != 1:
        raise MetalinkError(f"expected exactly one file in metalink, found {len(files)}")
    file = files[0]
    filename = file.get("name", "")
    if not ZIM_FILENAME_RE.match(filename):
        raise MetalinkError(f"refusing unexpected file name {filename!r}")
    size_text = child_text(file, "size")
    if not size_text.isdigit() or int(size_text) <= 0:
        raise MetalinkError("metalink has no valid size")
    sha256 = ""
    for hash_element in children(file, "hash"):
        if hash_element.get("type") == "sha-256" and hash_element.text:
            sha256 = hash_element.text.strip().lower()
    if not SHA256_RE.match(sha256):
        raise MetalinkError("metalink has no sha-256 hash")
    allowed_schemes = ("https://", "http://") if allow_insecure else ("https://",)
    ranked: list[tuple[int, int, str]] = []
    for index, url_element in enumerate(children(file, "url")):
        url = (url_element.text or "").strip()
        if url.startswith(allowed_schemes):
            ranked.append((_priority(url_element.get("priority")), index, url))
    if not ranked:
        raise MetalinkError("metalink lists no usable download mirrors")
    urls = tuple(url for _, _, url in sorted(ranked))
    return Metalink(filename=filename, size=int(size_text), sha256=sha256, urls=urls)
