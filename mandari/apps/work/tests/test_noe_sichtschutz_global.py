# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sichtschutz für Nichtöffentliches als ein Schalter für ganz Work (Issue #1201).

- Zustand als Cookie mit der Marke der Anmeldung (``apps/work/sichtschutz.py``): Standard verdeckt, jede Anmeldung
  beginnt verdeckt (neue Kennung), fremde oder falsche Marken gelten nicht.
- Jede Work-Seite kommt schon richtig vom Server: Klasse ``noe-verdeckt`` am ``<body>`` beider Rahmen, Marke der
  Anmeldung für den Alpine-Store ``noe``; Knopf in der Kopfleiste nur für Personen, die Nichtöffentliches sehen dürfen.
- Die übrigen Ansichten mit nichtöffentlichen TOPs: weichgezeichnete Titel mit Ersatz für Vorleser, Inhalte hinter der
  Abdeckung. Die Sitzungsansicht prüft ``apps/work/faction/tests/test_noe_sichtschutz.py``.

Nur Sichtschutz: Wer keine Rechte hat, bekommt die Inhalte wie bisher nicht (die Prüfungen dort bleiben unverändert).
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any, cast

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.work import sichtschutz
from apps.work.faction.models import FactionAgendaItem, FactionAttendance, FactionMeeting
from apps.work.tasks.models import Task

pytestmark = pytest.mark.django_db

VORSITZ = [
    "faction.view_public",
    "faction.view_non_public",
    "faction.manage",
    "faction.start",
    "protocols.create",
    "protocols.edit",
    "voting.participate",
    "tasks.view",
    "tasks.create",
    "dashboard.view",
    "faction.view_audit",
]
TITEL = "Personalfrage Geschäftsstelle"
BESCHREIBUNG = "Gespräch mit der Verwaltung am Donnerstag."
#: So steht ein NÖ-Titel in allen Ansichten: weichgezeichnet, für Vorleser ausgeblendet und ersetzt
VERDECKT = (
    f'<span class="fs-noe" aria-hidden="true" :aria-hidden="!$store.noe.sichtbar">{TITEL}</span>'
    '<span class="sr-only fs-noe-ersatz">Nichtöffentlicher TOP, ausgeblendet</span>'
)


@pytest.fixture
def vorsitz(org: Any, make_member: Any) -> Any:
    m = make_member(org, VORSITZ, email="vorsitz-1201@example.org")
    m.is_sworn_in = True
    m.save(update_fields=["is_sworn_in"])
    return m


@pytest.fixture
def mitglied(org: Any, make_member: Any) -> Any:
    return make_member(
        org, ["faction.view_public", "voting.participate", "dashboard.view"], email="mitglied-1201@example.org"
    )


@pytest.fixture
def neu(org: Any) -> Any:
    org.work_new_design = True
    org.save(update_fields=["work_new_design"])
    return org


def _sitzung(org: Any, vorsitz: Any, status: str) -> tuple[FactionMeeting, FactionAgendaItem]:
    meeting = FactionMeeting.objects.create(
        organization=org,
        title=f"Fraktionssitzung ({status})",
        start=timezone.now() + (timedelta(days=3) if status == "planned" else -timedelta(hours=2)),
        status=status,
        created_by=vorsitz,
    )
    FactionAttendance.objects.create(meeting=meeting, membership=vorsitz, status="invited")
    FactionAgendaItem.objects.create(meeting=meeting, number="1", title="Haushalt 2027", visibility="public", order=1)
    noe = FactionAgendaItem.objects.create(meeting=meeting, number="NÖ 1", title=TITEL, visibility="internal", order=2)
    cast(Any, noe).set_description_encrypted(BESCHREIBUNG)
    noe.save()
    return meeting, noe


def _seite(client: Client, url: str, **anfrage: str) -> str:
    antwort = client.get(url, anfrage)
    assert antwort.status_code == 200, (url, antwort.status_code)
    return str(antwort.content.decode())


def _body(html: str) -> str:
    treffer = re.search(r"<body[^>]*>", html)
    assert treffer, "kein body"
    return treffer.group(0)


def _detail(org: Any, meeting: FactionMeeting) -> str:
    return reverse("work:faction_detail", kwargs={"org_slug": org.slug, "meeting_id": meeting.pk})


# ---------------------------------------------------------------------------
# Zustand und Endpunkt
# ---------------------------------------------------------------------------


def _einblenden(client: Client) -> None:
    """Wie der Store im Browser: Cookie mit der Marke der Anmeldung setzen."""
    session = client.session
    assert session.session_key
    client.cookies[sichtschutz.COOKIE] = sichtschutz.marke_fuer(session.session_key, session[sichtschutz.ANMELDUNG])


def test_standard_verdeckt_und_einblenden_per_cookie(org: Any, neu: Any, vorsitz: Any, client_for: Any) -> None:
    client = client_for(vorsitz.user)
    dashboard = reverse("work:dashboard", kwargs={"org_slug": org.slug})
    body = _body(_seite(client, dashboard))
    marke = sichtschutz.marke_fuer(cast(str, client.session.session_key), client.session[sichtschutz.ANMELDUNG])
    assert "noe-verdeckt" in body and f'data-noe-marke="{marke}"' in body
    assert re.fullmatch(r"[0-9a-f]{64}", marke)

    _einblenden(client)
    html = _seite(client, dashboard)
    assert "noe-verdeckt" not in _body(html)
    # Knopf vom Server schon im richtigen Zustand: gedrückt, Kurzinfo nennt den nächsten Schritt
    knopf = re.search(r'<button[^>]*data-testid="noe-knopf"[^>]*>', html, flags=re.S)
    assert knopf and 'aria-pressed="true"' in knopf.group(0) and 'title="Nichtöffentliches verdecken"' in knopf.group(0)

    # Falscher Wert oder gelöschtes Cookie: verdeckt
    client.cookies[sichtschutz.COOKIE] = "0" * len(marke)
    assert "noe-verdeckt" in _body(_seite(client, dashboard))
    _einblenden(client)
    del client.cookies[sichtschutz.COOKIE]
    assert "noe-verdeckt" in _body(_seite(client, dashboard))


def test_jede_anmeldung_beginnt_verdeckt(org: Any, neu: Any, vorsitz: Any, client_for: Any) -> None:
    client = client_for(vorsitz.user)
    dashboard = reverse("work:dashboard", kwargs={"org_slug": org.slug})
    _einblenden(client)
    assert "noe-verdeckt" not in _body(_seite(client, dashboard))
    # Erneute Anmeldung desselben Kontos (Django behält dabei den Sitzungsschlüssel): neue Kennung, neue Marke
    schluessel = client.session.session_key
    client.force_login(vorsitz.user)
    assert client.session.session_key == schluessel
    assert sichtschutz.COOKIE in client.cookies
    assert "noe-verdeckt" in _body(_seite(client, dashboard))
    # Abmelden und wieder anmelden ebenso
    _einblenden(client)
    client.logout()
    client.force_login(vorsitz.user)
    assert "noe-verdeckt" in _body(_seite(client, dashboard))


def test_marke_einer_fremden_anmeldung_gilt_nicht(org: Any, neu: Any, vorsitz: Any, client_for: Any) -> None:
    dashboard = reverse("work:dashboard", kwargs={"org_slug": org.slug})
    andere = client_for(vorsitz.user)
    client = client_for(vorsitz.user)
    client.cookies[sichtschutz.COOKIE] = sichtschutz.marke_fuer(
        cast(str, andere.session.session_key), andere.session[sichtschutz.ANMELDUNG]
    )
    assert "noe-verdeckt" in _body(_seite(client, dashboard))
    # Ohne Anmeldung keine Marke
    assert sichtschutz.marke(None) == "" and not sichtschutz.ist_sichtbar(None)


def test_einblenden_aendert_keine_rechte(org: Any, neu: Any, mitglied: Any, vorsitz: Any, client_for: Any) -> None:
    """Auch eingeblendet bekommt, wer keine Rechte hat, den nichtöffentlichen Teil nicht."""
    meeting, _noe = _sitzung(org, vorsitz, "ongoing")
    client = client_for(mitglied.user)
    _einblenden(client)
    html = _seite(client, _detail(org, meeting), ansicht="bisher")
    assert "noe-verdeckt" not in _body(html)
    assert TITEL not in html and BESCHREIBUNG not in html


# ---------------------------------------------------------------------------
# Knopf in der Kopfleiste
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("neues_design", [True, False], ids=["rahmen-neu", "rahmen-bisher"])
def test_knopf_nur_mit_recht(
    org: Any, vorsitz: Any, mitglied: Any, make_member: Any, client_for: Any, neues_design: bool
) -> None:
    org.work_new_design = neues_design
    org.save(update_fields=["work_new_design"])
    dashboard = reverse("work:dashboard", kwargs={"org_slug": org.slug})

    html = _seite(client_for(vorsitz.user), dashboard)
    knopf = re.search(r'<button[^>]*data-testid="noe-knopf".*?</button>', html, flags=re.S)
    assert knopf, "Knopf fehlt"
    knopf_html = knopf.group(0)
    assert '@click="$store.noe.umschalten()"' in knopf_html
    assert 'aria-pressed="false"' in knopf_html and ':aria-pressed="$store.noe.sichtbar.toString()"' in knopf_html
    assert 'aria-label="Nichtöffentliches einblenden"' in knopf_html
    assert 'title="Nichtöffentliches einblenden"' in knopf_html
    assert 'data-lucide="eye-off"' in knopf_html and 'data-lucide="eye"' in knopf_html
    # Beide Rahmen: Klasse und Marke am body
    assert "noe-verdeckt" in _body(html) and 'data-noe-marke="' in _body(html)

    # Ohne Recht auf Nichtöffentliches: kein Knopf (der Zustand gilt trotzdem)
    assert 'data-testid="noe-knopf"' not in _seite(client_for(mitglied.user), dashboard)
    # Recht ohne Vereidigung reicht nicht (dieselbe Prüfung wie für den nichtöffentlichen Teil)
    unvereidigt = make_member(org, VORSITZ, email="unvereidigt-1201@example.org")
    assert 'data-testid="noe-knopf"' not in _seite(client_for(unvereidigt.user), dashboard)


# ---------------------------------------------------------------------------
# Ansichten außerhalb der Sitzungsansicht
# ---------------------------------------------------------------------------


def test_bisherige_ansicht(org: Any, neu: Any, vorsitz: Any, client_for: Any) -> None:
    meeting, noe = _sitzung(org, vorsitz, "ongoing")
    client = client_for(vorsitz.user)
    html = _seite(client, _detail(org, meeting), ansicht="bisher")
    assert VERDECKT in html
    # Beschreibung weichgezeichnet, aufgeklappter Inhalt hinter der Abdeckung
    assert re.search(r'class="text-sm text-gray-500 mt-0\.5 fs-noe" aria-hidden="true"[^>]*>' + BESCHREIBUNG, html)
    assert "fs-noe-abdeckung" in html and "Unterpunkte, Protokoll und Bearbeitung sind verdeckt" in html
    assert '@click="$store.noe.einblenden()"' in html

    # Seitenpanel des TOPs: Titelfeld weichgezeichnet und nicht bedienbar, Inhalte abgedeckt
    panel = _seite(
        client,
        reverse("work:faction_item_panel", kwargs={"org_slug": org.slug, "meeting_id": meeting.pk, "item_id": noe.pk}),
    )
    feld = re.search(r'<input type="text" name="title" value="' + TITEL + r'"[^>]*>', panel, flags=re.S)
    assert feld and " inert" in feld.group(0) and "fs-noe" in feld.group(0)
    assert 'x-bind:inert="!$store.noe.sichtbar"' in feld.group(0)
    assert panel.index("fs-noe-abdeckung") < panel.index('<div class="fs-noe-inhalt">')

    # Eingeblendet: dieselben Seiten ohne inert (der Server kennt den Zustand)
    _einblenden(client)
    panel = _seite(
        client,
        reverse("work:faction_item_panel", kwargs={"org_slug": org.slug, "meeting_id": meeting.pk, "item_id": noe.pk}),
    )
    feld = re.search(r'<input type="text" name="title" value="' + TITEL + r'"[^>]*>', panel, flags=re.S)
    assert feld and " inert" not in feld.group(0)


def test_loeschdialog(org: Any, neu: Any, vorsitz: Any, client_for: Any) -> None:
    meeting, noe = _sitzung(org, vorsitz, "planned")
    antwort = client_for(vorsitz.user).post(
        reverse("work:faction_action", kwargs={"org_slug": org.slug, "meeting_id": meeting.pk}),
        {"action": "delete_item_preview", "item_id": str(noe.pk)},
        HTTP_HX_REQUEST="true",
    )
    assert antwort.status_code == 200
    assert f"TOP NÖ 1 „{VERDECKT}“ wird" in antwort.content.decode()


def test_vorbereitung(org: Any, neu: Any, vorsitz: Any, client_for: Any) -> None:
    meeting, noe = _sitzung(org, vorsitz, "planned")
    client = client_for(vorsitz.user)
    html = _seite(client, _detail(org, meeting), ansicht="tagesordnung")
    zeile = re.search(rf'<li class="fs-top[^"]*" id="top-{noe.pk}".*?</li>', html, flags=re.S)
    assert zeile and VERDECKT in zeile.group(0)
    # Aufgeklappt: Felder, Unterlagen und Verlauf hinter der Abdeckung
    html = _seite(client, _detail(org, meeting), ansicht="tagesordnung", top=str(noe.pk))
    detail = html[html.index(f'id="d-{noe.pk}"') :]
    assert detail.index("fs-noe-abdeckung") < detail.index('<div class="fs-noe-inhalt">') < detail.index('name="title"')


def test_protokollansicht(org: Any, neu: Any, vorsitz: Any, client_for: Any) -> None:
    meeting, noe = _sitzung(org, vorsitz, "completed")
    html = _seite(client_for(vorsitz.user), _detail(org, meeting))
    assert 'data-testid="fs-prot-dok"' in html
    # Gliederung und Überschrift des TOPs weichgezeichnet, Inhalte abgedeckt
    assert f"NÖ 1 {VERDECKT}</a>" in html
    abschnitt = html[html.index(f'id="p-{noe.pk}"') :]
    assert VERDECKT in abschnitt[: abschnitt.index("</h3>")]
    assert "Notizen, Einträge, Aufgaben und Beschluss sind verdeckt" in abschnitt
    assert abschnitt.index("fs-noe-abdeckung") < abschnitt.index('<div class="fs-noe-inhalt">')


def test_aufgaben_aus_noe_top(org: Any, neu: Any, vorsitz: Any, client_for: Any) -> None:
    meeting, noe = _sitzung(org, vorsitz, "ongoing")
    aufgabe = Task.objects.create(
        organization=org,
        title=TITEL,
        description=BESCHREIBUNG,
        created_by=vorsitz,
        assigned_to=vorsitz,
        related_faction_meeting=meeting,
        related_faction_agenda_item=noe,
    )
    andere = Task.objects.create(organization=org, title="Saal reservieren", created_by=vorsitz, assigned_to=vorsitz)
    assert aufgabe.noe_top is True and andere.noe_top is False

    client = client_for(vorsitz.user)
    board = _seite(client, reverse("work:tasks", kwargs={"org_slug": org.slug}))
    assert VERDECKT in board and "Saal reservieren" in board
    panel = _seite(client, reverse("work:task_panel", kwargs={"org_slug": org.slug, "task_id": aufgabe.pk}))
    assert "fs-noe-abdeckung" in panel and '<div class="fs-noe-inhalt">' in panel
    # Startseite im bisherigen Erscheinungsbild: „Meine Aufgaben“ ebenso
    org.work_new_design = False
    org.save(update_fields=["work_new_design"])
    assert VERDECKT in _seite(client, reverse("work:dashboard", kwargs={"org_slug": org.slug}))
    assert VERDECKT in _seite(client, reverse("work:tasks", kwargs={"org_slug": org.slug}))


def test_historie_fuer_vereidigte_weichgezeichnet(org: Any, neu: Any, vorsitz: Any, client_for: Any) -> None:
    _sitzung(org, vorsitz, "planned")
    html = _seite(client_for(vorsitz.user), reverse("work:faction_audit", kwargs={"org_slug": org.slug}))
    assert "Nichtöffentlicher Eintrag, ausgeblendet" in html
    assert re.search(r'<div class="fs-noe" aria-hidden="true" :aria-hidden="!\$store\.noe\.sichtbar">', html)
