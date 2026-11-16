# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Benachrichtigungen prüfen, wer sie heute sehen darf.

Titel und Auszüge stehen im gespeicherten Text einer Benachrichtigung und gehen per E-Mail hinaus. Geprüft wird
deshalb an drei Stellen gegen den aktuellen Stand:

- Weiterleitung an die Vertretung bei Abwesenheit: nur an aktive Mitglieder ohne Gastzugang, und zu einer Unterlage
  aus „Nichtöffentliche Vorgänge“ oder einer Aufgabe aus einem nichtöffentlichen TOP nur, wenn die Vertretung den
  Bezug selbst öffnen darf.
- Neue Kommentare und entschiedene Freigaben: nur an Personen, die das Dokument noch öffnen dürfen.
- Glocke, Benachrichtigungsseite und Einblendung: Erwähnungen zu einem TOP, der für die Person nichtöffentlich
  ist, und Nachrichten zu endgültig gelöschten Unterlagen erscheinen für Nicht-Vereidigte nicht.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

import pytest
from django.core import mail
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from apps.common.tests.factories import MembershipFactory, UserFactory
from apps.work.faction.models import FactionAgendaItem, FactionMeeting
from apps.work.motions.models import DocumentFolder, Motion, MotionApproval, MotionComment, MotionShare
from apps.work.notifications.models import Notification, NotificationType
from apps.work.notifications.services import NotificationHub
from apps.work.organization import selectors
from apps.work.organization.models import MemberAbsence
from apps.work.tasks.models import Task

pytestmark = pytest.mark.django_db

GEHEIM = "Grundstück Musterweg Kaufpreis"
NOE_TOP = "Personalangelegenheit Dezernat"
RECHTE = [
    "dashboard.view",
    "motions.view",
    "motions.view_drafts",
    "motions.comment",
    "motions.edit",
    "faction.view_public",
    "faction.view_non_public",
    "tasks.view",
    "tasks.create",
]

hub = cast(Any, NotificationHub)


@pytest.fixture(autouse=True)
def _leerer_cache() -> None:
    cache.clear()


def _mitglied(org: Any, make_member: Any, name: str, *, vereidigt: bool, rechte: list[str] | None = None) -> Any:
    member = make_member(org, RECHTE if rechte is None else rechte, email=f"{name}@example.org")
    if vereidigt:
        member.is_sworn_in = True
        member.save(update_fields=["is_sworn_in"])
    return member


@pytest.fixture
def autorin(org: Any, make_member: Any) -> Any:
    return _mitglied(org, make_member, "autorin", vereidigt=True)


@pytest.fixture
def abwesend(org: Any, make_member: Any) -> Any:
    return _mitglied(org, make_member, "abwesend", vereidigt=True)


@pytest.fixture
def unterlage(org: Any, autorin: Any) -> Motion:
    """Unterlage im Ordner „Nichtöffentliche Vorgänge“: nur für vereidigte Mitglieder."""
    ordner = cast(Any, DocumentFolder).non_public_folder(org, created_by=autorin)
    return Motion.objects.create(
        organization=org, author=autorin, title=GEHEIM, status="archived", visibility="private", folder=ordner
    )


def _abwesenheit(org: Any, abwesend: Any, vertretung: Any) -> MemberAbsence:
    heute = timezone.now().date()
    return MemberAbsence.objects.create(
        organization=org,
        membership=abwesend,
        start_date=heute - timedelta(days=1),
        end_date=heute + timedelta(days=1),
        deputy=vertretung,
        notify_deputy=True,
    )


def _texte(empfaenger: Any) -> str:
    """Alles, was die Person als Benachrichtigung oder E-Mail erhalten hat."""
    gespeichert = [f"{n.title} {n.message}" for n in Notification.objects.filter(recipient=empfaenger)]
    mails = [f"{m.subject} {m.body}" for m in mail.outbox if empfaenger.user.email in m.to]
    return " ".join(gespeichert + mails)


def _kommentar(unterlage: Motion, von: Any, text: str = "Erste Einschätzung zum Kaufpreis") -> MotionComment:
    return MotionComment.objects.create(motion=unterlage, author=von, content=text)


# --- Weiterleitung an die Vertretung ----------------------------------------------------------------------------


def test_nichtoeffentliche_unterlage_nicht_an_nicht_vereidigte_vertretung(
    org: Any, make_member: Any, unterlage: Motion, autorin: Any, abwesend: Any
) -> None:
    vertretung = _mitglied(org, make_member, "vertretung", vereidigt=False)
    _abwesenheit(org, abwesend, vertretung)
    _kommentar(unterlage, abwesend)

    hub.notify_motion_comment(_kommentar(unterlage, autorin, "Antwort mit Zahlen"), autorin)
    hub.notify_motion_due_soon(unterlage, abwesend, 2)

    assert GEHEIM in _texte(abwesend), "Die abwesende, vereidigte Person erhält die Nachricht weiter"
    assert _texte(vertretung) == ""


def test_nichtoeffentliche_unterlage_weiter_an_vereidigte_vertretung(
    org: Any, make_member: Any, unterlage: Motion, autorin: Any, abwesend: Any
) -> None:
    vertretung = _mitglied(org, make_member, "vertretung", vereidigt=True)
    _abwesenheit(org, abwesend, vertretung)
    _kommentar(unterlage, abwesend)

    hub.notify_motion_comment(_kommentar(unterlage, autorin, "Antwort mit Zahlen"), autorin)

    [weitergeleitet] = Notification.objects.filter(recipient=vertretung)
    assert weitergeleitet.title.startswith("[Vertretung]") and GEHEIM in weitergeleitet.message


def test_keine_weiterleitung_nach_entzug_der_vereidigung_der_vertretung(
    org: Any, make_member: Any, unterlage: Motion, autorin: Any, abwesend: Any
) -> None:
    vertretung = _mitglied(org, make_member, "vertretung", vereidigt=True)
    _abwesenheit(org, abwesend, vertretung)
    _kommentar(unterlage, abwesend)
    vertretung.is_sworn_in = False
    vertretung.save(update_fields=["is_sworn_in"])

    hub.notify_motion_comment(_kommentar(unterlage, autorin, "Antwort mit Zahlen"), autorin)

    assert _texte(vertretung) == ""


def test_keine_weiterleitung_an_gastzugang_oder_deaktivierte_vertretung(
    org: Any, make_member: Any, autorin: Any, abwesend: Any
) -> None:
    gast = cast(Any, MembershipFactory)(
        user=cast(Any, UserFactory)(email="gast@example.org"), organization=org, is_guest=True
    )
    absenz = _abwesenheit(org, abwesend, gast)
    aufgabe = Task.objects.create(organization=org, title="Plakate kleben", created_by=autorin, assigned_to=abwesend)

    hub.notify_task_assigned(aufgabe, abwesend, autorin)
    assert _texte(gast) == "", "Ein Gastzugang ist keine Vertretung"

    ehemalig = _mitglied(org, make_member, "ehemalig", vereidigt=True)
    MemberAbsence.objects.filter(pk=absenz.pk).update(deputy=ehemalig)
    ehemalig.is_active = False
    ehemalig.save(update_fields=["is_active"])
    hub.notify_task_assigned(aufgabe, abwesend, autorin)
    assert _texte(ehemalig) == ""

    # Die Weiterleitung selbst bleibt: gewöhnliche Aufgabe an ein aktives Mitglied
    aktiv = _mitglied(org, make_member, "aktiv", vereidigt=False)
    MemberAbsence.objects.filter(pk=absenz.pk).update(deputy=aktiv)
    hub.notify_task_assigned(aufgabe, abwesend, autorin)
    assert "Plakate kleben" in _texte(aktiv)


def test_gastzugang_steht_nicht_zur_wahl_als_vertretung(org: Any, make_member: Any, abwesend: Any) -> None:
    """Sonst ließe sich eine Vertretung eintragen, an die nichts weitergeleitet wird."""
    gast = cast(Any, MembershipFactory)(
        user=cast(Any, UserFactory)(email="gast-wahl@example.org"), organization=org, is_guest=True
    )
    kollegin = _mitglied(org, make_member, "kollegin", vereidigt=False)

    zur_wahl = list(selectors.available_deputies(org, abwesend))
    assert kollegin in zur_wahl and gast not in zur_wahl
    assert selectors.find_active_member(org, gast.id) is None
    assert selectors.find_active_member(org, kollegin.id) == kollegin


@pytest.fixture
def fraktionssitzung(org: Any, autorin: Any) -> FactionMeeting:
    return FactionMeeting.objects.create(
        organization=org,
        title="Fraktionssitzung im November",
        start=timezone.now() + timedelta(days=3),
        status="planned",
        created_by=autorin,
    )


def test_aufgabe_aus_nichtoeffentlichem_top_nicht_an_nicht_vereidigte_vertretung(
    org: Any, make_member: Any, fraktionssitzung: FactionMeeting, unterlage: Motion, autorin: Any, abwesend: Any
) -> None:
    vertretung = _mitglied(org, make_member, "vertretung", vereidigt=False)
    _abwesenheit(org, abwesend, vertretung)
    top = FactionAgendaItem.objects.create(
        meeting=fraktionssitzung, number="N1", title=NOE_TOP, visibility="internal", order=1
    )
    unterpunkt = FactionAgendaItem.objects.create(
        meeting=fraktionssitzung, number="N1.1", title="Unterpunkt", parent=top, order=2
    )
    aus_top = Task.objects.create(
        organization=org,
        title=f"Nachfassen: {NOE_TOP}",
        created_by=autorin,
        assigned_to=abwesend,
        related_faction_agenda_item=unterpunkt,
    )
    zur_unterlage = Task.objects.create(
        organization=org,
        title=f"Prüfen: {GEHEIM}",
        created_by=autorin,
        assigned_to=abwesend,
        related_motion=unterlage,
    )

    hub.notify_task_assigned(aus_top, abwesend, autorin)
    hub.notify_task_assigned(zur_unterlage, abwesend, autorin)

    assert NOE_TOP in _texte(abwesend) and GEHEIM in _texte(abwesend)
    assert _texte(vertretung) == ""

    vertretung.is_sworn_in = True
    vertretung.save(update_fields=["is_sworn_in"])
    hub.notify_task_due_soon(aus_top, abwesend, 1)
    assert NOE_TOP in _texte(vertretung), "Eine vereidigte Vertretung erhält die Nachricht weiter"


# --- Kommentare und Freigaben nach Entzug des Zugriffs ----------------------------------------------------------


def test_kommentar_nach_entzug_des_zugriffs_ohne_nachricht(org: Any, make_member: Any, autorin: Any) -> None:
    dokument = Motion.objects.create(
        organization=org, author=autorin, title="Antrag Radwegenetz", status="review", visibility="shared"
    )
    mit_freigabe = _mitglied(org, make_member, "freigabe", vereidigt=False)
    bleibt = _mitglied(org, make_member, "bleibt", vereidigt=False)
    gast = cast(Any, MembershipFactory)(
        user=cast(Any, UserFactory)(email="gast@example.org"), organization=org, is_guest=True
    )
    for person in (mit_freigabe, bleibt, gast):
        MotionShare.objects.create(
            motion=dokument, scope="user", user=person.user, level="comment", created_by=autorin.user
        )
        _kommentar(dokument, person, "Anmerkung")
    assert dokument.can_access(gast) and dokument.can_access(mit_freigabe)

    # Freigabe entzogen: Die früheren Kommentare bleiben, der Zugriff nicht
    MotionShare.objects.filter(motion=dokument, user__in=[mit_freigabe.user, gast.user]).delete()
    hub.notify_motion_comment(_kommentar(dokument, autorin, "Neue Zahlen zur Finanzierung"), autorin)

    assert _texte(mit_freigabe) == "" and _texte(gast) == ""
    assert "Antrag Radwegenetz" in _texte(bleibt), "Wer das Dokument weiter öffnen darf, wird benachrichtigt"

    # Dokument privat gestellt: auch die verbliebene Freigabe trägt nicht mehr
    mail.outbox.clear()
    Notification.objects.all().delete()
    Motion.objects.filter(pk=dokument.pk).update(visibility="private")
    dokument.refresh_from_db()
    hub.notify_motion_comment(_kommentar(dokument, autorin, "Weitere Zahlen"), autorin)

    assert _texte(bleibt) == ""


def test_entschiedene_freigabe_ohne_zugriff_ohne_nachricht(org: Any, make_member: Any, autorin: Any) -> None:
    ohne_leserecht = _mitglied(org, make_member, "ohne-leserecht", vereidigt=False, rechte=["dashboard.view"])
    dokument = Motion.objects.create(
        organization=org, author=ohne_leserecht, title="Antrag Stadtbibliothek", status="review"
    )
    freigabe = MotionApproval.objects.create(motion=dokument, approver=autorin, approval_type="chair", approved=True)

    assert hub.notify_motion_approval_decided(freigabe, autorin, ohne_leserecht) is None
    assert _texte(ohne_leserecht) == ""

    mit_leserecht = _mitglied(org, make_member, "mit-leserecht", vereidigt=False)
    Motion.objects.filter(pk=dokument.pk).update(author=mit_leserecht)
    freigabe.motion.refresh_from_db()
    assert hub.notify_motion_approval_decided(freigabe, autorin, mit_leserecht) is not None


# --- Glocke, Benachrichtigungsseite, Einblendung ----------------------------------------------------------------


def _erwaehnung(top: FactionAgendaItem, empfaenger: Any, von: Any) -> Notification:
    """Benachrichtigung, wie sie die Erwähnung im Protokoll anlegt (``faction.erwaehnungen.benachrichtigen``)."""
    sitzung = top.meeting
    angelegt = hub.send(
        recipient=empfaenger,
        notification_type=NotificationType.FACTION_MENTION,
        title="Erwähnung im Protokoll",
        message=f"Jemand hat Sie im Protokoll zu TOP {top.number} „{top.title}“ ({sitzung.title}) erwähnt.",
        link=f"/work/{sitzung.organization.slug}/faction/{sitzung.pk}/?top={top.pk}",
        actor=von,
        metadata={"meeting_id": str(sitzung.pk), "item_id": str(top.pk)},
        send_email=False,
        event_key=f"fs-erwaehnung:{top.pk}",
    )
    assert angelegt is not None
    return cast(Notification, angelegt)


def _ausgaben(client_for: Any, org: Any, member: Any) -> str:
    """Benachrichtigungsseite, Liste in der Glocke und Einblendung der Person, als ein Text."""
    cache.clear()
    client = client_for(member.user)
    kwargs = {"org_slug": org.slug}
    seite = client.get(reverse("work:notifications", kwargs=kwargs))
    glocke = client.get(reverse("work:notifications_partial", kwargs=kwargs))
    einblendung = client.get(reverse("work:notification_latest", kwargs=kwargs))
    assert seite.status_code == glocke.status_code == einblendung.status_code == 200
    return " ".join(
        [seite.content.decode(), cast(str, glocke.json()["html"]), str(einblendung.json()["notifications"])]
    )


def test_erwaehnung_zu_nichtoeffentlichem_top_nach_entzug_der_vereidigung_verborgen(
    org: Any, make_member: Any, client_for: Any, fraktionssitzung: FactionMeeting, autorin: Any
) -> None:
    person = _mitglied(org, make_member, "erwaehnt", vereidigt=True)
    top = FactionAgendaItem.objects.create(
        meeting=fraktionssitzung, number="N1", title=NOE_TOP, visibility="internal", order=1
    )
    erwaehnung = _erwaehnung(top, person, autorin)
    aufgabe = Task.objects.create(organization=org, title="Plakate kleben", created_by=autorin, assigned_to=person)
    hub.notify_task_assigned(aufgabe, person, autorin)

    assert NOE_TOP in _ausgaben(client_for, org, person), "Vereidigte sehen die Erwähnung"

    person.is_sworn_in = False
    person.save(update_fields=["is_sworn_in"])

    ausgaben = _ausgaben(client_for, org, person)
    assert NOE_TOP not in ausgaben
    assert "Plakate kleben" in ausgaben, "Andere Nachrichten bleiben"
    assert erwaehnung.pk not in set(hub.for_recipient(person).values_list("pk", flat=True))
    assert hub.get_unread_count(person) == 1
    assert Notification.objects.filter(pk=erwaehnung.pk).exists(), "Gespeichert bleibt die Nachricht"


def test_erwaehnung_verborgen_wenn_top_nachtraeglich_nichtoeffentlich_wird(
    org: Any, make_member: Any, client_for: Any, fraktionssitzung: FactionMeeting, autorin: Any
) -> None:
    person = _mitglied(org, make_member, "erwaehnt", vereidigt=False)
    top = FactionAgendaItem.objects.create(meeting=fraktionssitzung, number="3", title=NOE_TOP, order=1)
    unterpunkt = FactionAgendaItem.objects.create(
        meeting=fraktionssitzung, number="3.1", title="Stellenplan im Einzelnen", parent=top, order=2
    )
    _erwaehnung(top, person, autorin)
    _erwaehnung(unterpunkt, person, autorin)
    assert NOE_TOP in _ausgaben(client_for, org, person), "Zu einem öffentlichen TOP erscheint die Erwähnung"

    FactionAgendaItem.objects.filter(pk=top.pk).update(visibility="internal")

    ausgaben = _ausgaben(client_for, org, person)
    assert NOE_TOP not in ausgaben
    assert "Stellenplan im Einzelnen" not in ausgaben, "Unterpunkte erben den nichtöffentlichen Teil"

    # Gibt es den TOP nicht mehr, lässt sich nichts mehr prüfen
    FactionAgendaItem.objects.filter(pk=top.pk).update(visibility="public")
    assert NOE_TOP in _ausgaben(client_for, org, person)
    FactionAgendaItem.objects.filter(pk=unterpunkt.pk).delete()
    FactionAgendaItem.objects.filter(pk=top.pk).delete()
    assert NOE_TOP not in _ausgaben(client_for, org, person)


def test_nachrichten_ohne_dokumentbezug_bleiben_neben_nichtoeffentlichen_unterlagen(
    org: Any, make_member: Any, client_for: Any, unterlage: Motion, autorin: Any
) -> None:
    """Gibt es Unterlagen in „Nichtöffentliche Vorgänge“, verschwinden für Nicht-Vereidigte nur Nachrichten dazu."""
    person = _mitglied(org, make_member, "person", vereidigt=False)
    aufgabe = Task.objects.create(organization=org, title="Plakate kleben", created_by=autorin, assigned_to=person)
    hub.notify_task_assigned(aufgabe, person, autorin)

    assert "Plakate kleben" in _ausgaben(client_for, org, person)
    assert hub.get_unread_count(person) == 1


def test_nachricht_zu_geloeschter_nichtoeffentlicher_unterlage_bleibt_verborgen(
    org: Any, make_member: Any, client_for: Any, unterlage: Motion, autorin: Any
) -> None:
    person = _mitglied(org, make_member, "person", vereidigt=True)
    weiter_vereidigt = _mitglied(org, make_member, "weiter", vereidigt=True)
    for empfaenger in (person, weiter_vereidigt):
        _kommentar(unterlage, empfaenger)
    hub.notify_motion_comment(_kommentar(unterlage, autorin, "Antwort mit Zahlen"), autorin)
    assert GEHEIM in _ausgaben(client_for, org, person)

    person.is_sworn_in = False
    person.save(update_fields=["is_sworn_in"])
    assert GEHEIM not in _ausgaben(client_for, org, person)

    # Endgültig gelöscht: Die Nachricht verweist ins Leere und darf nicht wieder erscheinen
    Motion.objects.filter(pk=unterlage.pk).delete()

    assert GEHEIM not in _ausgaben(client_for, org, person)
    assert hub.get_unread_count(person) == 0
    assert GEHEIM in _ausgaben(client_for, org, weiter_vereidigt), "Für Vereidigte ändert sich nichts"
