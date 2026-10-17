from xml.etree.ElementTree import Element
from xml.etree.ElementTree import ParseError

import defusedxml.ElementTree
from defusedxml import DefusedXmlException


class XmlError(ValueError):
    pass


def parse_xml(text: str | bytes) -> Element:
    try:
        root: Element = defusedxml.ElementTree.fromstring(text)
    except (ParseError, DefusedXmlException) as error:
        raise XmlError(str(error)) from error
    return root


def local_name(element: Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def children(element: Element, name: str) -> list[Element]:
    return [child for child in element if local_name(child) == name]


def child(element: Element, name: str) -> Element | None:
    for candidate in element:
        if local_name(candidate) == name:
            return candidate
    return None


def child_text(element: Element, name: str) -> str:
    found = child(element, name)
    if found is None or found.text is None:
        return ""
    return found.text.strip()


def parse_int(text: str | None) -> int:
    """A non-negative integer attribute or element value, or 0 when absent or malformed."""
    return int(text) if text and text.isdigit() else 0
