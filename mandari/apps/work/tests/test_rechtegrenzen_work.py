# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rechtegrenzen in Work, die an mehreren Stellen dieselbe Regel brauchen.

- Fraktionssitzung: Aufgaben aus Protokolleinträgen nur mit ``tasks.create``; nach genehmigtem Protokoll sind
  Tagesordnung und Beschlüsse fest, auch wenn der Status der Sitzung zurückgesetzt wird.
- Aufgaben: Freigaben zählen nur bei Sichtbarkeit „geteilt“ und entfallen beim Wechsel auf eine andere.
- Gastseite: Titel freigegebener Dokumente nur, wenn die betrachtende Person das Dokument sehen oder die Freigabe
  verwalten darf.
- Einbettungs-Snippet der Termine: setzt Titel und Ort als Text, nicht als HTML.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from apps.common.tests.factories import MembershipFactory, UserFactory
from apps.work.faction import agenda
from apps.work.faction.models import FactionAgendaItem, FactionMeeting, FactionProtocolEntry
from apps.work.motions.models import Motion, MotionShare
from apps.work.organization import selectors
from apps.work.tasks import selectors as task_selectors
from apps.work.tasks import services as task_services
from apps.work.tasks.models import Task, TaskShare

pytestmark = pytest.mark.django_db

LEITUNG = ["faction.view_public", "faction.manage", "protocols.create", "protocols.edit", "tasks.create"]


@pytest.fixture
def leitung(org: Any, make_member: Any) -> Any:
    return make_member(org, LEITUNG, email="leitung@example.org")


@pytest.fixture
def sitzung(org: Any, leitung: Any) -> FactionMeeting:
    return FactionMeeting.objects.create(
        organization=org,
        title="Fraktionssitzung",
        start=timezone.now() - timedelta(minutes=30),
        status="ongoing",
        created_by=leitung,
    )


@pytest.fixture
def top(sitzung: FactionMeeting) -> FactionAgendaItem:
    return FactionAgendaItem.objects.create(meeting=sitzung, number="1", title="Haushalt", visibility="public", order=1)


def _genehmigen(meeting: FactionMeeting, *, status: str = "ongoing") -> None:
    """Protokoll genehmigt, Status danach zurückgesetzt (der Zustand, in dem die Sperre gelten muss)."""
    FactionMeeting.objects.filter(pk=meeting.pk).update(protocol_approved=True, status=status)
    meeting.refresh_from_db()


# --- Aufgabe aus Protokolleintrag -----------------------------------------------------------------------------


def _aufgabeneintrag(sitzung: FactionMeeting, top: FactionAgendaItem, leitung: Any) -> FactionProtocolEntry:
    eintrag: Any = FactionProtocolEntry(meeting=sitzung, agenda_item=top, entry_type="action", created_by=leitung)
    eintrag.save()
    eintrag.set_content_encrypted("Stellungnahme zum Haushalt vorbereiten")
    eintrag.save()
    return eintrag  # type: ignore[no-any-return]


def _uebernehmen(client_for: Any, mitglied: Any, org: Any, sitzung: FactionMeeting, eintrag: Any) -> Any:
    url = reverse("work:faction_action", kwargs={"org_slug": org.slug, "meeting_id": sitzung.pk})
    return client_for(mitglied.user).post(url, {"action": "create_task", "entry_id": str(eintrag.pk)})


def test_aufgabe_aus_eintrag_nur_mit_recht_aufgaben_anzulegen(
    org: Any, make_member: Any, client_for: Any, leitung: Any, sitzung: FactionMeeting, top: FactionAgendaItem
) -> None:
    eintrag = _aufgabeneintrag(sitzung, top, leitung)
    ohne = make_member(org, ["faction.view_public"], email="ohne@example.org")

    antwort = _uebernehmen(client_for, ohne, org, sitzung, eintrag)

    assert antwort.status_code == 403
    assert not Task.objects.filter(related_faction_meeting=sitzung).exists()
    eintrag.refresh_from_db()
    assert not eintrag.action_completed


def test_aufgabe_aus_eintrag_setzt_erledigt_nur_mit_protokollrecht(
    org: Any, make_member: Any, client_for: Any, leitung: Any, sitzung: FactionMeeting, top: FactionAgendaItem
) -> None:
    eintrag = _aufgabeneintrag(sitzung, top, leitung)
    mitglied = make_member(org, ["faction.view_public", "tasks.create"], email="mitglied@example.org")

    _uebernehmen(client_for, mitglied, org, sitzung, eintrag)

    # Die Aufgabe entsteht; den Erledigt-Stand setzt nur, wer protokollieren darf
    assert Task.objects.filter(related_faction_meeting=sitzung).count() == 1
    eintrag.refresh_from_db()
    assert not eintrag.action_completed

    zweiter = _aufgabeneintrag(sitzung, top, leitung)
    _uebernehmen(client_for, leitung, org, sitzung, zweiter)
    zweiter.refresh_from_db()
    assert zweiter.action_completed


# --- Genehmigtes Protokoll -----------------------------------------------------------------------------------


def test_genehmigtes_protokoll_sperrt_die_verwaltung_der_tagesordnung(
    org: Any, make_member: Any, leitung: Any, sitzung: FactionMeeting
) -> None:
    andere = make_member(org, ["faction.view_public"], email="andere@example.org")
    assert agenda.manages(sitzung, leitung) and agenda.editable_by(sitzung, leitung)
    assert not agenda.manages(sitzung, andere)

    _genehmigen(sitzung)

    assert not agenda.manages(sitzung, leitung)
    assert not agenda.editable_by(sitzung, leitung)


def test_tagesordnung_nach_genehmigung_auch_nach_zurueckgesetztem_status_fest(
    org: Any, client_for: Any, leitung: Any, sitzung: FactionMeeting, top: FactionAgendaItem
) -> None:
    url = reverse("work:faction_action", kwargs={"org_slug": org.slug, "meeting_id": sitzung.pk})
    _genehmigen(sitzung, status="ongoing")

    antwort = client_for(leitung.user).post(url, {"action": "delete_item", "item_id": str(top.pk)})

    assert antwort.status_code == 403
    assert FactionAgendaItem.objects.filter(pk=top.pk).exists()


def test_vorschlag_wird_nach_genehmigtem_protokoll_nicht_mehr_angenommen(
    org: Any, make_member: Any, client_for: Any, sitzung: FactionMeeting
) -> None:
    vorsitz = make_member(org, ["faction.view_public", "agenda.manage"], email="vorsitz@example.org")
    vorschlag = FactionAgendaItem.objects.create(
        meeting=sitzung,
        number="",
        title="Nachträglicher Punkt",
        visibility="public",
        order=5,
        proposal_status="proposed",
        proposed_by=vorsitz,
    )
    url = reverse("work:faction_action", kwargs={"org_slug": org.slug, "meeting_id": sitzung.pk})
    _genehmigen(sitzung)

    client_for(vorsitz.user).post(url, {"action": "accept_proposal", "item_id": str(vorschlag.pk)})

    vorschlag.refresh_from_db()
    assert vorschlag.proposal_status == "proposed"


# --- Aufgaben: Freigaben nur bei „geteilt“ -----------------------------------------------------------------------


def test_uebrig_gebliebene_freigabe_oeffnet_keine_private_aufgabe(org: Any, make_member: Any) -> None:
    autorin = make_member(org, ["tasks.view", "tasks.create"], email="autorin@example.org")
    empfaenger = make_member(org, ["tasks.view"], email="empfaenger@example.org")
    aufgabe = Task.objects.create(organization=org, title="Vertraulich", created_by=autorin, visibility="shared")
    TaskShare.objects.create(task=aufgabe, membership=empfaenger, shared_by=autorin)
    basis = Task.objects.filter(organization=org)
    assert aufgabe in task_selectors.own_tasks(empfaenger, base=basis)

    # Sichtbarkeit am Freigabedialog vorbei geändert (Altbestand): Die Zeile bleibt, öffnet aber nichts mehr
    Task.objects.filter(pk=aufgabe.pk).update(visibility="private")

    assert aufgabe not in task_selectors.own_tasks(empfaenger, base=basis)
    assert aufgabe not in task_selectors.visible_tasks(org, empfaenger)
    assert aufgabe in task_selectors.own_tasks(autorin, base=basis)


def test_wiederteilen_ohne_freigabedialog_beginnt_ohne_empfaenger(org: Any, make_member: Any) -> None:
    autorin = make_member(org, ["tasks.view", "tasks.create"], email="autorin@example.org")
    empfaenger = make_member(org, ["tasks.view"], email="empfaenger@example.org")
    aufgabe = Task.objects.create(organization=org, title="Vertraulich", created_by=autorin, visibility="shared")
    TaskShare.objects.create(task=aufgabe, membership=empfaenger, shared_by=autorin)

    def panel(sichtbarkeit: str) -> None:
        alt = task_services.capture_old_values(aufgabe)
        aufgabe.visibility = sichtbarkeit
        task_services.apply_panel_update(aufgabe, autorin, alt)

    # Unverändert geteilt: Die Empfänger bleiben
    panel("shared")
    assert TaskShare.objects.filter(task=aufgabe).count() == 1
    # Auf privat: Die Zeile bleibt ohne Wirkung stehen (ein veraltetes Panel löscht so keine neuen Freigaben)
    panel("private")
    assert TaskShare.objects.filter(task=aufgabe).count() == 1
    assert not aufgabe.can_access(empfaenger)
    # Wieder geteilt, ohne Empfänger zu wählen: Die früheren bekommen nicht ungefragt wieder Zugriff
    panel("shared")
    assert not TaskShare.objects.filter(task=aufgabe).exists()
    assert not aufgabe.can_access(empfaenger)


# --- Gastseite ---------------------------------------------------------------------------------------------------


def test_gastseite_nennt_nur_sichtbare_oder_verwaltbare_freigaben(org: Any, make_member: Any) -> None:
    autorin = make_member(
        org, ["motions.view", "motions.create", "motions.edit", "motions.share"], email="autorin@example.org"
    )
    leser = make_member(org, ["members.view", "motions.view", "motions.view_drafts"], email="leser@example.org")
    gast_konto: Any = UserFactory(email="gast@example.org")  # type: ignore[no-untyped-call]
    gast: Any = MembershipFactory(user=gast_konto, organization=org, is_guest=True)  # type: ignore[no-untyped-call]
    privat = Motion.objects.create(organization=org, author=autorin, title="Privater Entwurf", visibility="private")
    offen = Motion.objects.create(organization=org, author=autorin, title="Für alle", visibility="organization")
    for dokument in (privat, offen):
        MotionShare.objects.create(motion=dokument, scope="user", user=gast.user, level="view", created_by=autorin.user)

    alle = selectors.guest_document_shares(org, gast.user)
    fuer_leser = selectors.guest_document_shares(org, gast.user, viewer=leser)
    fuer_autorin = selectors.guest_document_shares(org, gast.user, viewer=autorin)

    assert {share.motion.title for share in alle} == {"Privater Entwurf", "Für alle"}
    assert {share.motion.title for share in fuer_leser} == {"Für alle"}
    # Wer die Freigabe verwalten darf, sieht sie weiter (zum Entziehen nötig)
    assert {share.motion.title for share in fuer_autorin} == {"Privater Entwurf", "Für alle"}


# --- Einbettungs-Snippet -----------------------------------------------------------------------------------------


def test_snippet_setzt_titel_und_ort_als_text() -> None:
    vorlagen = Path(settings.BASE_DIR) / "templates" / "work" / "organization"
    quelltext = (vorlagen / "partials" / "_termine_snippet.html").read_text(encoding="utf-8")

    assert "fraktions-termine" in quelltext
    assert "innerHTML" not in quelltext
    assert "document.createElement" in quelltext and "textContent" in quelltext
    assert "partials/_termine_snippet.html" in (vorlagen / "api_settings.html").read_text(encoding="utf-8")
