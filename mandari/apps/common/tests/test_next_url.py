# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Absicherung von ``safe_next_url`` (Open-Redirect-Schutz an der Annahmestelle).

Gegenstück im Frontend: ``frontend/js/test/navigation.test.mjs``.
"""

import pytest
from django.test import RequestFactory

from apps.common.next_url import safe_next_url


@pytest.fixture
def request_factory() -> RequestFactory:
    return RequestFactory()


@pytest.mark.parametrize(
    "candidate",
    [
        "/work/beispiel/dashboard/",
        "/accounts/login/?next=/work/",
        "/pfad/mit/ümlaut/",
        # Ein einzelner Backslash ist für den Browser ein Pfad auf der eigenen
        # Herkunft (/example.org/phish) und damit unbedenklich.
        "\\example.org/phish",
    ],
)
def test_relative_ziele_bleiben_erhalten(request_factory: RequestFactory, candidate: str) -> None:
    request = request_factory.get("/")
    assert safe_next_url(request, candidate) == candidate


@pytest.mark.parametrize(
    "candidate",
    [
        "https://example.org/phish",
        "//example.org/phish",
        "http://example.org",
        "javascript:alert(1)",
        # Zwei Backslashes lesen Browser wie //, also fremde Herkunft.
        "\\\\example.org/phish",
    ],
)
def test_fremde_ziele_werden_abgewiesen(request_factory: RequestFactory, candidate: str) -> None:
    request = request_factory.get("/")
    assert safe_next_url(request, candidate) == ""


def test_eigener_host_absolut_ist_erlaubt(request_factory: RequestFactory) -> None:
    request = request_factory.get("/")
    ziel = f"http://{request.get_host()}/work/"
    assert safe_next_url(request, ziel) == ziel


@pytest.mark.parametrize("candidate", ["", None, 42, [], {"next": "/x/"}])
def test_leere_und_falsche_typen_liefern_den_ausweichwert(request_factory: RequestFactory, candidate: object) -> None:
    request = request_factory.get("/")
    assert safe_next_url(request, candidate) == ""


def test_ausweichwert_wird_zurueckgegeben(request_factory: RequestFactory) -> None:
    request = request_factory.get("/")
    assert safe_next_url(request, "https://example.org/phish", fallback="/admin/") == "/admin/"
    assert safe_next_url(request, None, fallback="/admin/") == "/admin/"
