# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Neues Erscheinungsbild des Sitzungsdienstes, Teil 2a (Issue #944).

Geprüft: Gremium, Person, Antrag, Sitzungsgelder, Monatliche Pauschalen, Vertretungen, Kalender und Audit-Log haben mit
eingeschaltetem Schalter eine eigene neue Vorlage, ohne Schalter bleiben die bisherigen. Keine Funktion entfällt: Jede
Aktion der bisherigen Seite (Links, Formulare mit Methode und Feldern, htmx-Aufrufe, Adressen für Skripte) steht auch
auf der neuen – für jede Standardrolle und mit einem Bestand, der die Aktionen wirklich zeigt – und ist dort genauso
abgesichert: Rückfragen vor dem Absenden (``data-confirm``, ``hx-confirm``) und bei htmx die mitgesendeten Felder und das
Ziel der Antwort (``hx-include``, ``hx-target``, ``hx-swap``). Dasselbe gilt für die
ruhigen Fassungen der eingebetteten Teilvorlagen auf Sitzung und Vorlage: Anwesenheit (mit Zugeschalteten und
Störungen), Anlagen, Beratungsfolge und Bezüge. Die Anwesenheitszeile antwortet per htmx im Erscheinungsbild des
Mandanten.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from html.parser import HTMLParser
from typing import Any, cast
from urllib.parse import parse_qsl, urlsplit

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import Resolver404, resolve, reverse
from django.utils import timezone

from apps.common.tests.factories import UserFactory
from apps.session import rahmen
from apps.session.models import (
    SessionAgendaItem,
    SessionAllowance,
    SessionAllowanceRate,
    SessionApplication,
    SessionAttendance,
    SessionAttendanceDisruption,
    SessionBody,
    SessionConsultation,
    SessionDelegation,
    SessionFile,
    SessionLegislativeTerm,
    SessionMeeting,
    SessionMonthlyAllowance,
    SessionMonthlyRate,
    SessionOrganization,
    SessionOrganizationMembership,
    SessionPaper,
    SessionPerson,
    SessionPersonMonthlyRate,
    SessionRole,
    SessionTenant,
    SessionUser,
)
from apps.session.services import file_version_service
from apps.session.tests.test_neues_design import (
    _HX,
    Aktion,
    _aktionen,
    _client,
    _hx_werte,
    _intern,
    _mandant,
    _volle_welt,
    _vorlagen,
)

pytestmark = pytest.mark.django_db

#: Seiten dieses Teils: Name → neue Vorlage
SEITEN = {
    "gremium": "session/neu/gremium.html",
    "person": "session/neu/person.html",
    "antrag": "session/neu/antrag.html",
    "sitzungsgelder": "session/neu/sitzungsgelder.html",
    "monatspauschalen": "session/neu/monatspauschalen.html",
    "vertretungen": "session/neu/vertretungen.html",
    "kalender": "session/neu/kalender.html",
    "audit": "session/neu/audit.html",
}
#: Seiten mit den eingebetteten Teilvorlagen (Anwesenheit, Anlagen, Beratungsfolge, Bezüge)
EINGEBETTET = ("sitzung", "vorlage")


def _person(tenant: SessionTenant, vorname: str, nachname: str) -> SessionPerson:
    return SessionPerson.objects.create(
        tenant=tenant, given_name=vorname, family_name=nachname, start_date=date(2025, 1, 1)
    )


def _anlage(tenant: SessionTenant, name: str, **ziel: Any) -> SessionFile:
    datei = SessionFile(tenant=tenant, name=name, mime_type="text/plain", is_public=False, **ziel)
    file_version_service.attach_upload(datei, SimpleUploadedFile(name, b"Inhalt", content_type="text/plain"), user=None)
    return datei


def _welt_2a(tenant: SessionTenant) -> dict[str, Any]:
    """
    Bestand für Teil 2a auf der Grundlage von ``_volle_welt``: ein Ausschuss mit Vorsitz (Sperrvermerk), Vertretung,
    künftigem und ausgeschiedenem Mitglied und einer Wahlperiode; Anwesenheit der Sitzung mit einer zugeschalteten
    Person samt Störungen und weiteren Personen zum Ergänzen; Beratungsfolge mit erledigter, terminierter und offener
    Station; eine Unternummer; Anträge mit Anhang und erstellter Vorlage; Sitzungsgelder, Monatspauschalen,
    Vertretungen, eine zweite Körperschaft (Filter im Kalender) und Sitzungen im Monat der Sitzung.
    """
    welt = _volle_welt(tenant)
    slug = tenant.slug
    sitzung, vorlage, vergangen = welt["sitzung_obj"], welt["vorlage_obj"], welt["vergangen_obj"]
    rat = sitzung.organization
    heute = timezone.localdate()

    personen = [
        _person(tenant, v, n) for v, n in (("Anna", "Amberg"), ("Bernd", "Birk"), ("Clara", "Conrad"), ("Dora", "Dahl"))
    ]
    _person(tenant, "Gerd", "Gast")  # ohne Besetzung: unter „Gast oder Verwaltung ergänzen“
    for person in personen:
        SessionOrganizationMembership.objects.create(organization=rat, person=person, start_date=date(2025, 1, 1))
    cast(Any, personen[0]).set_bank_iban_encrypted("DE02120300000000202051")  # Accessor zur Laufzeit
    personen[0].save()

    ausschuss = SessionOrganization.objects.create(tenant=tenant, name="Bauausschuss", organization_type="committee")
    periode = SessionLegislativeTerm.objects.create(tenant=tenant, name="2025–2030", start_date=date(2025, 11, 1))
    vorsitz = SessionOrganizationMembership.objects.create(
        organization=ausschuss, person=personen[0], role="chair", start_date=date(2025, 1, 1), legislative_term=periode
    )
    SessionOrganizationMembership.objects.create(
        organization=ausschuss, person=personen[1], start_date=date(2025, 1, 1), substitute_for=personen[2]
    )
    SessionOrganizationMembership.objects.create(
        organization=ausschuss, person=personen[2], start_date=heute + timedelta(days=30)
    )
    SessionOrganizationMembership.objects.create(
        organization=ausschuss, person=personen[3], start_date=date(2025, 1, 1), end_date=heute - timedelta(days=3)
    )
    ausschuss_sitzung = SessionMeeting.objects.create(
        tenant=tenant,
        organization=ausschuss,
        name="Bauausschuss",
        start=heute_um(heute - timedelta(days=5)),
        meeting_state="completed",
    )
    SessionPaper.objects.create(tenant=tenant, name="Radweg", reference="V/2026/009", main_organization=ausschuss)

    # Anwesenheit: eine Person zugeschaltet mit laufender und beendeter Störung, der Gast noch zu ergänzen
    anwesend = [
        SessionAttendance.objects.create(meeting=sitzung, person=person, status=status)
        for person, status in zip(personen, ("present", "excused", "absent", "confirmed"), strict=True)
    ]
    anwesend[0].participation_mode = "remote"
    anwesend[0].save(update_fields=["participation_mode"])
    SessionAttendanceDisruption.objects.create(attendance=anwesend[0], started_at=time(18, 5), cause="technical")
    SessionAttendanceDisruption.objects.create(
        attendance=anwesend[0], started_at=time(17, 30), ended_at=time(17, 40), cause="technical"
    )

    # Beratungsfolge: Vorberatung erledigt (ohne TOP), Entscheidung mit TOP, Kenntnisnahme mit Zielsitzung ohne TOP
    top = SessionAgendaItem.objects.get(meeting=sitzung, number="2")
    SessionConsultation.objects.create(
        paper=vorlage, organization=ausschuss, role="preliminary", order=1, result="approved", meeting=vergangen
    )
    SessionConsultation.objects.create(
        paper=vorlage, organization=rat, role="decision", order=2, meeting=sitzung, agenda_item=top, authoritative=True
    )
    SessionConsultation.objects.create(paper=vorlage, organization=rat, role="information", order=3, meeting=sitzung)
    SessionPaper.objects.create(
        tenant=tenant, name="Ergänzung Brücke", parent_paper=vorlage, relation_type="supplement", sub_number=1
    )

    # Anträge: offener Antrag mit Anhang, umgewandelter Antrag mit Vorlage
    antrag = welt["antrag_obj"]
    SessionApplication.objects.filter(pk=antrag.pk).update(
        target_organization=rat,
        is_urgent=True,
        urgency_reason="Frist",
        co_signers="Fraktion B",
        processing_notes="Rückfrage",
    )
    _anlage(tenant, "antrag.txt", application=antrag)
    umgewandelt = SessionApplication.objects.create(
        tenant=tenant, title="Neue Bänke", submitter_name="F", status="converted"
    )
    SessionPaper.objects.create(tenant=tenant, name="Bänke", reference="V/2026/010", source_application=umgewandelt)

    # Sitzungsgelder in der Ausschusssitzung (ohne genehmigte Niederschrift): ausstehend, genehmigt, exportiert
    SessionAllowanceRate.objects.create(organization=ausschuss, role="member", amount=Decimal("30.00"))
    for nummer, (person, status) in enumerate(zip(personen[:3], ("pending", "approved", "paid"), strict=True)):
        teilnahme = SessionAttendance.objects.create(meeting=ausschuss_sitzung, person=person, status="present")
        SessionAllowance.objects.create(
            attendance=teilnahme, amount=Decimal("30.00"), status=status, export_reference="SG-1" if nummer == 2 else ""
        )

    # Monatspauschalen: aktive und inaktive Pauschale, Zuordnung mit Zeitraum, Posten in drei Ständen
    teil = SessionMonthlyRate.objects.create(
        tenant=tenant, name="Teilpauschale", amount=Decimal("322.50"), legal_basis="§ 2"
    )
    SessionMonthlyRate.objects.create(tenant=tenant, name="Alt", amount=Decimal("1.00"), is_active=False)
    SessionPersonMonthlyRate.objects.create(
        person=personen[0], rate=teil, start_date=date(2026, 1, 1), end_date=date(2026, 12, 31)
    )
    monat = heute.replace(day=1)
    for person, status in zip(personen[:3], ("pending", "approved", "paid"), strict=True):
        SessionMonthlyAllowance.objects.create(
            tenant=tenant,
            person=person,
            rate=teil,
            period=monat,
            amount=teil.amount,
            status=status,
            export_reference="MP-1" if status == "paid" else "",
        )

    # Vertretungen: aktiv, geplant und aufgehoben
    konten = [SessionUser.objects.create(user=cast(Any, UserFactory)(), tenant=tenant) for _ in range(2)]
    for start, ende, aufgehoben in ((-1, 5, False), (10, 20, False), (-30, -20, True)):
        SessionDelegation.objects.create(
            tenant=tenant,
            principal=konten[0],
            deputy=konten[1],
            start_date=heute + timedelta(days=start),
            end_date=heute + timedelta(days=ende),
            revoked_at=timezone.now() if aufgehoben else None,
        )

    # Kalender: zweite Körperschaft (Filter), nichtöffentliche und abgesagte Sitzung im Monat der Sitzung
    SessionBody.objects.create(tenant=tenant, name="Gemeinde Nord", short_name="GN", body_type="mitgliedsgemeinde")
    for name, felder in (("Grundstücke", {"is_public": False}), ("Ausfall", {"cancelled": True})):
        SessionMeeting.objects.create(
            tenant=tenant, organization=rat, name=name, start=sitzung.start + timedelta(hours=2), **felder
        )

    def adresse(name: str, **kw: Any) -> str:
        return reverse(f"session:{name}", kwargs={"tenant_slug": slug, **kw})

    termin = timezone.localtime(sitzung.start)
    von = (heute - timedelta(days=6)).isoformat()
    welt.update(
        {
            "gremium": adresse("organization_detail", organization_id=ausschuss.pk),
            "person": adresse("person_detail", person_id=personen[0].pk),
            "antrag": adresse("application_detail", application_id=antrag.pk),
            "sitzungsgelder": adresse("allowances") + f"?from={von}&to={heute.isoformat()}",
            "monatspauschalen": adresse("allowances_monthly"),
            "vertretungen": adresse("settings_delegations"),
            "kalender": adresse("meeting_calendar") + f"?year={termin.year}&month={termin.month}",
            "audit": adresse("audit_log"),
            "ausschuss_obj": ausschuss,
            "anwesend": anwesend,
        }
    )
    welt["erwartet_2a"] = {
        ("POST", adresse("membership_update", membership_id=vorsitz.pk)),
        ("POST", adresse("membership_succession", membership_id=vorsitz.pk)),
        ("GET", adresse("person_data_export", person_id=personen[0].pk)),
        ("GET", adresse("application_convert", application_id=antrag.pk)),
        ("POST", adresse("allowance_export_sepa")),
        ("POST", adresse("monthly_rate_save")),
        ("POST", adresse("settings_delegation_create")),
        ("GET", adresse("organization_ics_feed", org_id=ausschuss.pk)),
        ("POST", adresse("audit_log_export")),
        ("POST", adresse("attendance_update", attendance_id=anwesend[0].pk)),
        ("POST", adresse("attendance_add", meeting_id=sitzung.pk)),
        ("POST", reverse("session:attendance_disruption_add", kwargs={"tenant_slug": slug, "meeting_id": sitzung.pk})),
        (
            "POST",
            adresse("consultation_forward", consultation_id=SessionConsultation.objects.get(paper=vorlage, order=1).pk),
        ),
        ("POST", adresse("consultation_add", paper_id=vorlage.pk)),
        ("POST", adresse("paper_child_create", paper_id=vorlage.pk)),
        ("POST", adresse("file_upload")),
    }
    return welt


def heute_um(tag: date) -> datetime:
    return timezone.make_aware(datetime.combine(tag, time(17, 0)))


def _gleich(aktion: Aktion) -> Aktion:
    """Anfrageparameter in fester Reihenfolge: ``{% querystring %}`` ordnet sie anders als die bisherige Seitenwahl."""
    methode, adresse, felder = aktion
    teile = urlsplit(adresse)
    if not teile.query:
        return aktion
    parameter = "&".join(f"{k}={v}" for k, v in sorted(parse_qsl(teile.query, keep_blank_values=True)))
    return methode, f"{teile.path}?{parameter}", felder


def _normiert(antwort: Any) -> set[Aktion]:
    return {_gleich(a) for a in _aktionen(antwort)}


#: Absicherung einer Stelle, die eine Aktion auslöst: die Aktion und ihre Schutz- und Bezugsangaben
Absicherung = tuple[Aktion, frozenset[tuple[str, str]]]
RUECKFRAGE = ("Rückfrage", "")
#: Elemente ohne Endtag: sie sind keine Vorfahren und bleiben nicht auf dem Stapel
_OHNE_ENDE = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"})
#: Die Anwesenheit ist im neuen Rahmen eine Liste statt einer Tabelle: „die eigene Zeile“ bleibt derselbe Bezug
_EIGENE_ZEILE = {"closest tr": "closest Zeile", "closest li": "closest Zeile"}


class _Absicherungen(HTMLParser):
    """
    Je Stelle, die eine Aktion auslöst (Link, Formular mit Feldern, htmx-Aufruf – Schlüssel wie bei ``_aktionen``),
    ihre Absicherung: eine Rückfrage vor dem Absenden (``data-confirm`` an Formular, Knopf oder Link,
    ``data-confirm-message``, ``data-prompt``, ``hx-confirm``) und bei htmx die mitgesendeten Felder (``hx-include``), das
    Ziel (``hx-target``) und die Art des Einsetzens (``hx-swap``). ``hx-*`` gilt wie bei htmx auch von Vorfahren.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stellen: list[Absicherung] = []
        self._vorfahren: list[tuple[str, dict[str, str]]] = []
        self._formular: tuple[str, str] | None = None
        self._felder: set[str] = set()
        self._schutz: set[tuple[str, str]] = set()

    @staticmethod
    def _rueckfrage(a: dict[str, str]) -> set[tuple[str, str]]:
        return {RUECKFRAGE} if {"data-confirm", "data-confirm-message", "data-prompt"} & a.keys() else set()

    def _geerbt(self, a: dict[str, str], name: str) -> str:
        if name in a:
            return a[name]
        return next((attribute[name] for _, attribute in reversed(self._vorfahren) if name in attribute), "")

    def _htmx(self, a: dict[str, str]) -> set[tuple[str, str]]:
        schutz = {RUECKFRAGE} if self._geerbt(a, "hx-confirm") else set()
        for name in ("hx-include", "hx-target", "hx-swap"):
            if wert := self._geerbt(a, name):
                schutz.add((name, _EIGENE_ZEILE.get(wert, wert)))
        return schutz

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {name: wert or "" for name, wert in attrs}
        hx = next(((verb.upper(), a[f"hx-{verb}"]) for verb in _HX if a.get(f"hx-{verb}")), None)
        if tag == "form":
            self._formular = hx or (a.get("method", "get").upper(), a.get("action", ""))
            self._felder = set()
            self._schutz = self._rueckfrage(a) | (self._htmx(a) if hx else set())
        elif self._formular and tag in ("input", "select", "textarea", "button"):
            if a.get("name") and a["name"] != "csrfmiddlewaretoken":
                typ = a.get("type", "submit" if tag == "button" else "text").lower()
                fest = tag in ("input", "button") and typ in ("hidden", "submit", "button")
                self._felder.add(f"{a['name']}={a.get('value', '')}" if fest else a["name"])
            self._schutz |= self._rueckfrage(a)  # Rückfrage am Absendeknopf
        if tag == "a" and _intern(a.get("href", "")):
            self.stellen.append((("GET", a["href"], ()), frozenset(self._rueckfrage(a))))
        if hx and tag != "form" and _intern(hx[1]):
            aktion = (hx[0], hx[1], _hx_werte(a.get("hx-vals", "")))
            self.stellen.append((aktion, frozenset(self._htmx(a) | self._rueckfrage(a))))
        if tag not in _OHNE_ENDE:
            self._vorfahren.append((tag, a))

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._formular:
            self.stellen.append(((*self._formular, tuple(sorted(self._felder))), frozenset(self._schutz)))
            self._formular = None
        for stelle in range(len(self._vorfahren) - 1, -1, -1):
            if self._vorfahren[stelle][0] == tag:
                del self._vorfahren[stelle:]
                break


def _absicherungen(antwort: Any) -> list[Absicherung]:
    leser = _Absicherungen()
    leser.feed(antwort.content.decode())
    leser.close()
    return [(_gleich(aktion), schutz) for aktion, schutz in leser.stellen]


def _schwaecher(alt: list[Absicherung], neu: list[Absicherung]) -> list[str]:
    """
    Was die neue Seite schwächer absichert als die bisherige: Jede Absicherung einer bisherigen Stelle gibt es für
    dieselbe Aktion auch neu, und keine neue Stelle einer Aktion hat weniger als das, was bisher jede Stelle hatte (eine
    zweite Stelle ohne Rückfrage zählt also). Fehlt die Aktion ganz, meldet das schon der Vergleich der Aktionen.
    """
    bisher: dict[Aktion, list[frozenset[tuple[str, str]]]] = defaultdict(list)
    jetzt: dict[Aktion, list[frozenset[tuple[str, str]]]] = defaultdict(list)
    for aktion, schutz in alt:
        bisher[aktion].append(schutz)
    for aktion, schutz in neu:
        jetzt[aktion].append(schutz)
    fehlt: list[str] = []
    for aktion, varianten in bisher.items():
        if aktion not in jetzt:
            continue
        fehlt.extend(
            f"{aktion}: {sorted(schutz)} fehlt" for schutz in varianten if not any(schutz <= s for s in jetzt[aktion])
        )
        jede = frozenset.intersection(*varianten)
        fehlt.extend(f"{aktion}: Stelle ohne {sorted(jede - s)}" for s in jetzt[aktion] if not jede <= s)
    return fehlt


def _adressname(aktion: Aktion) -> str:
    try:
        return resolve(urlsplit(aktion[1]).path).url_name or ""
    except Resolver404:
        return ""


# =============================================================================
# Vorlagen
# =============================================================================


def test_seiten_waehlen_ihre_vorlage_nach_dem_schalter() -> None:
    tenant = _mandant("vorlagen2a", neu=False)
    welt = _welt_2a(tenant)
    for name, vorlage in SEITEN.items():
        rolle = "Revision" if name == "audit" else "Administrator"
        client = _client(tenant, rolle)
        SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=False)
        alt = client.get(welt[name])
        assert alt.status_code == 200, name
        assert vorlage not in _vorlagen(alt) and "session/base_session_alt.html" in _vorlagen(alt), name
        SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=True)
        neu = client.get(welt[name])
        assert neu.status_code == 200, name
        assert {vorlage, "session/base_session_neu.html"} <= _vorlagen(neu), name
        assert 'data-gestaltung="neu"' in neu.content.decode(), name


def test_eingebettete_teilvorlagen_im_neuen_rahmen() -> None:
    tenant = _mandant("teile2a", neu=True)
    welt = _welt_2a(tenant)
    client = _client(tenant, "Administrator")
    sitzung = client.get(welt["sitzung"])
    assert {"session/neu/_anwesenheit.html", "session/neu/_anwesenheit_zeile.html", "session/neu/_stoerungen.html",
            "session/neu/_anlagen.html"} <= _vorlagen(sitzung)  # fmt: skip
    assert not {"session/meetings/_attendance.html", "session/partials/file_section.html"} & _vorlagen(sitzung)
    html = sitzung.content.decode()
    assert "<table" not in html[html.index('data-testid="anwesenheit"') :], "Anwesenheit als Liste, nicht als Tabelle"
    vorlage = client.get(welt["vorlage"])
    assert {"session/neu/_beratungsfolge.html", "session/neu/_bezuege.html", "session/neu/_anlagen.html"} <= _vorlagen(
        vorlage
    )
    assert not {"session/papers/consultation_section.html", "session/papers/_relations.html"} & _vorlagen(vorlage)
    # Ohne Schalter bleiben die bisherigen Teilvorlagen
    SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=False)
    assert "session/meetings/_attendance.html" in _vorlagen(client.get(welt["sitzung"]))
    assert "session/papers/consultation_section.html" in _vorlagen(client.get(welt["vorlage"]))


# =============================================================================
# Keine Funktion entfällt
# =============================================================================


#: Rückfragen und htmx-Bezüge, die die bisherigen Seiten mit dem Bestand zeigen (Adressname, Art)
ERWARTETE_ABSICHERUNGEN = {
    *(
        (name, "Rückfrage")
        for name in (
            "organization_deactivate",
            "person_deactivate",
            "allowance_cancel",
            "monthly_rate_delete",
            "monthly_export_sepa",
            "monthly_cancel",
            "settings_delegation_revoke",
            "file_delete",
            "consultation_delete",
            "attendance_disruption_add",
            "attendance_delete",
        )
    ),
    ("attendance_update", "hx-include"),
    ("attendance_update", "hx-target"),
    ("agenda_item_create", "hx-target"),
}


def test_keine_funktion_der_bisherigen_seiten_entfaellt_teil2a() -> None:
    """
    Jede Aktion der bisherigen acht Seiten und der eingebetteten Teilvorlagen auf Sitzung und Vorlage steht auch auf der
    neuen Seite – für jede Standardrolle, die Vorlage in jedem Schritt des Freigabelaufs (Terminieren erst nach der
    Freigabe). Anfrageparameter zählen ohne ihre Reihenfolge. Ausnahme wie in Teil 1 nur die vier Seiten, die die
    bisherige Seitenleiste direkt anbot und die jetzt als Reiter über ihrer Liste stehen (Kalender, Zu prüfen, Meine
    Mitzeichnungen, Umlaufbeschlüsse) – das betrifft den Rahmen, nicht die Seiten. Jede Aktion ist neu genauso
    abgesichert wie bisher (``_schwaecher``): dieselben Rückfragen, bei htmx dieselben mitgesendeten Felder, dasselbe
    Ziel und Einsetzen; „die eigene Tabellenzeile“ der Anwesenheit ist jetzt „die eigene Listenzeile“.
    """
    tenant = _mandant("vergleich2a", neu=False)
    welt = _welt_2a(tenant)
    reiter: set[Aktion] = {
        ("GET", reverse(f"session:{name}", kwargs={"tenant_slug": tenant.slug}), ())
        for name in ("meeting_calendar", "papers_review", "my_cosignatures", "circulars")
    }
    fehlt: list[str] = []
    schwaecher: list[str] = []
    gesehen: set[Aktion] = set()
    abgesichert: set[tuple[str, str]] = set()
    for rolle in [definition["name"] for definition in SessionRole.default_role_definitions().values()]:
        client = _client(tenant, rolle)
        for status in ("draft", "review", "approved"):
            SessionPaper.objects.filter(pk=welt["vorlage_obj"].pk).update(status=status)
            for name in (*SEITEN, *EINGEBETTET) if status == "draft" else ("vorlage",):
                SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=False)
                alt = client.get(welt[name])
                SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=True)
                neu = client.get(welt[name])
                assert alt.status_code == neu.status_code, f"{rolle}/{status}/{name}"
                if alt.status_code != 200:
                    continue
                bisher = _normiert(alt)
                gesehen |= bisher
                fehlt.extend(f"{rolle}/{status}/{name}: {a}" for a in sorted(bisher - _normiert(neu) - reiter))
                stellen = _absicherungen(alt)
                abgesichert |= {(_adressname(aktion), art) for aktion, schutz in stellen for art, _ in schutz}
                schwaecher.extend(f"{rolle}/{status}/{name}: {s}" for s in _schwaecher(stellen, _absicherungen(neu)))
    assert not fehlt, "\n".join(fehlt)
    assert not schwaecher, "\n".join(schwaecher)
    # Der Bestand zeigt wirklich, was er prüfen soll (sonst wäre der Vergleich leer)
    gezeigt = {(methode, adresse) for methode, adresse, _ in gesehen}
    assert welt["erwartet_2a"] <= gezeigt, sorted(welt["erwartet_2a"] - gezeigt)
    assert abgesichert >= ERWARTETE_ABSICHERUNGEN, sorted(ERWARTETE_ABSICHERUNGEN - abgesichert)


class _Antwort:
    def __init__(self, html: str) -> None:
        self.content = html.encode()


def test_absicherungen_werden_erkannt() -> None:
    """Der Vergleich erkennt eine fehlende Rückfrage, ein anderes Ziel und eine zweite Stelle ohne Rückfrage."""
    bisher = _absicherungen(
        _Antwort(
            '<form method="post" action="/x/" data-confirm="Sicher?"><button name="a" value="1">A</button></form>'
            '<div hx-target="closest tr"><button hx-post="/y/" hx-include="closest tr">S</button></div>'
            '<a href="/z/" data-confirm="Weg?">Z</a>'
        )
    )
    assert (("POST", "/x/", ("a=1",)), frozenset({RUECKFRAGE})) in bisher
    assert (("POST", "/y/", ()), frozenset({("hx-target", "closest Zeile"), ("hx-include", "closest Zeile")})) in bisher
    gleich = _absicherungen(
        _Antwort(
            '<form method="post" action="/x/"><button name="a" value="1" data-confirm="Sicher?">A</button></form>'
            '<button hx-post="/y/" hx-include="closest li" hx-target="closest li">S</button>'
            '<a href="/z/" data-confirm="Wirklich?">Z</a>'
        )
    )
    assert _schwaecher(bisher, gleich) == []
    schwach = _absicherungen(
        _Antwort(
            '<form method="post" action="/x/"><button name="a" value="1">A</button></form>'
            '<button hx-post="/y/" hx-include="closest li" hx-target="#anderswo">S</button>'
            '<a href="/z/" data-confirm="Weg?">Z</a><a href="/z/">Z ohne Rückfrage</a>'
        )
    )
    meldungen = _schwaecher(bisher, schwach)
    assert len(meldungen) == 5, meldungen  # /x/ und /y/ je fehlt und schwächere Stelle, /z/ zweite Stelle


def test_anfrageparameter_ohne_reihenfolge() -> None:
    assert _gleich(("GET", "?page=2&model=x", ())) == _gleich(("GET", "?model=x&page=2", ()))
    assert _gleich(("GET", "/session/x/a/?to=2&from=1", ())) == ("GET", "/session/x/a/?from=1&to=2", ())
    assert _gleich(("GET", "/session/x/", ())) == ("GET", "/session/x/", ())


# =============================================================================
# Reiter nur mit dem Recht der Seite
# =============================================================================


def test_reiter_jahresplanung_nur_mit_dem_recht_der_seite() -> None:
    """„Jahresplanung“ verlangt ``edit_meetings`` (MeetingPlanView): ohne das Recht kein Reiter, der in einen 403 führt."""
    tenant = _mandant("reiter2a", neu=True)
    lesen = rahmen.navigation(tenant, {"view_meetings"}, "meeting_calendar")
    assert [r["key"] for r in lesen["reiter"]] == ["liste", "kalender", "archiv"]
    planen = rahmen.navigation(tenant, {"view_meetings", "edit_meetings"}, "meeting_calendar")
    assert [r["key"] for r in planen["reiter"]] == ["liste", "kalender", "jahresplanung", "archiv"]
    # Auf dem Kalender: der Lesezugriff sieht keinen Weg zur Jahresplanung, wer planen darf, nur den Reiter
    welt = _welt_2a(tenant)
    plan = f'href="{reverse("session:meeting_plan", kwargs={"tenant_slug": tenant.slug})}"'
    assert plan not in _client(tenant, "Lesezugriff").get(welt["kalender"]).content.decode()
    assert _client(tenant, "Administrator").get(welt["kalender"]).content.decode().count(plan) == 1


def test_reiter_verlangen_das_recht_ihrer_seite() -> None:
    """Jeder Reiter erscheint genau mit dem Recht, das seine Seite verlangt (sonst führte er in einen 403)."""
    for ziele in rahmen.REITER.values():
        for ziel in ziele:
            ansicht = getattr(resolve(reverse(ziel.url_name, kwargs={"tenant_slug": "x"})).func, "view_class", None)
            verlangt = getattr(ansicht, "permission_required", None)
            verlangt = (verlangt,) if isinstance(verlangt, str) else tuple(verlangt or ())
            assert set(ziel.rechte) == set(verlangt), (ziel.key, verlangt)


# =============================================================================
# Anwesenheit per htmx
# =============================================================================


def test_anwesenheitszeile_antwortet_im_erscheinungsbild_des_mandanten() -> None:
    tenant = _mandant("zeile2a", neu=True)
    welt = _welt_2a(tenant)
    teilnahme: SessionAttendance = welt["anwesend"][1]
    client = _client(tenant, "Administrator")
    adresse = reverse("session:attendance_update", kwargs={"tenant_slug": tenant.slug, "attendance_id": teilnahme.pk})
    daten = {"status": "present", "participation_mode": "in_person", "mode_column": "1", "notes": "spät"}
    neu = client.post(adresse, daten, HTTP_HX_REQUEST="true")
    assert neu.status_code == 200
    assert "session/neu/_anwesenheit_zeile.html" in _vorlagen(neu)
    html = neu.content.decode()
    assert "<li" in html and 'hx-target="closest li"' in html and 'value="spät"' in html and "<tr" not in html
    SessionAttendance.objects.filter(pk=teilnahme.pk).update(status="excused")

    SessionTenant.objects.filter(pk=tenant.pk).update(session_new_design=False)
    alt = client.post(adresse, daten, HTTP_HX_REQUEST="true")
    assert "session/partials/attendance_row.html" in _vorlagen(alt) and "<tr" in alt.content.decode()
    assert SessionAttendance.objects.get(pk=teilnahme.pk).status == "present"


# =============================================================================
# Darstellung
# =============================================================================


def test_kalender_am_handy_als_liste_und_breit_als_raster() -> None:
    tenant = _mandant("kalender2a", neu=True)
    welt = _welt_2a(tenant)
    html = _client(tenant, "Administrator").get(welt["kalender"]).content.decode()
    raster = html[html.index('data-testid="monatsraster"') : html.index('data-testid="monatsliste"')]
    liste = html[html.index('data-testid="monatsliste"') :]
    sitzung_url = reverse(
        "session:meeting_detail", kwargs={"tenant_slug": tenant.slug, "meeting_id": welt["sitzung_obj"].pk}
    )
    assert sitzung_url in raster and sitzung_url in liste
    assert "line-through" in liste and "nichtöffentlich" in liste


def test_keine_farbigen_fuellknoepfe_in_unterbereichen() -> None:
    """Auf den neuen Seiten trägt höchstens die eine Hauptaktion im Kopfband die Füllfarbe, nie ein Unterbereich."""
    tenant = _mandant("knoepfe2a", neu=True)
    welt = _welt_2a(tenant)
    for name in (*SEITEN, *EINGEBETTET):
        client = _client(tenant, "Revision" if name == "audit" else "Administrator")
        html = client.get(welt[name]).content.decode()
        inhalt = html[html.index('class="work-page-content"') : html.index("</main>")]
        fuellung = inhalt.count("bg-primary-600 hover:bg-primary-700")
        erlaubt = 1 if name == "vertretungen" else 0
        assert fuellung <= erlaubt, f"{name}: {fuellung} gefüllte Knöpfe im Inhalt"
        for farbe in ("green", "blue", "indigo", "amber"):
            assert f"bg-{farbe}-600 hover:bg-{farbe}-700" not in inhalt, f"{name}: Knopf in {farbe}"


def test_listen_nicht_in_12_px() -> None:
    """Die neuen Vorlagen setzen Listen in 13 bis 14 px, nicht in text-xs (12 px) oder kleiner."""
    from pathlib import Path

    neu = Path(__file__).resolve().parents[3] / "templates" / "session" / "neu"
    dateien = [*(f"{n}.html" for n in ("gremium", "person", "antrag", "sitzungsgelder", "monatspauschalen", "vertretungen", "kalender", "audit")),
               "_anwesenheit.html", "_anwesenheit_zeile.html", "_stoerungen.html", "_anlagen.html", "_beratungsfolge.html",
               "_station_bearbeiten.html", "_bezuege.html", "_gremium_mitglied.html", "_kalender_eintrag.html",
               "_sitzungsgelder_einstellungen.html", "_monatspauschalen_katalog.html", "_vertretungen_liste.html",
               "_audit_kontrolle.html"]  # fmt: skip
    for datei in dateien:
        text = (neu / datei).read_text(encoding="utf-8")
        for klein in ("text-xs", "text-[11px]", "text-[10px]", "text-[0.75rem]"):
            assert klein not in text, f"{datei}: {klein}"


def test_beratungsfolge_nennt_sitzungen_ungekuerzt() -> None:
    """Issue #1018: Bei 40 Zeichen abgeschnittene Namen ließen sich in der Auswahl nicht unterscheiden."""
    tenant = _mandant("lang2a", neu=True)
    welt = _welt_2a(tenant)
    lang = "Sondersitzung des Rates zum Haushalt und zur Mobilität, Teil zwei"
    SessionMeeting.objects.filter(pk=welt["sitzung_obj"].pk).update(name=lang)
    html = _client(tenant, "Administrator").get(welt["vorlage"]).content.decode()
    # Hinzufügen und je Station der Beratungsfolge im Rat, die noch keinen TOP hat
    assert html.count(f"{lang}</option>") >= 2
    assert lang[:39] + "…" not in html
