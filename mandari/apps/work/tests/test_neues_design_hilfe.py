# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Hilfe und Support im neuen Erscheinungsbild (Issue #951, Teilpaket 4): Tickets, Ticket, Neues Ticket.

Mit eingeschaltetem Schalter rendern SupportListView, SupportCreateView und SupportDetailView je eine neue Vorlage aus
work/support/neu/. Das Nachladen der Nachrichten (SupportTicketMessagesPartialView) liefert die neue Fassung nur, wenn
die neue Seite sie mit der Kopfzeile X-Work-Gestaltung anfordert. Geprüft werden Gleichstand, das Nachladen, die
Hinweise des Rahmens auf der Ticketseite, Schalter und Rückweg, Rechte und die Bausteine.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.common.tests.factories import MembershipFactory, UserFactory
from apps.work.support.models import SupportTicket, SupportTicketAttachment, SupportTicketMessage
from apps.work.tests.gleichstand import funktionen, vergleiche

pytestmark = pytest.mark.django_db

VORLAGEN = Path(settings.BASE_DIR) / "templates" / "work" / "support" / "neu"
RECHTE = ["dashboard.view", "support.view", "support.create"]
#: Der Statusfilter ist ein GET-Formular mit demselben Parameter ``status`` statt eines Selects mit data-filter-param
AUSNAHMEN_LISTE = {"Aktion data-filter-param=status"}


@pytest.fixture
def org(org: Any) -> Any:
    org.work_new_design = True
    org.save(update_fields=["work_new_design"])
    return org


@pytest.fixture(autouse=True)
def _medien(settings: Any, tmp_path: Path) -> None:
    settings.MEDIA_ROOT = tmp_path


@pytest.fixture
def person(org: Any, make_member: Any) -> Any:
    return make_member(org, RECHTE, email="vera@example.org")


def _ticket(org: Any, person: Any, betreff: str, status: str = "open", **kwargs: Any) -> SupportTicket:
    ticket = SupportTicket(organization=org, subject=betreff, status=status, created_by=person, **kwargs)
    ticket.set_description_encrypted("Seit gestern fehlt die Sitzung.\nZweite Zeile.")  # type: ignore[attr-defined]
    ticket.save()
    return ticket


@pytest.fixture
def ticket(org: Any, person: Any) -> SupportTicket:
    ticket = _ticket(org, person, "Kalender & Feed", status="waiting", priority="urgent", category="bug")
    stab: Any = UserFactory(email="stab@example.org", is_staff=True)  # type: ignore[no-untyped-call]
    antwort = SupportTicketMessage(ticket=ticket, author_staff=stab)
    antwort.set_content_encrypted("Ist der Ausschuss ausgewählt?")  # type: ignore[attr-defined]
    antwort.save()
    eigene = SupportTicketMessage(ticket=ticket, author_membership=person)
    eigene.set_content_encrypted("Ja.")  # type: ignore[attr-defined]
    eigene.save()
    SupportTicketAttachment.objects.create(
        ticket=ticket,
        file=SimpleUploadedFile("bild.png", b"x"),
        filename="bild.png",
        mime_type="image/png",
        file_size=1,
    )
    SupportTicketAttachment.objects.create(
        ticket=ticket,
        message=eigene,
        file=SimpleUploadedFile("log.txt", b"x"),
        filename="log.txt",
        mime_type="text/plain",
        file_size=1,
    )
    return ticket


def _url(name: str, org: Any, **kwargs: Any) -> str:
    return reverse(f"work:{name}", kwargs={"org_slug": org.slug, **kwargs})


def test_gleichstand_tickets(org: Any, person: Any, ticket: SupportTicket, client_for: Any) -> None:
    _ticket(org, person, "Frage", status="closed")
    client = client_for(person.user)
    neu, antwort = vergleiche(client, _url("support", org), AUSNAHMEN_LISTE)
    html = antwort.content.decode()
    assert "Kalender &amp; Feed" in html and "Dringend" in html and "Wartet auf Antwort" in html
    assert any(z.startswith("GET /work/") and "status" in f for z, f in neu.formulare)
    assert "Link mailto:support@mandari.de" in neu.alles()
    vergleiche(client, _url("support", org) + "?status=closed", AUSNAHMEN_LISTE)
    _, leer = vergleiche(client, _url("support", org) + "?status=escalated", AUSNAHMEN_LISTE)
    assert "Keine Tickets mit diesem Status." in leer.content.decode()


def test_gleichstand_neues_ticket(org: Any, person: Any, client_for: Any) -> None:
    neu, antwort = vergleiche(client_for(person.user), _url("support_create", org))
    html = antwort.content.decode()
    assert 'x-data="ticketForm"' in html and 'enctype="multipart/form-data"' in html
    assert "Ticket erstellen" in html


def test_gleichstand_ticket_offen_und_geschlossen(
    org: Any, person: Any, ticket: SupportTicket, client_for: Any
) -> None:
    client = client_for(person.user)
    neu, antwort = vergleiche(client, _url("support_detail", org, ticket_id=ticket.id))
    html = antwort.content.decode()
    assert "Ist der Ausschuss ausgewählt?" in html and "Kalender &amp; Feed" in html
    assert 'hx-headers=\'{"X-Work-Gestaltung": "neu"}\'' in html and 'x-data="ticketVerlauf"' in html
    assert "bild.png" in html and "log.txt" in html

    ticket.status = "closed"
    ticket.save(update_fields=["status"])
    _, geschlossen = vergleiche(client, _url("support_detail", org, ticket_id=ticket.id))
    assert 'data-testid="ticket-geschlossen"' in geschlossen.content.decode()


def test_ticketseite_zeigt_hinweise_des_rahmens_nicht_die_nachrichten(
    org: Any, person: Any, ticket: SupportTicket, client_for: Any
) -> None:
    client = client_for(person.user)
    antwort = client.post(_url("support_detail", org, ticket_id=ticket.id), {"action": "reply", "content": "Danke"})
    assert antwort.status_code == 302
    html = client.get(_url("support_detail", org, ticket_id=ticket.id)).content.decode()
    hinweise = json.loads(re.search(r'id="django-messages">(.*?)</script>', html, re.S).group(1))  # type: ignore[union-attr]
    assert [h["message"] for h in hinweise] == ["Ihre Nachricht wurde gesendet."]


def test_nachladen_liefert_die_fassung_der_anfragenden_seite(
    org: Any, person: Any, ticket: SupportTicket, client_for: Any
) -> None:
    client = client_for(person.user)
    url = _url("support_messages_partial", org, ticket_id=ticket.id)
    neu = client.get(url, {"count": 0}, HTTP_X_WORK_GESTALTUNG="neu")
    bisher = client.get(url, {"count": 0})
    assert neu.status_code == bisher.status_code == 200 and neu["X-Message-Count"] == "2"
    assert 'data-testid="ticket-nachrichten"' in neu.content.decode()
    assert 'data-testid="ticket-nachrichten"' not in bisher.content.decode()
    assert {t.name for t in bisher.templates} >= {"work/support/partials/message_thread.html"}
    assert client.get(url, {"count": 2}, HTTP_X_WORK_GESTALTUNG="neu").status_code == 204
    # Beide Fassungen bieten dieselben Downloads an
    alt_f = funktionen(bisher.content.decode(), url, nur_main=False)
    neu_f = funktionen(neu.content.decode(), url, nur_main=False)
    assert alt_f.links <= neu_f.links


def test_schalter_rueckweg_und_rechte(
    org: Any, person: Any, ticket: SupportTicket, make_member: Any, client_for: Any
) -> None:
    fremd = make_member(org, RECHTE, email="fremd@example.org")
    ohne = make_member(org, ["dashboard.view"], email="ohne@example.org")
    konto = UserFactory(email="gast@example.org")  # type: ignore[no-untyped-call]
    gast: Any = MembershipFactory(user=konto, organization=org, is_guest=True)  # type: ignore[no-untyped-call]
    seiten = (_url("support", org), _url("support_create", org), _url("support_detail", org, ticket_id=ticket.id))
    for mitglied in (person, fremd, ohne, gast):
        client = client_for(mitglied.user)
        for url in seiten:
            neu, bisher = client.get(url), client.get(url, {"ansicht": "bisher"})
            assert neu.status_code == bisher.status_code, (
                url,
                mitglied.user.email,
                neu.status_code,
                bisher.status_code,
            )
            assert neu.get("Location") == bisher.get("Location")
    org.work_new_design = False
    org.save(update_fields=["work_new_design"])
    client = client_for(person.user)
    for url, vorlage in zip(seiten, ("list.html", "create.html", "detail.html"), strict=True):
        assert f"work/support/{vorlage}" in {t.name for t in client.get(url).templates}


def test_vorlagen_ohne_kaesten_chips_und_inline_skript() -> None:
    for datei in sorted(VORLAGEN.glob("*.html")):
        text = datei.read_text(encoding="utf-8")
        assert "rounded-xl" not in text and "rounded-full text-xs" not in text and ' style="' not in text, datei.name
        assert "<script" not in text and "shadow" not in text, datei.name
    for seite in ("liste.html", "neu.html", "ticket.html"):
        text = (VORLAGEN / seite).read_text(encoding="utf-8")
        assert "<c-rahmen.kopfband" in text and "{% block gestaltung %}neu{% endblock %}" in text, seite
    assert '<c-form.datei name="attachments"' in (VORLAGEN / "neu.html").read_text(encoding="utf-8")
    assert '<c-form.datei name="attachments"' in (VORLAGEN / "ticket.html").read_text(encoding="utf-8")
