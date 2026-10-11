# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungen, Fraktionssitzungen und Recherche-Reste im neuen Erscheinungsbild (Issue #951, Teilpaket 2).

Zehn Seiten bekommen hinter dem Schalter ``Organization.work_new_design`` eine eigene Vorlage: Meine Sitzungen,
Kalender, Ladungen der Verwaltung, Sitzungsdetail, Fraktionssitzungen, Änderungshistorie, Nichtöffentliche Unterlage
einlesen und übernehmen, Beschlüsse (mit neuer Detailseite) und Dokumente der Recherche. Geprüft wird:

- Gleichstand: Beide Vorlagen werden mit denselben Daten gerendert; jede Aktion der bisherigen Seite (Links,
  Formulare mit Methode, Ziel und Feldern, htmx, Adressen für Skripte) steht auch auf der neuen – für jede Rolle.
  Ausnahmen stehen mit Begründung bei der Seite.
- Rechte: Jede Rolle bekommt mit und ohne Schalter dieselbe Antwort (Status), die Views bleiben unverändert.
- Schalter: aus → bisherige Vorlage, an → neue, ``?ansicht=bisher`` → bisherige.
- Bausteine statt Handarbeit: Kopfband, keine Kästen, keine Farbchips; der Kalender ohne Inline-Skript und -CSS.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode

import pytest
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from apps.common.tests.factories import MembershipFactory, UserFactory
from apps.session.models import (
    SessionAgendaItem,
    SessionAPIToken,
    SessionMeeting,
    SessionOrganization,
    SessionOrganizationMembership,
    SessionPerson,
    SessionTenant,
)
from apps.session.services import invitation_service
from apps.tenants.models import Membership, Organization
from apps.work.faction.models import FactionAuditLog, FactionMeeting
from apps.work.meetings.models import MeetingPreparation
from apps.work.motions.models import Motion
from apps.work.motions.ris_submission import connect_with_token
from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlFile,
    OParlMeeting,
    OParlOrganization,
    OParlPaper,
    OParlSource,
)

pytestmark = pytest.mark.django_db

RIS = "https://ris.teil2.example/oparl"
EMAIL_RAT = "rita.rat@example.org"
VORLAGEN = Path(settings.BASE_DIR) / "templates"

# ---------------------------------------------------------------------------
# Was eine Seite anbietet
# ---------------------------------------------------------------------------

#: Eine Aktion: Methode (GET, POST, … oder SKRIPT), Adresse und Felder
Aktion = tuple[str, str, tuple[str, ...]]
_HX = ("get", "post", "put", "patch", "delete")
#: Felder mit festem Wert: der Wert gehört zur Aktion („action=confirm“ ist etwas anderes als „action=decline“)
_FESTE_WERTE = {"hidden", "submit", "button"}
_SKRIPT_ADRESSE = re.compile(r"""["'](/work/[^"'\s]+)["']""")


def _intern(adresse: str) -> bool:
    """Adresse dieser Anwendung (auch nur Anfrageparameter wie ``?page=2``), keine Dateien aus static/media."""
    return adresse.startswith("?") or (
        adresse.startswith("/") and not adresse.startswith(("//", "/static/", "/media/"))
    )


def _norm(adresse: str) -> str:
    """Anfrageparameter sortiert und ohne leere Schlüssel: ``?view=all&time=x`` = ``?time=x&view=all``."""
    pfad, _, anfrage = adresse.partition("?")
    if not anfrage:
        return adresse
    teile = sorted((k, v) for k, v in parse_qsl(anfrage, keep_blank_values=True) if k)
    return f"{pfad}?{urlencode(teile)}"


class _Aktionen(HTMLParser):
    """
    Aktionen im Inhalt einer Seite (``<main>``; Kopf und Seitenleiste des Rahmens zählen nicht): Links (GET),
    Formulare mit Methode, Ziel und Feldern (feste Werte mit Wert, Eingaben nur mit Namen), htmx-Aufrufe und Adressen
    für Skripte (``data-*``, Adressen in Skripten).
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: set[Aktion] = set()
        self.formulare: list[tuple[str, str, frozenset[str]]] = []
        self._formular: tuple[str, str] | None = None
        self._felder: set[str] = set()
        self._im_skript = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {name: wert or "" for name, wert in attrs}
        if tag == "a" and _intern(a.get("href", "")):
            self.links.add(("GET", _norm(a["href"]), ()))
        for verb in _HX:
            if a.get(f"hx-{verb}") and tag != "form" and _intern(a[f"hx-{verb}"]):
                self.links.add((verb.upper(), _norm(a[f"hx-{verb}"]), ()))
        for name, wert in a.items():
            if name.startswith("data-") and wert.startswith("/") and _intern(wert):
                self.links.add(("SKRIPT", wert, ()))
        if tag == "form":
            self._formular = (a.get("method", "get").upper(), _norm(a.get("action", "")))
            self._felder = set()
        elif self._formular and tag in ("input", "select", "textarea", "button") and a.get("name"):
            typ = a.get("type", "submit" if tag == "button" else "text").lower()
            if a["name"] != "csrfmiddlewaretoken":
                fest = tag in ("input", "button") and typ in _FESTE_WERTE
                self._felder.add(f"{a['name']}={a.get('value', '')}" if fest else a["name"])
        self._im_skript = tag == "script"

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._formular:
            self.formulare.append((*self._formular, frozenset(self._felder)))
            self._formular = None
        if tag == "script":
            self._im_skript = False

    def handle_data(self, data: str) -> None:
        if self._im_skript:
            self.links.update(("SKRIPT", adresse, ()) for adresse in _SKRIPT_ADRESSE.findall(data))


def _inhalt(html: str) -> str:
    """
    Nur der Inhalt der Seite: ``<main …>…</main>`` in beiden Rahmen, im bisherigen ohne seine Kopfzeile
    (work/base_work_alt_kopf.html mit Glocke und Personenmenü steht dort im ``<main>``, ab „Page Header“ die Seite).
    """
    anfang = html.index("<main")
    ende = html.index("</main>", anfang)
    seite = html.find("<!-- Page Header -->", anfang, ende)
    return html[seite if seite >= 0 else anfang : ende]


def _aktionen(html: str) -> _Aktionen:
    leser = _Aktionen()
    leser.feed(_inhalt(html))
    leser.close()
    return leser


class _Skripte(HTMLParser):
    """Zählt Skripte ohne ``src`` (Inline-Skripte) in einer Vorlage."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.inline = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" and not dict(attrs).get("src"):
            self.inline += 1


def _inline_skripte(vorlage: str) -> int:
    leser = _Skripte()
    leser.feed(vorlage)
    leser.close()
    return leser.inline


def _fehlt(alt: str, neu: str, ausnahmen: set[Aktion]) -> list[str]:
    """Was die bisherige Seite anbietet und die neue nicht: Links und Formulare (die neue darf Felder ergänzen)."""
    bisher, jetzt = _aktionen(alt), _aktionen(neu)
    fehlt = [f"Link {a}" for a in sorted(bisher.links - jetzt.links - ausnahmen)]
    for methode, ziel, felder in bisher.formulare:
        if (methode, ziel, ()) in ausnahmen:
            continue
        if not any(m == methode and z == ziel and felder <= f for m, z, f in jetzt.formulare):
            fehlt.append(f"Formular {methode} {ziel!r} {sorted(felder)}")
    return fehlt


def test_vergleich_erkennt_formulare_felder_und_links() -> None:
    """Der Vergleich unterscheidet feste Werte, normalisiert Anfragen und erlaubt zusätzliche Felder."""
    alt = (
        '<main><a href="?view=all&time=upcoming"></a><a href="/static/x.css"></a>'
        '<form method="post" action="/work/x/r/"><button type="submit" name="action" value="confirm"></button></form>'
        '<form method="get"><input name="q"><input type="hidden" name="view" value="my"></form>'
        "<script>fetch('/work/x/events/')</script></main>"
    )
    leser = _aktionen(alt)
    assert leser.links == {("GET", "?time=upcoming&view=all", ()), ("SKRIPT", "/work/x/events/", ())}
    assert ("POST", "/work/x/r/", frozenset({"action=confirm"})) in leser.formulare
    neu = (
        '<main><a href="?time=upcoming&view=all"></a><div data-ereignisse="/work/x/events/"></div>'
        '<form method="post" action="/work/x/r/"><button name="action" value="confirm"></button></form>'
        '<form method="get"><input name="q"><input type="hidden" name="view" value="my">'
        '<input type="hidden" name="time" value="x"></form></main>'
    )
    assert _fehlt(alt, neu, set()) == []
    anders = neu.replace('value="confirm"', 'value="decline"')
    assert _fehlt(alt, anders, set()) == ["Formular POST '/work/x/r/' ['action=confirm']"]


# ---------------------------------------------------------------------------
# Bestand
# ---------------------------------------------------------------------------


def _kennung(art: str, nummer: Any) -> str:
    return f"{RIS}/{art}/{nummer}"


@dataclass
class Welt:
    org: Organization
    rollen: dict[str, Membership]
    body: OParlBody
    sitzung: OParlMeeting
    fraktionssitzung: FactionMeeting
    tenant: SessionTenant
    beschluss: SessionAgendaItem
    nicht_oeffentlich: SessionAgendaItem
    extra: dict[str, Any] = field(default_factory=dict)


def _schalter(org: Organization, an: bool) -> None:
    Organization.objects.filter(pk=org.pk).update(work_new_design=an)


def _ris(org: Organization) -> tuple[OParlBody, dict[str, OParlOrganization], OParlMeeting]:
    """Kommune mit drei Gremien, Sitzungen in den nächsten Wochen und einer Sitzung mit Tagesordnung und Vorlagen."""
    source = OParlSource.objects.create(name="Teil-2-RIS", url=f"{RIS}/system")
    body = OParlBody.objects.create(external_id=_kennung("bodies", 1), source=source, name="Musterstadt")
    org.body = body
    org.save(update_fields=["body"])
    gremien = {
        name: OParlOrganization.objects.create(
            external_id=_kennung("org", name), body=body, name=name, classification="Ausschuss"
        )
        for name in ("Rat", "Hauptausschuss", "Bauausschuss")
    }
    jetzt = timezone.now()
    sitzung = None
    for tage, name in enumerate(("Rat", "Hauptausschuss", "Bauausschuss", "Hauptausschuss"), start=2):
        neu = OParlMeeting.objects.create(
            external_id=_kennung("meetings", tage),
            body=body,
            name=f"Sitzung {name}",
            start=jetzt + timedelta(days=tage),
            location_name="Rathaus, Saal 2",
            location_address="Markt 1, 12345 Musterstadt",
        )
        neu.organizations.add(gremien[name])
        sitzung = sitzung or neu
    assert sitzung is not None
    vorlage = OParlPaper.objects.create(
        external_id=_kennung("papers", 1), body=body, name="Radwegekonzept", reference="V/2026/1", date=date.today()
    )
    for nummer, (name, oeffentlich) in enumerate((("Radwege", True), ("Grundstück", False)), start=1):
        top = OParlAgendaItem.objects.create(
            external_id=_kennung("items", nummer),
            meeting=sitzung,
            number=str(nummer),
            order=nummer,
            name=name,
            public=oeffentlich,
            result="beschlossen" if nummer == 1 else "",
        )
        if oeffentlich:
            OParlConsultation.objects.create(
                external_id=_kennung("consultations", nummer),
                body=body,
                paper=vorlage,
                paper_external_id=vorlage.external_id,
                meeting_external_id=sitzung.external_id,
                agenda_item_external_id=top.external_id,
            )
    # Dokumente der Recherche: mehr als eine Seite (30 je Seite)
    OParlFile.objects.bulk_create(
        [
            OParlFile(
                external_id=_kennung("files", n),
                body=body,
                paper=vorlage,
                name=f"Anlage {n}",
                access_url=f"https://ris.teil2.example/datei-{n}.pdf",
                mime_type="application/pdf",
                size=2048,
                file_date=date.today() - timedelta(days=n),
            )
            for n in range(31)
        ]
    )
    return body, gremien, sitzung


def _beschluesse(body: OParlBody, sitzung: OParlMeeting) -> tuple[SessionTenant, SessionAgendaItem, SessionAgendaItem]:
    """Verwaltung der Kommune mit mehr als einer Seite öffentlicher Beschlüsse (50 je Seite) und einem nichtöffentlichen."""
    tenant = SessionTenant.objects.create(name="Stadt Musterstadt", slug="musterstadt-teil2", oparl_body=body)
    rat = SessionOrganization.objects.create(tenant=tenant, name="Rat", organization_type="council")
    ratssitzung = SessionMeeting.objects.create(
        tenant=tenant,
        organization=rat,
        name="Ratssitzung",
        start=timezone.now() - timedelta(days=30),
        is_public=True,
        oparl_meeting=sitzung,
    )
    heute = timezone.localdate()
    staende = ("open", "in_progress", "done", "deferred")
    SessionAgendaItem.objects.bulk_create(
        [
            SessionAgendaItem(
                meeting=ratssitzung,
                number=str(n),
                order=n,
                name=f"Beschluss {n}",
                resolution_text=f"Der Rat beschließt Punkt {n}.\nZweiter Absatz.",
                resolution_number=f"B/2026/{n:04d}",
                vote_result="rejected" if n % 7 == 0 else "approved",
                implementation_status=staende[n % 4],
                implementation_recipient="Bauamt",
                implementation_deadline=heute - timedelta(days=3) if n % 5 == 0 else heute + timedelta(days=30),
                implementation_note=f"Vermerk {n}",
                implementation_updated_at=timezone.now(),
            )
            for n in range(1, 53)
        ]
    )
    beschluss = SessionAgendaItem.objects.get(meeting=ratssitzung, number="1")
    nicht_oeffentlich = SessionAgendaItem.objects.create(
        meeting=ratssitzung, number="99", order=99, name="Personalsache", vote_result="approved", is_public=False
    )
    return tenant, beschluss, nicht_oeffentlich


def _fraktion(org: Organization, vorsitz: Membership) -> FactionMeeting:
    """Mehr als eine Seite anstehender Fraktionssitzungen (15 je Seite) und der Änderungshistorie (50 je Seite)."""
    jetzt = timezone.now()
    erste = None
    for n in range(16):
        sitzung = FactionMeeting.objects.create(
            organization=org,
            title=f"Fraktionssitzung {n + 1}",
            start=jetzt + timedelta(days=n + 1),
            status="planned",
            location="Fraktionsbüro",
            created_by=vorsitz,
        )
        erste = erste or sitzung
    assert erste is not None
    for n in range(51):
        FactionAuditLog.objects.create(
            organization=org,
            membership=vorsitz,
            actor_label="Vera Vorsitz",
            action=("create", "update", "delete", "generated")[n % 4],
            model_name="FactionMeeting",
            object_id=erste.pk,
            object_repr=f"Fraktionssitzung {n}",
            meeting_id_ref=erste.pk,
            changes={"title": {"alt": "Alt", "neu": "Neu"}} if n % 4 == 1 else {},
        )
    return erste


def _ladungen(org: Organization, rat: Membership, sitzung_name: str = "Sitzung des Hauptausschusses") -> None:
    """Verbindung zur Verwaltung, das Konto ``rat`` ist einer Person zugeordnet und hat eine offene Ladung."""
    tenant = SessionTenant.objects.create(name="Verwaltung Musterstadt", slug="verwaltung-teil2")
    gremium = SessionOrganization.objects.create(tenant=tenant, name="Hauptausschuss")
    person = SessionPerson.objects.create(tenant=tenant, given_name="Rita", family_name="Rat", email=EMAIL_RAT)
    SessionOrganizationMembership.objects.create(organization=gremium, person=person, role="member")
    sitzung = SessionMeeting.objects.create(
        tenant=tenant,
        name=sitzung_name,
        organization=gremium,
        start=timezone.now() + timedelta(days=10),
        meeting_state="scheduled",
        location="Rathaus",
        room="Saal 1",
    )
    rat.user.email_verified = True
    rat.user.save(update_fields=["email_verified"])
    _token, roh = SessionAPIToken.create_token(tenant, org.name, can_submit_applications=True)
    connect_with_token(org, roh, rat)
    invitation_service.send_invitations(sitzung, sent_by=None)


@pytest.fixture(autouse=True)
def _medien(settings: Any, tmp_path: Any) -> None:
    settings.MEDIA_ROOT = tmp_path


@pytest.fixture
def welt(org: Organization, make_member: Any) -> Welt:
    vorsitz = make_member(org, [], email="vera.vorsitz@example.org", is_admin=True)
    vorsitz.is_sworn_in = True
    vorsitz.save(update_fields=["is_sworn_in"])
    leser = make_member(
        org,
        ["meetings.view", "faction.view_public", "ris.view", "dashboard.view", "motions.view"],
        email="lea.leser@example.org",
    )
    ohne = make_member(org, [], email="otto.ohne@example.org")
    konto = UserFactory(email="gabi.gast@example.org")  # type: ignore[no-untyped-call]
    gast = MembershipFactory(user=konto, organization=org, is_guest=True)  # type: ignore[no-untyped-call]
    rat = make_member(org, ["meetings.view", "dashboard.view"], email=EMAIL_RAT)
    body, gremien, sitzung = _ris(org)
    vorsitz.followed_organizations.add(gremien["Hauptausschuss"])
    MeetingPreparation.objects.create(organization=org, meeting=sitzung, is_prepared=True, prepared_at=timezone.now())
    tenant, beschluss, nicht_oeffentlich = _beschluesse(body, sitzung)
    fraktionssitzung = _fraktion(org, vorsitz)
    _ladungen(org, rat)
    rollen = {"vorsitz": vorsitz, "leser": leser, "ohne": ohne, "gast": gast, "rat": rat}
    return Welt(org, rollen, body, sitzung, fraktionssitzung, tenant, beschluss, nicht_oeffentlich)


def _text_pdf() -> bytes:
    """PDF mit Textebene und zwei nichtöffentlichen Punkten (wie test_nichtoeffentliche_unterlagen)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    puffer = io.BytesIO()
    leinwand = canvas.Canvas(puffer, pagesize=A4)
    for y, zeile in zip(
        range(780, 600, -24),
        ["Tagesordnung", "Nichtöffentlicher Teil", "N 1 Grundstücksangelegenheit", "N 2 Personalangelegenheit"],
        strict=False,
    ):
        leinwand.drawString(72, y, zeile)
    leinwand.showPage()
    leinwand.save()
    return puffer.getvalue()


def _unterlage(welt: Welt, client_for: Any) -> Motion:
    """Eingelesene nichtöffentliche Unterlage (Schritt 1) für die Seite „Übernehmen“."""
    vorsitz = welt.rollen["vorsitz"]
    adresse = reverse(
        "work:faction_internal_import",
        kwargs={"org_slug": welt.org.slug, "meeting_id": welt.fraktionssitzung.pk},
    )
    datei = SimpleUploadedFile("noe.pdf", _text_pdf(), content_type="application/pdf")
    assert client_for(vorsitz.user).post(adresse, {"datei": datei}).status_code == 200
    return Motion.objects.get(organization=welt.org)


# ---------------------------------------------------------------------------
# Seiten: Adresse, Anfrage, Vorlagen und begründete Ausnahmen
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Seite:
    name: str
    neu: str
    alt: str
    kwargs: tuple[tuple[str, str], ...] = ()
    anfrage: tuple[tuple[str, str], ...] = ()

    @property
    def schluessel(self) -> str:
        return f"{self.name}{'?' + urlencode(self.anfrage) if self.anfrage else ''}"


SEITEN = [
    Seite("work:meetings", "work/meetings/neu/sitzungen.html", "work/meetings/list.html"),
    Seite(
        "work:meetings",
        "work/meetings/neu/sitzungen.html",
        "work/meetings/list.html",
        anfrage=(("view", "all"), ("time", "all")),
    ),
    Seite("work:meetings_calendar", "work/meetings/neu/kalender.html", "work/meetings/calendar.html"),
    Seite(
        "work:meeting_detail",
        "work/meetings/neu/sitzung.html",
        "work/meetings/detail.html",
        kwargs=(("meeting_id", "sitzung"),),
    ),
    Seite("work:session_invitations", "work/meetings/neu/ladungen.html", "work/meetings/session_invitations.html"),
    Seite("work:faction", "work/faction/neu/sitzungen.html", "work/faction/list.html", anfrage=(("time", "upcoming"),)),
    Seite("work:faction_audit", "work/faction/neu/historie.html", "work/faction/audit_list.html"),
    Seite(
        "work:faction_internal_import",
        "work/faction/neu/unterlage_einlesen.html",
        "work/faction/internal_import.html",
        kwargs=(("meeting_id", "fraktionssitzung"),),
    ),
    Seite(
        "work:faction_internal_import_confirm",
        "work/faction/neu/unterlage_uebernehmen.html",
        "work/faction/internal_import_confirm.html",
        kwargs=(("meeting_id", "fraktionssitzung"), ("motion_id", "unterlage")),
    ),
    Seite("work:ris_decisions", "work/ris/neu/beschluesse.html", "work/ris/decisions.html"),
    Seite("work:ris_files", "work/ris/neu/dokumente.html", "work/ris/files.html"),
]

#: Aktionen der bisherigen Seiten, die die neue bewusst anders anbietet (je Seite, mit Begründung)
AUSNAHMEN: dict[str, set[Aktion]] = {
    # Die eigenen Reiter „Meine Gremien · Alle Sitzungen“ entfallen (doppelte Reiter, die Reiter trägt der Rahmen);
    # der Wechsel bleibt als Textlink. Nur der Reiter der gerade gezeigten Ansicht zeigte auf die Seite selbst.
    "work:meetings": {("GET", "?time=upcoming&view=my", ())},
    "work:meetings?view=all&time=all": {("GET", "?time=all&view=all", ())},
    # Zeitraum als Textlinks: der gewählte ist Text mit aria-current statt eines Links auf die Seite selbst
    "work:faction?time=upcoming": {("GET", "?time=upcoming", ())},
}


def _adresse(welt: Welt, seite: Seite) -> str:
    werte = {schluessel: getattr(welt, wert, None) or welt.extra[wert] for schluessel, wert in seite.kwargs}
    return reverse(seite.name, kwargs={"org_slug": welt.org.slug, **{k: v.pk for k, v in werte.items()}})


def _abrufen(client: Any, adresse: str, anfrage: dict[str, str], org: Organization, an: bool) -> Any:
    _schalter(org, an)
    return client.get(adresse, anfrage)


@pytest.mark.parametrize("seite", SEITEN, ids=[s.schluessel for s in SEITEN])
def test_keine_funktion_der_bisherigen_seite_entfaellt(welt: Welt, client_for: Any, seite: Seite) -> None:
    """
    Gleichstand je Rolle (Vorsitz mit allen Rechten, Leserecht, ohne Rechte, Gast, Ratsmitglied mit Ladung): gleicher
    Status mit und ohne Schalter, und jede Aktion der bisherigen Seite steht auch auf der neuen.
    """
    if ("motion_id", "unterlage") in seite.kwargs:
        welt.extra["unterlage"] = _unterlage(welt, client_for)
    adresse = _adresse(welt, seite)
    anfrage = dict(seite.anfrage)
    fehlt: list[str] = []
    gesehen = 0
    for rolle, mitglied in welt.rollen.items():
        client = client_for(mitglied.user)
        alt = _abrufen(client, adresse, anfrage, welt.org, an=False)
        neu = _abrufen(client, adresse, anfrage, welt.org, an=True)
        assert alt.status_code == neu.status_code, f"{rolle}: {alt.status_code} ≠ {neu.status_code}"
        if alt.status_code != 200:
            continue
        gesehen += 1
        assert seite.alt in [t.name for t in alt.templates], rolle
        assert seite.neu in [t.name for t in neu.templates], rolle
        alt_html, neu_html = alt.content.decode(), neu.content.decode()
        assert 'data-gestaltung="neu"' in neu_html, rolle
        fehlt.extend(f"{rolle}: {f}" for f in _fehlt(alt_html, neu_html, AUSNAHMEN.get(seite.schluessel, set())))
    assert gesehen, "keine Rolle sieht die Seite – der Vergleich wäre leer"
    assert not fehlt, "\n".join(fehlt)


def test_bestand_zeigt_was_der_vergleich_pruefen_soll(welt: Welt, client_for: Any) -> None:
    """Die bisherigen Seiten bieten im Bestand wirklich Seitenwahl, Ampel-Filter, Rückmeldung und Vorbereiten an."""
    _schalter(welt.org, False)
    slug = welt.org.slug
    vorsitz = client_for(welt.rollen["vorsitz"].user)

    def aktionen(name: str, anfrage: dict[str, str] | None = None, client: Any = vorsitz, **kw: Any) -> _Aktionen:
        antwort = client.get(reverse(name, kwargs={"org_slug": slug, **kw}), anfrage or {})
        assert antwort.status_code == 200, name
        return _aktionen(antwort.content.decode())

    assert ("GET", "?page=2&time=upcoming", ()) in aktionen("work:faction", {"time": "upcoming"}).links
    assert ("GET", "?page=2", ()) in aktionen("work:faction_audit").links
    beschluesse = aktionen("work:ris_decisions").links
    assert {("GET", "?status=open", ()), ("GET", "?overdue=1", ()), ("GET", "?page=2", ())} <= beschluesse
    assert ("GET", "?page=2", ()) in aktionen("work:ris_files").links
    ladung = aktionen("work:session_invitations", client=client_for(welt.rollen["rat"].user))
    assert any("action=decline" in felder for _m, _z, felder in ladung.formulare)
    sitzung = aktionen("work:meeting_detail", meeting_id=welt.sitzung.pk).links
    prepare = reverse("work:meeting_prepare", kwargs={"org_slug": slug, "meeting_id": welt.sitzung.pk})
    assert ("GET", prepare, ()) in sitzung


# ---------------------------------------------------------------------------
# Schalter und Rückweg
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seite", SEITEN, ids=[s.schluessel for s in SEITEN])
def test_rueckweg_ansicht_bisher(welt: Welt, client_for: Any, seite: Seite) -> None:
    """Mit eingeschaltetem Schalter öffnet ``?ansicht=bisher`` die bisherige Vorlage (Rückweg, falls etwas fehlt)."""
    if ("motion_id", "unterlage") in seite.kwargs:
        welt.extra["unterlage"] = _unterlage(welt, client_for)
    _schalter(welt.org, True)
    antwort = client_for(welt.rollen["vorsitz"].user).get(
        _adresse(welt, seite), {**dict(seite.anfrage), "ansicht": "bisher"}
    )
    assert antwort.status_code == 200
    namen = [t.name for t in antwort.templates]
    assert seite.alt in namen and seite.neu not in namen


# ---------------------------------------------------------------------------
# Inhalte der neuen Seiten
# ---------------------------------------------------------------------------


def _neu(welt: Welt, client_for: Any, rolle: str, name: str, anfrage: dict[str, str] | None = None, **kw: Any) -> str:
    _schalter(welt.org, True)
    antwort = client_for(welt.rollen[rolle].user).get(
        reverse(name, kwargs={"org_slug": welt.org.slug, **kw}), anfrage or {}
    )
    assert antwort.status_code == 200
    return str(antwort.content.decode())


def test_meine_sitzungen_satz_statt_hinweiskasten_und_vorbereitungsstand(welt: Welt, client_for: Any) -> None:
    html = _neu(welt, client_for, "vorsitz", "work:meetings")
    slug = welt.org.slug
    ausschuss = OParlMeeting.objects.get(external_id=_kennung("meetings", 3))

    assert "2 anstehende Sitzungen der Gremien, denen Sie folgen: Hauptausschuss." in html
    assert "Alle Gremien einbeziehen" in html and "Gremien wählen" in html
    assert 'data-testid="sitzungsliste"' in html
    # Vorbereiten mit meetings.prepare (Vorsitz), Ziel der Zeile ist das Sitzungsdetail der Vorbereitung
    assert reverse("work:meeting_prepare", kwargs={"org_slug": slug, "meeting_id": ausschuss.pk}) in html
    assert reverse("work:meeting_detail", kwargs={"org_slug": slug, "meeting_id": ausschuss.pk}) in html
    # Die Reiter „Für mich · Alle Gremien“ trägt der Rahmen; eigene Reiter „Meine Gremien“ gibt es nicht mehr
    assert 'aria-label="Sitzungen"' in html and "Meine Gremien" not in _inhalt(html)

    ohne_gremien = _neu(welt, client_for, "leser", "work:meetings")
    assert "Sie folgen noch keinen Gremien" in ohne_gremien
    assert "/prepare/" not in _inhalt(ohne_gremien)


def test_kalender_ohne_inline_skript_und_feste_farbe(welt: Welt, client_for: Any) -> None:
    html = _neu(welt, client_for, "leser", "work:meetings_calendar")
    ereignisse = reverse("work:meetings_calendar_events", kwargs={"org_slug": welt.org.slug})

    assert f'x-data="sitzungsKalender" data-ereignisse="{ereignisse}"' in html
    vorlage = (VORLAGEN / "work/meetings/neu/kalender.html").read_text(encoding="utf-8")
    assert "<style" not in vorlage and "#6366f1" not in vorlage
    assert _inline_skripte(vorlage) == 0


def test_sitzungsdetail_eine_hauptaktion_und_vorlagen_je_punkt(welt: Welt, client_for: Any) -> None:
    html = _neu(welt, client_for, "vorsitz", "work:meeting_detail", meeting_id=welt.sitzung.pk)
    slug = welt.org.slug

    assert html.count(reverse("work:meeting_prepare", kwargs={"org_slug": slug, "meeting_id": welt.sitzung.pk})) == 1
    vorlage = OParlPaper.objects.get(reference="V/2026/1")
    assert reverse("work:ris_paper_detail", kwargs={"org_slug": slug, "paper_id": vorlage.pk}) in html
    assert "Nichtöffentlich" in html and "Abgeschlossen am" in html
    assert "Markt 1, 12345 Musterstadt" in html


def test_fraktionssitzungen_hauptaktion_textlinks_und_weiteres(welt: Welt, client_for: Any) -> None:
    html = _neu(welt, client_for, "vorsitz", "work:faction")
    slug = welt.org.slug

    assert "16 Sitzungen, davon 16 anstehend." in html
    assert "$dispatch(&#x27;open-create-modal&#x27;)" in html or "$dispatch('open-create-modal')" in html
    assert 'x-show="showCreateModal"' in html and 'aria-labelledby="neue-sitzung-titel"' in html
    assert reverse("work:faction_audit", kwargs={"org_slug": slug}) in html
    liste, weiteres = html.index("Fraktionssitzung 1<"), html.index('data-testid="fraktion-weiteres"')
    assert liste < weiteres
    for feld in ("nachweis-von", "nachweis-bis", "sammel-von", "sammel-bis", "sammel-format"):
        assert f'for="{feld}"' in html and f'id="{feld}"' in html

    # Ohne Rechte zum Anlegen kein Dialog und keine Hauptaktion, ohne Historienrecht kein Link
    leser = _neu(welt, client_for, "leser", "work:faction")
    assert "open-create-modal')" not in _inhalt(leser).replace("&#x27;", "'")
    assert 'x-show="showCreateModal"' not in leser
    assert reverse("work:faction_audit", kwargs={"org_slug": slug}) not in leser


def test_historie_ohne_farbchips_mit_gesperrten_eintraegen(welt: Welt, client_for: Any, make_member: Any) -> None:
    FactionAuditLog.objects.create(
        organization=welt.org,
        action="update",
        model_name="FactionAgendaItem",
        object_id=welt.fraktionssitzung.pk,
        object_repr="Geheimer Punkt",
        is_internal=True,
        changes={"title": {"alt": "Geheim alt", "neu": "Geheim neu"}},
    )
    # Nicht vereidigt: nichtöffentliche Einträge nur als Platzhalter (Issue #64)
    welt.rollen["historie"] = make_member(
        welt.org, ["faction.view_public", "faction.view_audit"], email="h@example.org"
    )

    html = _neu(welt, client_for, "historie", "work:faction_audit")
    assert "Geheimer Punkt" not in html and "Geheim neu" not in html and "Gesperrte Information" in html
    assert "rounded-full text-[10px]" not in html and 'data-testid="historie"' in html
    anzahl = FactionAuditLog.objects.filter(organization=welt.org).count()
    assert "Vera Vorsitz" in html and f"{anzahl} Einträge, neueste zuerst" in html


def test_beschluesse_ampel_als_filter_im_satz_und_detailseite(welt: Welt, client_for: Any) -> None:
    html = _neu(welt, client_for, "leser", "work:ris_decisions")
    slug = welt.org.slug

    stand = html[html.index('data-testid="beschluss-stand"') :]
    stand = stand[: stand.index("</p>")]
    for ziel in ("?status=open", "?status=in_progress", "?overdue=1", "?status=done"):
        assert f'href="{ziel}"' in stand
    assert "52 Beschlüsse" in stand
    detail = reverse("work:ris_decision_detail", kwargs={"org_slug": slug, "item_id": welt.beschluss.pk})
    assert detail in html

    gefiltert = _neu(welt, client_for, "leser", "work:ris_decisions", {"status": "done"})
    assert 'aria-current="true"' in gefiltert

    seite = _neu(welt, client_for, "leser", "work:ris_decision_detail", item_id=welt.beschluss.pk)
    assert "Der Rat beschließt Punkt 1." in seite and "Zweiter Absatz." in seite and "Vermerk 1" in seite
    assert "B/2026/0001" in seite and "Bauamt" in seite
    # Brotkrumen: Recherche › Beschlüsse › Beschluss
    krumen = seite.split('aria-label="Brotkrumen"', 1)[1].split("</nav>", 1)[0]
    assert reverse("work:ris_decisions", kwargs={"org_slug": slug}) in krumen


def test_beschluss_detailseite_nur_oeffentlich_und_mit_leserecht(welt: Welt, client_for: Any) -> None:
    slug = welt.org.slug
    _schalter(welt.org, True)

    def status(rolle: str, item: SessionAgendaItem) -> int:
        adresse = reverse("work:ris_decision_detail", kwargs={"org_slug": slug, "item_id": item.pk})
        return int(client_for(welt.rollen[rolle].user).get(adresse).status_code)

    assert status("leser", welt.beschluss) == 200
    assert status("leser", welt.nicht_oeffentlich) == 404
    liste = reverse("work:ris_decisions", kwargs={"org_slug": slug})
    for rolle in ("ohne", "gast"):
        # Dieselbe Antwort wie die Liste der Beschlüsse (Rechte wie dort)
        assert status(rolle, welt.beschluss) == client_for(welt.rollen[rolle].user).get(liste).status_code

    fremd = SessionTenant.objects.create(name="Andere Stadt", slug="andere-stadt-teil2")
    gremium = SessionOrganization.objects.create(tenant=fremd, name="Rat", organization_type="council")
    sitzung = SessionMeeting.objects.create(
        tenant=fremd, organization=gremium, name="Fremd", start=timezone.now(), is_public=True
    )
    fremder = SessionAgendaItem.objects.create(meeting=sitzung, number="1", name="Fremd", vote_result="approved")
    assert status("leser", fremder) == 404


def test_dokumente_ansehen_per_tastatur_und_original(welt: Welt, client_for: Any) -> None:
    html = _neu(welt, client_for, "leser", "work:ris_files")

    assert "31 Dokumente aus Musterstadt" in html
    assert html.count('data-doc-url="/') == 30 and "Ansehen<span" in html
    assert 'href="https://ris.teil2.example/datei-0.pdf" target="_blank"' in html


def test_ladungen_ohne_karten_mit_rueckmeldung(welt: Welt, client_for: Any) -> None:
    html = _neu(welt, client_for, "rat", "work:session_invitations")

    assert "Sitzung des Hauptausschusses" in html and "Empfang nicht bestätigt" in html
    assert 'name="action" value="decline"' in html and 'name="substitute"' in html and 'value="1"' in html
    assert "Verwaltung Musterstadt: Empfang bestätigen und Teilnahme melden." in html
    ohne = _neu(welt, client_for, "leser", "work:session_invitations")
    assert "Keine Zuordnung zum Sitzungsdienst" in ohne


def test_unterlage_einlesen_mit_dateifeld_und_uebernehmen(welt: Welt, client_for: Any) -> None:
    slug, sitzung = welt.org.slug, welt.fraktionssitzung
    html = _neu(welt, client_for, "vorsitz", "work:faction_internal_import", meeting_id=sitzung.pk)
    assert 'type="file" name="datei"' in html and "oder hierher ziehen" in html

    unterlage = _unterlage(welt, client_for)
    seite = _neu(
        welt,
        client_for,
        "vorsitz",
        "work:faction_internal_import_confirm",
        meeting_id=sitzung.pk,
        motion_id=unterlage.pk,
    )
    assert "Grundstücksangelegenheit" in seite and "Als nichtöffentliche TOPs übernehmen" in seite
    assert reverse("work:document_editor", kwargs={"org_slug": slug, "motion_id": unterlage.pk}) in seite


NEUE_VORLAGEN = sorted({s.neu for s in SEITEN} | {"work/ris/neu/beschluss.html"})


@pytest.mark.parametrize("name", NEUE_VORLAGEN)
def test_neue_vorlagen_mit_bausteinen_statt_handarbeit(name: str) -> None:
    """Kopfband und Gestaltung „neu“; keine Kartenkästen, keine Farbchips, keine Inline-Skripte oder -Stile."""
    text = (VORLAGEN / name).read_text(encoding="utf-8")
    assert "{% block gestaltung %}neu{% endblock %}" in text
    assert "<c-rahmen.kopfband" in text
    for handarbeit in ("rounded-xl border", "rounded-full text-[10px]", "<style", "c-ui.card", "c-ui.badge"):
        assert handarbeit not in text, f"{name}: {handarbeit}"
    assert _inline_skripte(text) == 0, name
