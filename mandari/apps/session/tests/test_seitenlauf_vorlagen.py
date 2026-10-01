# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Seitenlauf durch Vorlagen, Anträge, Beratungsfolge, Mitzeichnung, Fassungen, Nummernkreise und Beschlüsse.

Abgeleitet aus dem Durchlauf durch das Session-RIS: Jede Route dieses Bereichs (aus dem URL-Resolver,
neue Routen kommen automatisch dazu) wird für jede Rolle aufgerufen – GET mit Vorlagen in allen
Zuständen, GET mit unsinnigen Filterwerten und POST mit leeren bzw. ungültigen Angaben (zurückgerollt).
Keine Anfrage darf mit einem Serverfehler oder einer Ausnahme enden, und kein Fehler darf im Log
landen. Ob eine Rolle eine Seite sehen darf, prüft ``test_security_matrix``; hier geht es nur darum,
dass keine Kombination aus Rolle, Objektzustand und Eingabe die Anwendung zum Absturz bringt.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, cast

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import Client, override_settings
from django.urls import URLPattern, reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.common.tests.factories import UserFactory
from apps.session import urls as session_urls
from apps.session.models import (
    SessionAgendaItem,
    SessionApplication,
    SessionConsultation,
    SessionCosignature,
    SessionCosignatureRule,
    SessionFile,
    SessionMeeting,
    SessionNumberRange,
    SessionOrganization,
    SessionPaper,
    SessionPaperVersion,
    SessionPerson,
    SessionRole,
    SessionTenant,
    SessionUser,
)
from apps.session.services import paper_version_service
from apps.session.services.application_service import convert_to_paper

pytestmark = pytest.mark.django_db

#: Routen dieses Bereichs: Name beginnt mit einem dieser Präfixe (Schnittstellen „api_*“ gehören nicht dazu)
BEREICH = (
    "paper",
    "consultation",
    "application",
    "my_cosignatures",
    "cosign",
    "settings_cosign",
    "department_assignment",
    "settings_numbering",
    "resolution",
    "file_",
)

ALLE_RECHTE = [f.name[4:] for f in SessionRole._meta.get_fields() if f.name.startswith("can_") and f.concrete]

#: Rolle → Rechte (None: nicht angemeldet; "ohne_mandant": angemeldet, aber ohne Zugang zum Mandanten)
ROLLEN: dict[str, set[str] | str | None] = {
    "admin": "admin",
    "sachbearbeitung": {
        "view_dashboard",
        "view_meetings",
        "edit_meetings",
        "view_papers",
        "create_papers",
        "edit_papers",
        "view_applications",
        "process_applications",
    },
    "freigabe": {
        "view_meetings",
        "view_non_public_meetings",
        "view_papers",
        "view_non_public_papers",
        "approve_papers",
    },
    "lesezugriff": {"view_dashboard", "view_meetings", "view_papers", "view_applications"},
    "einstellungen": {"manage_settings"},
    "benutzerverwaltung": {"manage_users"},
    "ohne_rechte": set(),
    "ohne_mandant": "ohne_mandant",
    "anonym": None,
}
#: Rollen mit Rechten in diesem Bereich: alle Objektzustände und ungültige Eingaben. Die übrigen
#: scheitern schon an der Rechteprüfung – für sie genügt je Route ein Aufruf (Laufzeit).
VOLLSTAENDIG = {"admin", "sachbearbeitung", "freigabe", "lesezugriff", "einstellungen"}

#: Unsinnige Werte für Filter in der Adresszeile – alle zugleich …
FUZZ_GET = {
    "year": "0",
    "organization": "kaputt",
    "status": "kaputt",
    "result": "kaputt",
    "type": "kaputt",
    "term": "kaputt",
    "page": "kaputt",
    "overdue": "1",
    "q": "%",
    "a": "x",
    "b": "-1",
}
#: … und einzeln: Ein Filter, der schon leer macht (``organization=kaputt``), verdeckt sonst die übrigen
FUZZ_GET_EINZELN = [
    ("year", "0"),
    ("year", "99999999999"),
    ("year", "abc"),
    ("organization", "kaputt"),
    ("status", "kaputt"),
    ("result", "kaputt"),
    ("type", "kaputt"),
    ("term", "kaputt"),
    ("page", "kaputt"),
    ("page", "0"),
    ("page", "99999999999"),
    ("overdue", "1"),
    ("q", "%"),
    ("a", "x"),
    ("a", "99999999999"),
    ("b", "-1"),
]
#: Rollen, für die jeder Filter einzeln geprüft wird (Laufzeit; die Filter hängen nicht an der Rolle)
EINZELN = {"admin"}
#: Unsinnige Angaben für Formulare
FUZZ_POST = {
    "organization": "kaputt",
    "meeting": "kaputt",
    "department": "kaputt",
    "session_user": "kaputt",
    "rule_id": "kaputt",
    "range_id": "kaputt",
    "target_type": "paper",
    "target_id": "kaputt",
    "main_organization": "kaputt",
    "lead_department": "kaputt",
    "relation_type": "kaputt",
    "paper_type": "kaputt",
    "status": "kaputt",
    "result": "kaputt",
    "role": "kaputt",
    "direction": "kaputt",
    "order": "x",
    "next_number": "x",
    "deadline": "kaputt",
    "date": "kaputt",
    "comment": "x",
    "note": "x",
    "recipient": "x",
    "method": "kaputt",
    "preset": "kaputt",
    "pattern": "{kaputt",
    "reset": "kaputt",
}
#: Zusätzliche POST-Varianten je Route (mit FUZZ_POST zusammengeführt)
POST_VARIANTEN: dict[str, list[dict[str, str]]] = {
    "settings_numbering_save": [{"action": aktion} for aktion in ("label", "range", "preset", "next")],
    "cosign_rule_manage": [{"action": "delete"}],
    "department_assignment": [{"action": "remove"}],
}
#: Routen, deren POST für jeden Objektzustand geprüft wird (Freigabelauf und Statuswechsel)
POST_ALLE_ZUSTAENDE = {
    "paper_workflow",
    "paper_edit",
    "cosignature_action",
    "application_convert",
    "application_process",
}


@dataclass
class Welt:
    tenant: SessionTenant
    werte: dict[str, list[Any]]
    clients: dict[str, Client] = field(default_factory=dict)
    users: list[User] = field(default_factory=list)


def _konto(welt: Welt, name: str, rechte: set[str] | str | None) -> Client:
    client = Client()
    if rechte is None:
        return client
    user = cast(User, UserFactory(email=f"lauf-{name}@example.org"))  # type: ignore[no-untyped-call]
    welt.users.append(user)
    client.force_login(user)
    if rechte == "ohne_mandant":
        return client
    admin = rechte == "admin"
    flags = {f"can_{recht}": (not admin and recht in rechte) for recht in ALLE_RECHTE}
    role = SessionRole.objects.create(tenant=welt.tenant, name=f"lauf-{name}", is_admin=admin, **flags)
    session_user = SessionUser.objects.create(user=user, tenant=welt.tenant)
    session_user.roles.add(role)
    return client


def _bauen() -> Welt:
    tenant = SessionTenant.objects.create(name="Stadt Seitenlauf", slug="seitenlauf")
    welt = Welt(tenant=tenant, werte={})
    for name, rechte in ROLLEN.items():
        welt.clients[name] = _konto(welt, name, rechte)
    admin = SessionUser.objects.get(tenant=tenant, user__email="lauf-admin@example.org")
    sachbearbeitung = SessionUser.objects.get(tenant=tenant, user__email="lauf-sachbearbeitung@example.org")
    freigabe = SessionUser.objects.get(tenant=tenant, user__email="lauf-freigabe@example.org")

    jetzt = timezone.now()
    ha = SessionOrganization.objects.create(tenant=tenant, name="Hauptausschuss", short_name="HA")
    bau = SessionOrganization.objects.create(tenant=tenant, name="Bauausschuss", short_name="BA")
    amt = SessionOrganization.objects.create(tenant=tenant, name="Kämmerei", organization_type="department")
    freigabe.departments.add(amt)
    person = SessionPerson.objects.create(tenant=tenant, given_name="Anna", family_name="Antrag")
    SessionNumberRange.objects.create(
        tenant=tenant, name="Je Gremium", pattern="{gremium}/{lfd:3}/{jahr}", reset="yearly", is_active=False
    )
    SessionCosignatureRule.objects.create(tenant=tenant, department=amt, order=1)

    kommend = SessionMeeting.objects.create(
        tenant=tenant, name="Hauptausschuss", organization=ha, start=jetzt + timedelta(days=7)
    )
    kommend.joint_organizations.add(bau)
    vergangen = SessionMeeting.objects.create(
        tenant=tenant, name="Hauptausschuss alt", organization=ha, start=jetzt - timedelta(days=60)
    )
    SessionMeeting.objects.create(
        tenant=tenant, name="Nichtöffentlich", organization=ha, start=jetzt + timedelta(days=9), is_public=False
    )

    gemeinsam = {
        "main_organization": ha,
        "lead_department": amt,
        "originator_organization": bau,
        "originator_person": person,
        "created_by": sachbearbeitung,
        "date": timezone.localdate(),
    }
    entwurf = SessionPaper.objects.create(tenant=tenant, name="Entwurf", has_financial_impact=True, **gemeinsam)
    pruefung = SessionPaper.objects.create(
        tenant=tenant, name="In Prüfung", status="review", has_financial_impact=False, **gemeinsam
    )
    frei = SessionPaper.objects.create(
        tenant=tenant, name="Freigegeben", status="approved", approved_by=admin, approved_at=jetzt, **gemeinsam
    )
    zurueckgezogen = SessionPaper.objects.create(
        tenant=tenant, name="Zurückgezogen", status="withdrawn", approved_at=jetzt, **gemeinsam
    )
    noe = SessionPaper.objects.create(tenant=tenant, name="Nichtöffentlich", is_public=False, **gemeinsam)
    SessionPaper.objects.create(
        tenant=tenant, name="Ergänzung", parent_paper=frei, relation_type="supplement", **gemeinsam
    )

    SessionCosignature.objects.create(paper=pruefung, department=amt, order=0, status="signed", decided_by=freigabe)
    offen = SessionCosignature.objects.create(paper=pruefung, department=amt, order=1)

    station = SessionConsultation.objects.create(paper=frei, organization=ha, meeting=kommend, order=1)
    ohne_sitzung = SessionConsultation.objects.create(paper=frei, organization=bau, order=2)
    SessionConsultation.objects.create(paper=entwurf, organization=ha, order=1)

    beschluss = SessionAgendaItem.objects.create(
        meeting=vergangen,
        number="1",
        name="Beschluss",
        paper=frei,
        vote_result="approved",
        implementation_status="in_progress",
        implementation_deadline=timezone.localdate() - timedelta(days=3),
    )

    datei = SessionFile.objects.create(
        tenant=tenant,
        name="anlage.txt",
        file=SimpleUploadedFile("anlage.txt", b"Inhalt der Anlage"),
        is_public=True,
        paper=entwurf,
    )
    SessionFile.objects.create(
        tenant=tenant,
        name="noe-anlage.txt",
        file=SimpleUploadedFile("noe-anlage.txt", b"Vertraulich"),
        is_public=False,
        paper=frei,
    )
    manuell = SessionPaperVersion.TRIGGER_MANUAL
    fassung = paper_version_service.snapshot(entwurf, trigger=manuell)
    paper_version_service.snapshot(entwurf, trigger=manuell)
    eintrag = fassung.files.get(attachment_id=datei.id)

    eingereicht = SessionApplication.objects.create(
        tenant=tenant,
        title="Mehr Bänke",
        justification="Begründung",
        resolution_proposal="Beschluss",
        financial_impact="ca. 5.000 Euro",
        submitter_name="Fraktion",
        submitter_email="fraktion@example.org",
        target_organization=ha,
    )
    umgewandelt = SessionApplication.objects.create(
        tenant=tenant,
        title="Radweg",
        justification="Begründung",
        resolution_proposal="Beschluss",
        submitter_name="Fraktion",
        submitter_email="fraktion@example.org",
    )
    aus_antrag, _ = convert_to_paper(umgewandelt, session_user=admin)
    abgelehnt = SessionApplication.objects.create(
        tenant=tenant,
        title="Abgelehnt",
        justification="Begründung",
        resolution_proposal="Beschluss",
        submitter_name="Fraktion",
        submitter_email="fraktion@example.org",
        status="rejected",
    )

    welt.werte = {
        "tenant_slug": [tenant.slug],
        "paper_id": [entwurf.id, pruefung.id, frei.id, zurueckgezogen.id, noe.id, aus_antrag.id],
        "application_id": [eingereicht.id, umgewandelt.id, abgelehnt.id],
        "consultation_id": [station.id, ohne_sitzung.id],
        "cosign_id": [offen.id],
        "meeting_id": [vergangen.id, kommend.id],
        "item_id": [beschluss.id],
        "number": [1, 99],
        "entry_id": [eintrag.id],
        "file_id": [datei.id],
        "blob_id": [eintrag.blob_id],
        "action:paper_workflow": ["submit", "approve", "reject", "unbekannt"],
        "action:cosignature_action": ["sign", "reject", "unbekannt"],
    }
    return welt


@pytest.fixture(scope="module")
def welt(django_db_setup: None, django_db_blocker: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Welt]:
    """Modulweite Testdaten; jede schreibende Anfrage wird zurückgerollt."""
    media_root = tmp_path_factory.mktemp("seitenlauf-media")
    with override_settings(MEDIA_ROOT=str(media_root)), django_db_blocker.unblock():
        gebaut = _bauen()
        yield gebaut
        # Unternummern zuerst: Die Bezugsvorlage ist geschützt, solange es sie gibt
        SessionPaper.objects.filter(tenant=gebaut.tenant, parent_paper__isnull=False).delete()
        SessionTenant.objects.filter(pk=gebaut.tenant.pk).delete()
        User.objects.filter(pk__in=[user.pk for user in gebaut.users]).delete()


def _routen() -> list[URLPattern]:
    return [
        muster
        for muster in session_urls.urlpatterns
        if isinstance(muster, URLPattern) and muster.name and muster.name.startswith(BEREICH)
    ]


def _parameter(muster: URLPattern) -> list[str]:
    return list(cast(Any, muster.pattern).converters)


def _werte(welt: Welt, muster: URLPattern, name: str) -> list[Any]:
    return welt.werte.get(f"{name}:{muster.name}") or welt.werte[name]


def _adressen(welt: Welt, muster: URLPattern, *, alle: bool) -> list[str]:
    """Adressen einer Route: Vorgabewerte, mit ``alle`` jeder Wert des ersten Objektparameters."""
    namen = _parameter(muster)
    vorgabe = {name: _werte(welt, muster, name)[0] for name in namen}
    varianten = [vorgabe]
    objekt = next((name for name in namen if name != "tenant_slug"), None)
    if alle and objekt is not None:
        varianten = [{**vorgabe, objekt: wert} for wert in _werte(welt, muster, objekt)]
    # Aktionen (Freigabelauf, Mitzeichnung) immer alle
    if "action" in namen:
        varianten = [{**v, "action": aktion} for v in varianten for aktion in _werte(welt, muster, "action")]
    return [reverse(f"session:{muster.name}", kwargs=v) for v in varianten]


def test_bereich_hat_routen_und_alle_parameter_sind_belegt() -> None:
    routen = _routen()
    assert len(routen) >= 40, [m.name for m in routen]
    bekannt = {
        "tenant_slug",
        "paper_id",
        "application_id",
        "consultation_id",
        "cosign_id",
        "meeting_id",
        "item_id",
        "number",
        "entry_id",
        "file_id",
        "blob_id",
        "action",
    }
    offen = {(m.name, name) for m in routen for name in _parameter(m) if name not in bekannt}
    assert not offen, f"Neue Parameter im Seitenlauf belegen: {sorted(offen)}"


class _Fehlerlog(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.eintraege: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.eintraege.append(f"{record.name}: {record.getMessage()[:300]}")


@pytest.mark.parametrize("rolle", list(ROLLEN))
def test_seitenlauf_ohne_serverfehler(welt: Welt, rolle: str) -> None:
    client = welt.clients[rolle]
    befunde: list[str] = []
    log = _Fehlerlog()
    logging.getLogger().addHandler(log)

    def aufruf(methode: str, url: str, daten: dict[str, str] | None = None, kopf: dict[str, str] | None = None) -> int:
        try:
            with transaction.atomic():
                if methode == "GET":
                    antwort = client.get(url, daten or {}, headers=kopf)
                else:
                    antwort = client.post(url, daten or {}, headers=kopf)
                transaction.set_rollback(True)
        except Exception as exc:  # Ausnahme der Anwendung – im Bericht sammeln statt abzubrechen
            befunde.append(f"{methode} {url} {daten or ''}: {type(exc).__name__}: {exc}"[:500])
            return 500
        if antwort.status_code >= 500:
            befunde.append(f"{methode} {url} {daten or ''}: HTTP {antwort.status_code}")
        return int(antwort.status_code)

    voll = rolle in VOLLSTAENDIG
    try:
        for muster in _routen():
            for url in _adressen(welt, muster, alle=voll):
                aufruf("GET", url)
            for url in _adressen(welt, muster, alle=False):
                aufruf("POST", url)
                if not voll:
                    continue
                if aufruf("GET", url, FUZZ_GET) == 200 and rolle in EINZELN:
                    for schluessel, wert in FUZZ_GET_EINZELN:
                        aufruf("GET", url, {schluessel: wert})
                if rolle == "admin":
                    aufruf("GET", url, kopf={"HX-Request": "true"})
            if not voll:
                continue
            for url in _adressen(welt, muster, alle=muster.name in POST_ALLE_ZUSTAENDE):
                aufruf("POST", url, FUZZ_POST)
                for variante in POST_VARIANTEN.get(str(muster.name), []):
                    aufruf("POST", url, {**FUZZ_POST, **variante})
    finally:
        logging.getLogger().removeHandler(log)

    befunde += [f"Fehler im Log: {eintrag}" for eintrag in log.eintraege]
    assert not befunde, f"Rolle {rolle}:\n" + "\n".join(befunde)
