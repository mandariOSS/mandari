# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Seitenrundgang Sitzungen (Teil von #708): Jede Seite des Bereichs Sitzungen, Tagesordnung, Ladung, Anwesenheit,
Abstimmung und Niederschrift antwortet allen Standardrollen ohne Serverfehler.

Abgeleitet aus dem automatischen Durchlauf der Fehlersuche, aber klein gehalten:
- Muster: alle URL-Muster von ``apps.session``, deren View in einem Modul des Bereichs liegt (``BEREICH``).
  Ein neues Muster ohne Testobjekt für einen Platzhalter lässt den Rundgang scheitern – so wächst er mit.
- Bestand: eine Kommune mit kommender, nichtöffentlicher, abgesagter und vergangener Sitzung (genehmigte
  Niederschrift mit namentlicher Abstimmung, Berichtigung), Niederschrift in Prüfung, Ladung mit Rückmeldung
  und Vertretungsanfrage, Nachtrag, Anwesenheit mit Zuschaltung und Störung, Umlauf und Sitzungsmappe.
- Rollen: die sechs Standardrollen des Mandanten, ein Konto ohne Rolle und ein anonymer Aufruf.
- Aufrufe: GET je Rolle, mit HTMX-Kopf für die Verwaltung; Anfrageparameter, die eine View liest, mit
  Grenzwerten (0, sehr groß, unlesbar); jede POST-Route einmal leer und einmal mit unlesbaren Werten (in
  einer zurückgerollten Transaktion).

Geprüft wird nur „kein Serverfehler“: Der Testclient reicht Ausnahmen der View durch, Statuscodes ab 500
gelten als Fehler. Fachliche Ergebnisse prüfen die Tests der einzelnen Funktionen.
"""

from __future__ import annotations

import inspect
import itertools
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, time, timedelta
from typing import Any, cast

import pytest
from django.core.cache import cache
from django.db import transaction
from django.test import Client, override_settings
from django.urls import URLPattern, reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.common.tests.factories import UserFactory
from apps.session import urls as session_urls
from apps.session.models import (
    SessionAgendaItem,
    SessionAttendance,
    SessionAttendanceDisruption,
    SessionCircularResolution,
    SessionInvitationDispatch,
    SessionLegislativeTerm,
    SessionMeeting,
    SessionMeetingPackage,
    SessionOrganization,
    SessionOrganizationMembership,
    SessionPaper,
    SessionPerson,
    SessionProtocol,
    SessionProtocolCorrection,
    SessionRole,
    SessionTenant,
    SessionUser,
)
from apps.session.services import attendance_service, invitation_response_service, invitation_service, voting_service

pytestmark = pytest.mark.django_db

#: View-Module des Bereichs Sitzungen, Tagesordnung, Ladung, Anwesenheit, Abstimmung, Niederschrift
BEREICH = frozenset(
    f"apps.session.views.{name}"
    for name in (
        "meetings",
        "agenda",
        "attendance",
        "voting",
        "protocols",
        "invitations",
        "invitation_responses",
        "calendar",
        "resolutions",
        "packages",
        "cockpit",
    )
)
VERWALTUNG = "admin"
ROLLEN = [*SessionRole.default_role_definitions(), "ohne_rolle", "anonym"]
#: Grenzwerte für Anfrageparameter, die eine View liest
PARAMETERWERTE = ("0", "99999999999", "-1", "abc", "²")
#: unlesbare Werte für POST-Formulare (Kennungen, Zahlen, Auswahlfelder, Uhrzeiten)
UNSINN = "abc"
POST_FELDER = (
    "person", "attendance", "approval_meeting", "approval_item", "meeting", "organization", "paper", "parent",
    "status", "direction", "order", "dispatch_type", "voting_method", "vote_result", "is_election", "variant",
    "started_at", "ended_at", "cause", "action", "deadline", "date_from", "date_to", "weekday", "time", "rhythm",
    "start", "end", "votes_yes", "votes_no", "votes_abstain", "role", "participation_mode", "arrival_time",
    "departure_time", "name", "title", "reason", "next", "restore", "is_public", "format", "meeting_state",
)  # fmt: skip


@dataclass
class Rundgang:
    tenant: SessionTenant
    #: Platzhalter im URL-Muster -> Werte (mehrere: jede Variante wird aufgerufen)
    werte: dict[str, list[Any]]
    clients: dict[str, Client]
    users: list[User] = field(default_factory=list)


def _person(tenant: SessionTenant, name: str, **extra: Any) -> SessionPerson:
    return SessionPerson.objects.create(
        tenant=tenant, given_name="Rundgang", family_name=name, email=f"{name.lower()}@example.org", **extra
    )


def _top(meeting: SessionMeeting, nummer: str, order: int, name: str, **extra: Any) -> SessionAgendaItem:
    return SessionAgendaItem.objects.create(meeting=meeting, number=nummer, order=order, name=name, **extra)


def _baue() -> Rundgang:
    jetzt = timezone.now()
    tenant = SessionTenant.objects.create(
        name="Stadt Rundgang", slug="rundgang", oparl_public_since=jetzt, contact_email="rathaus@example.org"
    )
    rollen = SessionRole.create_default_roles(tenant)
    SessionLegislativeTerm.objects.create(tenant=tenant, name="22. WP", number=22, start_date=date(2020, 1, 1))
    gremium = SessionOrganization.objects.create(tenant=tenant, name="Hauptausschuss", invitation_period_days=7)
    mitglieder = [_person(tenant, name) for name in ("Amsel", "Buche", "Carl")]
    brief = _person(tenant, "Brief", delivery_channel="letter")
    vertretung = _person(tenant, "Vertretung")
    gast = _person(tenant, "Gast")
    for person in [*mitglieder, brief]:
        SessionOrganizationMembership.objects.create(organization=gremium, person=person)
    SessionOrganizationMembership.objects.create(organization=gremium, person=vertretung, substitute_for=mitglieder[0])
    vorlage = SessionPaper.objects.create(tenant=tenant, reference="V/1", name="Radweg", status="approved")
    vorlage_noe = SessionPaper.objects.create(tenant=tenant, reference="V/2", name="Grundstück", is_public=False)

    def sitzung(name: str, tage: int, **extra: Any) -> SessionMeeting:
        start = (jetzt + timedelta(days=tage)).replace(hour=17, minute=0, second=0, microsecond=0)
        meeting = SessionMeeting(tenant=tenant, name=name, organization=gremium, start=start, **extra)
        meeting.assign_legislative_term()
        meeting.save()
        return meeting

    # Kommende Sitzung: hybride Sitzung mit Tagesordnung, Ladung, Rückmeldung, Vertretung, Nachtrag, Störung
    kommend = sitzung("Kommende Sitzung", 14, meeting_state="scheduled")
    SessionMeeting.objects.filter(pk=kommend.pk).update(format=SessionMeeting.FORMAT_HYBRID)
    kommend.refresh_from_db()
    top = _top(kommend, "1", 1, "Radweg", paper=vorlage)
    _top(kommend, "1.1", 2, "Abschnitt Nord", parent=top)
    top_noe = _top(kommend, "N1", 3, "Grundstück", is_public=False, paper=vorlage_noe)
    _top(kommend, "2", 4, "Verschiedenes", is_end_item=True)
    attendance_service.generate_attendance(kommend)
    invitation_service.send_invitations(kommend, sent_by=None)
    invitation_response_service.record_response(
        kommend, mitglieder[0], decision="decline", source="link", reason="Urlaub", substitute_requested=True
    )
    _top(kommend, "3", 5, "Nachtrag", is_supplementary=True)
    SessionAttendance.objects.get_or_create(meeting=kommend, person=gast, defaults={"role": "guest"})
    zugeschaltet = SessionAttendance.objects.get(meeting=kommend, person=mitglieder[1])
    zugeschaltet.participation_mode = SessionAttendance.PARTICIPATION_REMOTE
    zugeschaltet.save()
    stoerung = SessionAttendanceDisruption.objects.create(attendance=zugeschaltet, started_at=time(17, 30))
    paket = SessionMeetingPackage.objects.create(tenant=tenant, meeting=kommend, variant="public", version=1)

    noe = sitzung("Nichtöffentliche Sitzung", 21, is_public=False)
    abgesagt = sitzung("Abgesagte Sitzung", 28, cancelled=True)

    # Vergangene Sitzung: genehmigte Niederschrift mit namentlicher Abstimmung und offener Berichtigung
    vergangen = sitzung("Vergangene Sitzung", -14, meeting_state="completed")
    beschluss = _top(vergangen, "1", 1, "Haushalt", voting_method="roll_call", vote_result="approved")
    for person in mitglieder:
        SessionAttendance.objects.create(meeting=vergangen, person=person, status="present")
    voting_service.capture_votes(beschluss, dict.fromkeys(mitglieder, "yes"), recorded_by=None)
    protokoll = SessionProtocol.objects.create(meeting=vergangen, content="Niederschrift", status="approved")
    berichtigung = SessionProtocolCorrection.objects.create(protocol=protokoll, target="item", agenda_item=beschluss)

    # Niederschrift in Prüfung
    pruefung = sitzung("Sitzung in Prüfung", -7, meeting_state="completed")
    _top(pruefung, "1", 1, "Eröffnung")
    SessionProtocol.objects.create(meeting=pruefung, content="Entwurf", status="review")

    umlauf = SessionCircularResolution.objects.create(
        tenant=tenant, organization=gremium, title="Umlauf", resolution_text="Text", deadline=jetzt.date()
    )

    clients: dict[str, Client] = {"anonym": Client()}
    users: list[User] = []
    for schluessel, rolle in rollen.items():
        user = cast(User, cast(Any, UserFactory)(email=f"rundgang-{schluessel}@example.org"))
        session_user = SessionUser.objects.create(user=user, tenant=tenant)
        session_user.roles.add(rolle)
        clients[schluessel] = Client()
        clients[schluessel].force_login(user)
        users.append(user)
    user = cast(User, cast(Any, UserFactory)(email="rundgang-ohne-rolle@example.org"))
    SessionUser.objects.create(user=user, tenant=tenant)
    clients["ohne_rolle"] = Client()
    clients["ohne_rolle"].force_login(user)
    users.append(user)

    werte: dict[str, list[Any]] = {
        "tenant_slug": [tenant.slug],
        "meeting_id": [kommend.pk, noe.pk, abgesagt.pk, vergangen.pk, pruefung.pk],
        "item_id": [top.pk, top_noe.pk, beschluss.pk],
        "attendance_id": [zugeschaltet.pk],
        "disruption_id": [stoerung.pk],
        "person_id": [mitglieder[0].pk],
        "dispatch_id": list(SessionInvitationDispatch.objects.filter(meeting=kommend).values_list("pk", flat=True)),
        "fmt": ["pdf", "csv", "zip"],
        "package_id": [paket.pk],
        "correction_id": [berichtigung.pk],
        "decision": ["confirm"],
        "action": ["submit"],
        "circular_id": [umlauf.pk],
        "org_id": [gremium.pk],
    }
    return Rundgang(tenant=tenant, werte=werte, clients=clients, users=users)


@pytest.fixture(scope="module")
def rundgang(
    django_db_setup: None, django_db_blocker: Any, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Rundgang]:
    media = tmp_path_factory.mktemp("rundgang-media")
    with override_settings(MEDIA_ROOT=str(media)), django_db_blocker.unblock():
        gebaut = _baue()
        yield gebaut
        SessionTenant.objects.filter(pk=gebaut.tenant.pk).delete()
        User.objects.filter(pk__in=[user.pk for user in gebaut.users]).delete()


@pytest.fixture(autouse=True)
def _ohne_drossel() -> None:
    cache.clear()


# =============================================================================
# Muster und Adressen
# =============================================================================


def _view(pattern: URLPattern) -> Any:
    return getattr(pattern.callback, "view_class", pattern.callback)


def _muster() -> list[URLPattern]:
    return [
        p for p in session_urls.urlpatterns if isinstance(p, URLPattern) and _view(p).__module__ in BEREICH and p.name
    ]


def _platzhalter(pattern: URLPattern) -> list[str]:
    return re.findall(r"<(?:\w+:)?(\w+)>", str(pattern.pattern))


def _adressen(pattern: URLPattern, werte: dict[str, list[Any]]) -> list[str]:
    namen = _platzhalter(pattern)
    return [
        reverse(f"session:{pattern.name}", kwargs=dict(zip(namen, kombi, strict=True)))
        for kombi in itertools.product(*(werte[name] for name in namen))
    ]


def _methoden(pattern: URLPattern) -> set[str]:
    view = _view(pattern)
    erlaubt = set(getattr(view, "http_method_names", ["get", "post"]))
    return {m for m in ("get", "post") if m in erlaubt and hasattr(view, m)}


def _gelesene_parameter(pattern: URLPattern) -> list[str]:
    """Anfrageparameter, die der Quelltext der View liest (``GET.get("…")``, ``GET["…"]``)."""
    return sorted(set(re.findall(r"GET(?:\.get\(|\[)\s*[\"'](\w+)[\"']", inspect.getsource(_view(pattern)))))


def _pruefe(antwort: Any, beschreibung: str, fehler: list[str]) -> None:
    if antwort.status_code >= 500:
        fehler.append(f"{beschreibung}: {antwort.status_code}")


# =============================================================================
# Tests
# =============================================================================


def test_jedes_muster_hat_testobjekte(rundgang: Rundgang) -> None:
    muster = _muster()
    assert len(muster) >= 50, "Bereich ohne Muster – stimmen die Modulnamen in BEREICH noch?"
    fehlend = {
        f"{p.name}: {name}" for p in muster for name in _platzhalter(p) if not rundgang.werte.get(name)
    }  # fmt: skip
    assert not fehlend, f"Neue Platzhalter ohne Testobjekt im Seitenrundgang: {sorted(fehlend)}"


@pytest.mark.parametrize("rolle", ROLLEN)
def test_seiten_ohne_serverfehler(rundgang: Rundgang, rolle: str) -> None:
    client = rundgang.clients[rolle]
    fehler: list[str] = []
    for pattern in _muster():
        if "get" not in _methoden(pattern):
            continue
        for adresse in _adressen(pattern, rundgang.werte):
            _pruefe(client.get(adresse), f"GET {adresse}", fehler)
            if rolle == VERWALTUNG:
                _pruefe(client.get(adresse, HTTP_HX_REQUEST="true"), f"GET (HTMX) {adresse}", fehler)
    assert not fehler, f"Rolle {rolle}: " + "; ".join(fehler)


def test_anfrageparameter_ohne_serverfehler(rundgang: Rundgang) -> None:
    client = rundgang.clients[VERWALTUNG]
    fehler: list[str] = []
    for pattern in _muster():
        if "get" not in _methoden(pattern):
            continue
        parameter = _gelesene_parameter(pattern)
        if not parameter:
            continue
        adresse = _adressen(pattern, rundgang.werte)[0]
        for name, wert in itertools.product(parameter, PARAMETERWERTE):
            _pruefe(client.get(adresse, {name: wert}), f"GET {adresse}?{name}={wert}", fehler)
    assert not fehler, "; ".join(fehler)


def test_formulare_mit_unsinnigen_werten_ohne_serverfehler(rundgang: Rundgang) -> None:
    client = rundgang.clients[VERWALTUNG]
    fehler: list[str] = []
    unsinn = dict.fromkeys(POST_FELDER, UNSINN)
    for pattern in _muster():
        if "post" not in _methoden(pattern):
            continue
        for adresse in _adressen(pattern, rundgang.werte):
            for daten in ({}, unsinn):
                with transaction.atomic():
                    antwort = client.post(adresse, daten)
                    transaction.set_rollback(True)
                _pruefe(antwort, f"POST {adresse} {'leer' if not daten else 'unsinnig'}", fehler)
    assert not fehler, "; ".join(fehler)
