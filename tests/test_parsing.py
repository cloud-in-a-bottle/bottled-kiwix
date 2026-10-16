import pytest

from kiwix_manager.auth import is_owner
from kiwix_manager.auth import is_same_origin_request
from kiwix_manager.auth import login_url
from kiwix_manager.catalog import parse_entries
from kiwix_manager.catalog import parse_facets
from kiwix_manager.metalink import MetalinkError
from kiwix_manager.metalink import parse_metalink

ENTRIES_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:dc="http://purl.org/dc/terms/">
  <totalResults>65</totalResults>
  <startIndex>24</startIndex>
  <entry>
    <id>urn:uuid:dd8a2bd2-c05b-ac6c-8074-6c30e14803fb</id>
    <title>Wikipedia 100</title>
    <updated>2026-08-20T00:00:00Z</updated>
    <summary>Top one hundred Wikipedia articles</summary>
    <language>eng</language>
    <name>wikipedia_en_100</name>
    <flavour></flavour>
    <category>wikipedia</category>
    <tags>wikipedia;_ftindex:yes</tags>
    <articleCount>5056</articleCount>
    <mediaCount>4122</mediaCount>
    <link rel="http://opds-spec.org/image/thumbnail" href="/catalog/v2/illustration/x/?size=48" type="image/png"/>
    <dc:issued>2026-08-19T00:00:00Z</dc:issued>
    <link rel="http://opds-spec.org/acquisition/open-access" type="application/x-zim"
          href="https://lb.download.kiwix.org/zim/wikipedia/wikipedia_en_100_2026-08.zim.meta4" length="333430784" />
  </entry>
  <entry>
    <id>urn:uuid:not-a-uuid</id>
    <title>Broken</title>
  </entry>
</feed>
"""

METALINK = """<?xml version="1.0" encoding="UTF-8"?>
<metalink xmlns="urn:ietf:params:xml:ns:metalink">
  <file name="wikipedia_en_100_2026-08.zim">
    <size>333429922</size>
    <hash type="md5">e00f3861b76dd768ab73db8fcb757bc2</hash>
    <hash type="sha-256">2EB7FD115BFB8644A08A9482F01F52B507428B1B8A30129FEAD92F7B6715440B</hash>
    <url location="nl" priority="5">https://ftp.nluug.nl/pub/kiwix/zim/wikipedia/wikipedia_en_100_2026-08.zim</url>
    <url location="us" priority="1">https://dumps.wikimedia.org/kiwix/zim/wikipedia/wikipedia_en_100_2026-08.zim</url>
    <url location="xx" priority="2">http://insecure.example/wikipedia_en_100_2026-08.zim</url>
    <url location="yy">ftp://ftp.example/wikipedia_en_100_2026-08.zim</url>
  </file>
</metalink>
"""


def test_parse_entries() -> None:
    page = parse_entries(ENTRIES_FEED)
    assert page.total == 65
    assert page.start == 24
    assert len(page.entries) == 1
    entry = page.entries[0]
    assert entry.id == "dd8a2bd2-c05b-ac6c-8074-6c30e14803fb"
    assert entry.name == "wikipedia_en_100"
    assert entry.flavour == ""
    assert entry.issued == "2026-08-19"
    assert entry.size_bytes == 333430784
    assert entry.article_count == 5056
    assert entry.has_illustration
    assert entry.meta4_url.endswith(".zim.meta4")


def test_parse_facets() -> None:
    feed = """<feed xmlns="http://www.w3.org/2005/Atom" xmlns:dc="http://purl.org/dc/terms/"
        xmlns:thr="http://purl.org/syndication/thread/1.0">
      <entry><title>English</title><dc:language>eng</dc:language><thr:count>1200</thr:count></entry>
      <entry><title>wikipedia</title></entry>
    </feed>"""
    facets = parse_facets(feed, "language")
    assert [(facet.value, facet.label, facet.count) for facet in facets] == [
        ("eng", "English", 1200),
        ("wikipedia", "wikipedia", 0),
    ]


def test_parse_metalink_orders_mirrors_and_drops_insecure() -> None:
    metalink = parse_metalink(METALINK, allow_insecure=False)
    assert metalink.filename == "wikipedia_en_100_2026-08.zim"
    assert metalink.size == 333429922
    assert metalink.sha256 == "2eb7fd115bfb8644a08a9482f01f52b507428b1b8a30129fead92f7b6715440b"
    assert metalink.urls == (
        "https://dumps.wikimedia.org/kiwix/zim/wikipedia/wikipedia_en_100_2026-08.zim",
        "https://ftp.nluug.nl/pub/kiwix/zim/wikipedia/wikipedia_en_100_2026-08.zim",
    )


def test_parse_metalink_allows_http_when_configured() -> None:
    metalink = parse_metalink(METALINK, allow_insecure=True)
    assert "http://insecure.example/wikipedia_en_100_2026-08.zim" in metalink.urls


@pytest.mark.parametrize("name", ["../evil.zim", "/etc/passwd", "file.txt", ".hidden.zim", "a/b.zim", ""])
def test_parse_metalink_rejects_unsafe_filenames(name: str) -> None:
    with pytest.raises(MetalinkError):
        parse_metalink(METALINK.replace("wikipedia_en_100_2026-08.zim", name, 1), allow_insecure=False)


def test_parse_metalink_requires_sha256() -> None:
    without_hash = METALINK.replace('type="sha-256"', 'type="sha-1"')
    with pytest.raises(MetalinkError):
        parse_metalink(without_hash, allow_insecure=False)


def test_parse_metalink_rejects_malformed_xml() -> None:
    with pytest.raises(MetalinkError):
        parse_metalink("<metalink><file", allow_insecure=False)


def test_parse_metalink_rejects_entity_expansion() -> None:
    bomb = '<?xml version="1.0"?><!DOCTYPE m [<!ENTITY a "aaaa">]><metalink>&a;</metalink>'
    with pytest.raises(Exception):  # noqa: B017 - defusedxml raises its own exception types
        parse_metalink(bomb, allow_insecure=False)


def test_is_owner_requires_exact_header() -> None:
    assert is_owner({"x-openhost-is-owner": "true"})
    assert not is_owner({"x-openhost-is-owner": "True"})
    assert not is_owner({})


def test_login_url_targets_zone_and_round_trips() -> None:
    headers = {"x-forwarded-host": "kiwix.example.com", "x-forwarded-proto": "https"}
    assert (
        login_url(headers, "/content/wiki", "a=1")
        == "https://example.com/login?next=https%3A%2F%2Fkiwix.example.com%2Fcontent%2Fwiki%3Fa%3D1"
    )


def test_login_url_keeps_port_and_ignores_bogus_proto() -> None:
    headers = {"x-forwarded-host": "kiwix.lvh.me:8080", "x-forwarded-proto": "javascript"}
    assert login_url(headers, "/", "").startswith(
        "https://lvh.me:8080/login?next=https%3A%2F%2Fkiwix.lvh.me%3A8080%2F"
    )


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"origin": "https://kiwix.example.com"}, True),
        ({"origin": "https://other.example.com"}, False),
        ({"origin": "http://kiwix.example.com"}, False),
        ({"origin": "null"}, False),
        ({"sec-fetch-site": "same-origin"}, True),
        ({"sec-fetch-site": "same-site"}, False),
        ({}, False),
    ],
)
def test_same_origin(headers: dict[str, str], expected: bool) -> None:
    base = {"x-forwarded-host": "kiwix.example.com", "x-forwarded-proto": "https"}
    assert is_same_origin_request({**base, **headers}) is expected
