# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Listen in Seiten wie in OParl 1.1 (Abschnitt „Objektlisten und Paginierung“), für alle Bereiche.

Eine Seite ist ein Objekt mit ``data`` (die Einträge), ``pagination`` (``totalElements``,
``elementsPerPage``, ``currentPage``, ``totalPages``) und ``links`` (``first``, ``prev``, ``next``,
``last``, ``self``). Dieselben Verweise stehen außer ``self`` im ``Link``-Header (RFC 8288), damit
Abnehmer blättern können, ohne den Inhalt zu lesen.

- ``list_envelope``: Seite eines Django-``Paginator`` samt ``Link``-Header.
- ``page_schema``: Schema einer Seite für OpenAPI-Beschreibungen.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final
from urllib.parse import urlencode

from django.core.paginator import Page, Paginator


def list_envelope(
    base_url: str,
    filters: Mapping[str, str],
    paginator: Paginator[Any],
    page: Page[Any],
    data: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, str]]:
    """
    Hülle einer Seite samt ``Link``-Header: ``(envelope, headers)``.

    ``filters`` sind die Parameter der Anfrage, wie der Abnehmer sie geschickt hat; sie bleiben in den
    Blätter-Links erhalten. Seite 1 hat keinen ``page``-Parameter (eine Schreibweise je Adresse).
    """

    def page_link(number: int) -> str:
        params: dict[str, Any] = dict(filters)
        if number > 1:
            params["page"] = number
        return f"{base_url}?{urlencode(params)}" if params else base_url

    links = {"first": page_link(1), "self": page_link(page.number)}
    if page.has_previous():
        links["prev"] = page_link(page.number - 1)
    if page.has_next():
        links["next"] = page_link(page.number + 1)
    links["last"] = page_link(paginator.num_pages)

    envelope = {
        "data": data,
        "pagination": {
            "totalElements": paginator.count,
            "elementsPerPage": paginator.per_page,
            "currentPage": page.number,
            "totalPages": paginator.num_pages,
        },
        "links": links,
    }
    headers = {"Link": ", ".join(f'<{url}>; rel="{rel}"' for rel, url in links.items() if rel != "self")}
    return envelope, headers


_URL: Final[dict[str, Any]] = {"type": "string", "format": "uri"}

#: Schema von ``pagination``
PAGINATION_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "totalElements": {"type": "integer", "minimum": 0},
        "elementsPerPage": {"type": "integer", "minimum": 1},
        "currentPage": {"type": "integer", "minimum": 1},
        "totalPages": {"type": "integer", "minimum": 1},
    },
}

#: Schema von ``links``
LINKS_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": dict.fromkeys(("first", "prev", "next", "last", "self"), _URL),
}


def page_schema(items: Mapping[str, Any], *, pagination: str, links: str) -> dict[str, Any]:
    """
    Schema einer Seite mit Einträgen nach ``items``; ``pagination`` und ``links`` sind Verweise (``$ref``)
    auf ``PAGINATION_SCHEMA`` und ``LINKS_SCHEMA`` in der Beschreibung.
    """
    return {
        "type": "object",
        "required": ["data", "pagination", "links"],
        "properties": {
            "data": {"type": "array", "items": dict(items)},
            "pagination": {"$ref": pagination},
            "links": {"$ref": links},
        },
    }
