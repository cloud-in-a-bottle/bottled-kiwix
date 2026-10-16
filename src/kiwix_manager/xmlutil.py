from xml.etree.ElementTree import Element

import defusedxml.ElementTree


def parse_xml(text: str | bytes) -> Element:
    root: Element = defusedxml.ElementTree.fromstring(text)
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
