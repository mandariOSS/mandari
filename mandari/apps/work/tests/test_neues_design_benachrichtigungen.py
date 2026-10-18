# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Benachrichtigungen im neuen Erscheinungsbild (Issue #951, Teilpaket 4).

Mit eingeschaltetem Schalter rendert NotificationCenterView work/notifications/neu/liste.html. Geprüft werden
Gleichstand (apps.work.tests.gleichstand), der Reiter „Ungelesen“ (filtert jetzt wirklich), Seitenwahl, Schalter und
Rückweg, Rechte und die Kopplung von „Als gelesen markieren“ über Datenattribute statt einer Abstandsklasse.
"""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.urls import reverse

from apps.common.tests.factories import MembershipFactory, UserFactory
from apps.work.notifications.models import Notification
from apps.work.notifications.services import NotificationHub
from apps.work.tests.gleichstand import vergleiche

pytestmark = pytest.mark.django_db

VORLAGE = Path(settings.BASE_DIR) / "templates" / "work" / "notifications" / "neu" / "liste.html"


@pytest.fixture
def org(org: Any) -> Any:
    org.work_new_design = True
    org.save(update_fields=["work_new_design"])
    return org


@pytest.fixture
def person(org: Any, make_member: Any) -> Any:
    return make_member(org, ["dashboard.view"], email="vera@example.org")


@pytest.fixture
def kollegin(org: Any, make_member: Any) -> Any:
    return make_member(org, ["dashboard.view"], email="mira@example.org")


def _benachrichtigung(empfaenger: Any, titel: str, gelesen: bool = False, **kwargs: Any) -> Notification:
    return Notification.objects.create(
        recipient=empfaenger,
        notification_type=kwargs.pop("typ", "task_assigned"),
        title=titel,
        message=kwargs.pop("text", "Bitte ansehen."),
        is_read=gelesen,
        **kwargs,
    )


@pytest.fixture
def bestand(org: Any, person: Any, kollegin: Any) -> list[Notification]:
    return [
        _benachrichtigung(person, "Neue Aufgabe & mehr", actor=kollegin, link=f"/work/{org.slug}/tasks/"),
        _benachrichtigung(person, "Freigabe angefragt", typ="motion_approval_req"),
        _benachrichtigung(person, "Sitzung morgen", gelesen=True, typ="meeting_reminder", link="/work/x/"),
    ]


def _url(org: Any) -> str:
    return reverse("work:notifications", kwargs={"org_slug": org.slug})


def test_gleichstand(org: Any, person: Any, bestand: list[Notification], client_for: Any) -> None:
    client = client_for(person.user)
    # Die neue Liste hängt „Als gelesen markieren“ an ein Datenattribut statt an die Abstandsklasse .p-4
    ausnahmen = {"Aktion data-parent=.p-4"}
    neu, antwort = vergleiche(client, _url(org), ausnahmen)
    html = antwort.content.decode()
    assert "Aktion data-parent=[data-benachrichtigung]" in {f"Aktion {a}" for a in neu.aktionen}
    assert "2 ungelesen von 3." in html and "Neue Aufgabe &amp; mehr" in html
    assert html.count("data-ungelesen-punkt") == 4  # Punkt und Hinweis für Bildschirmleser je ungelesener
    assert "Aufgabe zugewiesen" in html and "Von " in html
    vergleiche(client, _url(org) + "?filter=unread", ausnahmen)
    vergleiche(client, _url(org) + "?filter=all", ausnahmen)


def test_reiter_ungelesen_zeigt_nur_ungelesene(
    org: Any, person: Any, bestand: list[Notification], client_for: Any
) -> None:
    client = client_for(person.user)
    for ansicht in ({}, {"ansicht": "bisher"}):
        antwort = client.get(_url(org), {"filter": "unread", **ansicht})
        titel = {n.title for n in antwort.context["notifications"]}
        assert titel == {"Neue Aufgabe & mehr", "Freigabe angefragt"}
    html = client.get(_url(org), {"filter": "unread"}).content.decode()
    assert 'aria-current="page"' in html and "Sitzung morgen" not in html


class _Links(HTMLParser):
    """Links mit hx-post (Als gelesen markieren) und ihr data-after-request."""

    def __init__(self) -> None:
        super().__init__()
        self.links: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "a" and a.get("hx-post") and a.get("href"):
            self.links[a["href"] or ""] = a.get("data-after-request") or ""


def test_anzeigen_folgt_dem_link(org: Any, person: Any, bestand: list[Notification], client_for: Any) -> None:
    # htmx 2 unterdrückt bei hx-post an einem Link das Folgen; data-after-request="follow-href" holt es nach
    client = client_for(person.user)
    for ansicht in ({}, {"ansicht": "bisher"}):
        sammler = _Links()
        sammler.feed(client.get(_url(org), ansicht).content.decode())
        assert sammler.links == {f"/work/{org.slug}/tasks/": "follow-href", "/work/x/": "follow-href"}


def test_seitenwahl_und_leerzustand(org: Any, person: Any, client_for: Any) -> None:
    client = client_for(person.user)
    assert "Sie haben noch keine Benachrichtigungen erhalten." in client.get(_url(org)).content.decode()
    for i in range(22):
        _benachrichtigung(person, f"Nachricht {i}", gelesen=i % 2 == 0)
    erste = client.get(_url(org))
    assert erste.context["has_more"] and "Ältere Benachrichtigungen" in erste.content.decode()
    ungelesen = client.get(_url(org), {"filter": "unread"})
    assert not ungelesen.context["has_more"]  # 11 ungelesene passen auf eine Seite
    assert "?page=2&amp;filter=unread" not in ungelesen.content.decode()
    zweite = client.get(_url(org), {"page": 2})
    assert len(zweite.context["notifications"]) == 2


def test_alle_gelesen_nur_mit_ungelesenen(org: Any, person: Any, client_for: Any) -> None:
    client = client_for(person.user)
    _benachrichtigung(person, "Gelesen", gelesen=True)
    assert "Alle als gelesen markieren" not in client.get(_url(org)).content.decode()
    _benachrichtigung(person, "Neu")
    NotificationHub.invalidate_count_cache(person)  # type: ignore[no-untyped-call]  # Zahl der Ungelesenen im Cache
    assert "Alle als gelesen markieren" in client.get(_url(org)).content.decode()


def test_schalter_rueckweg_und_rechte(org: Any, person: Any, bestand: list[Notification], client_for: Any) -> None:
    konto = UserFactory(email="gast@example.org")  # type: ignore[no-untyped-call]
    gast: Any = MembershipFactory(user=konto, organization=org, is_guest=True)  # type: ignore[no-untyped-call]
    for mitglied in (person, gast):
        client = client_for(mitglied.user)
        neu, bisher = client.get(_url(org)), client.get(_url(org), {"ansicht": "bisher"})
        assert neu.status_code == bisher.status_code == 200
        assert 'data-gestaltung="neu"' in neu.content.decode()
        assert 'data-gestaltung="neu"' not in bisher.content.decode()
    org.work_new_design = False
    org.save(update_fields=["work_new_design"])
    aus = client_for(person.user).get(_url(org))
    assert "work/notifications/center.html" in {t.name for t in aus.templates}


def test_vorlage_ohne_farbkreise_und_kaesten() -> None:
    text = VORLAGE.read_text(encoding="utf-8")
    assert (
        "<c-rahmen.kopfband" in text
        and "<c-rahmen.reiter" in text
        and "{% block gestaltung %}neu{% endblock %}" in text
    )
    assert "notification.color" not in text and "rounded-xl" not in text and ' style="' not in text
    assert 'data-parent="[data-benachrichtigung]"' in text and ".p-4" not in text
