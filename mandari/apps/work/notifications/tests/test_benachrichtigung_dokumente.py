# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Abonnement ``benachrichtigung`` für Dokumente (Issue #529): Kommentar, Federführung, Freigabe angefragt bzw.
entschieden und Freigabe für eine Person entstehen als Ereignis in der Anfrage; die Benachrichtigung legt erst das
Abonnement im Worker an.

Die Anfragen laufen über die Ansichten (wie im Betrieb), die Ereignisse über die echte Zustellung
(``deliver_batch``); die Folgenummer vergibt hier ``_nummerieren`` wie im Test der Aufgaben.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from django.core import mail
from django.db.models import Max
from django.urls import reverse
from prometheus_client import REGISTRY

from apps.events import registry
from apps.events.dispatch import deliver_batch, ensure_subscription, rewind
from apps.events.models import Event, ParkedEvent
from apps.events.registry import Subscriber, get
from apps.work import subscribers
from apps.work.motions.models import Motion, MotionApproval, MotionComment, MotionShare
from apps.work.notifications import abonnement
from apps.work.notifications.models import Notification, NotificationType

pytestmark = pytest.mark.django_db

MITGLIED = ["motions.view", "motions.view_drafts", "motions.create", "motions.edit", "motions.comment", "motions.share"]


@pytest.fixture
def leeres_register(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Subscriber]]:
    """Eigenes Register: Was ein Test registriert, bleibt nicht für andere stehen."""
    eintraege: dict[str, Subscriber] = {}
    monkeypatch.setattr(registry, "_REGISTRY", eintraege)
    yield eintraege


@pytest.fixture
def autorin(org: Any, make_member: Any) -> Any:
    return make_member(org, MITGLIED, email="autorin@example.org")


@pytest.fixture
def kollege(org: Any, make_member: Any) -> Any:
    return make_member(org, MITGLIED, email="kollege@example.org")


@pytest.fixture
def dokument(org: Any, autorin: Any) -> Motion:
    return Motion.objects.create(
        organization=org, author=autorin, title="Radweg Hafenstraße", visibility="organization"
    )


def _abonnement(settings: Any, modus: str = "aktiv") -> Subscriber:
    settings.WORK_NOTIFICATION_SUBSCRIPTION = modus
    assert subscribers.register()
    spec = get(abonnement.NAME)
    ensure_subscription(spec)
    return spec


def _nummerieren() -> None:
    hoechste = Event.objects.aggregate(hoechste=Max("seq"))["hoechste"] or 0
    for event in Event.objects.filter(seq__isnull=True).order_by("id"):
        hoechste += 1
        Event.objects.filter(pk=event.pk).update(seq=hoechste)


def _zustellen(spec: Subscriber) -> int:
    _nummerieren()
    return deliver_batch(spec).delivered


def _xhr(client: Any, url: str, daten: dict[str, Any]) -> Any:
    return client.post(url, daten, HTTP_X_REQUESTED_WITH="XMLHttpRequest")


def _url(name: str, org: Any, dokument: Motion, **weitere: Any) -> str:
    return reverse(f"work:{name}", kwargs={"org_slug": org.slug, "motion_id": dokument.id, **weitere})


def _benachrichtigungen(empfaenger: Any, art: str) -> list[Notification]:
    return list(Notification.objects.filter(recipient=empfaenger, notification_type=art))


def _zaehler(typ: str, ergebnis: str) -> float:
    labels = {"type": typ, "result": ergebnis}
    return REGISTRY.get_sample_value("mandari_notification_subscription_total", labels) or 0.0


def _kommentieren(client_for: Any, wer: Any, org: Any, dokument: Motion, text: str = "Bitte Zahlen ergänzen") -> Any:
    antwort = _xhr(client_for(wer.user), _url("document_comment", org, dokument), {"content": text})
    assert antwort.status_code == 200, antwort.content
    return MotionComment.objects.filter(motion=dokument).latest("created_at")


# -- aus: wie bisher -----------------------------------------------------------------------------


def test_ohne_schalter_kein_ereignis_und_benachrichtigung_in_der_anfrage(
    settings: Any, org: Any, autorin: Any, kollege: Any, dokument: Motion, client_for: Any
) -> None:
    settings.WORK_NOTIFICATION_SUBSCRIPTION = "aus"
    _kommentieren(client_for, kollege, org, dokument)

    assert not Event.objects.exists()
    assert len(_benachrichtigungen(autorin, NotificationType.MOTION_COMMENT)) == 1
    assert [m.to for m in mail.outbox] == [["autorin@example.org"]]


# -- aktiv: die Benachrichtigung entsteht im Abonnement --------------------------------------------


def test_kommentar_wird_ereignis_und_erst_das_abonnement_benachrichtigt(
    settings: Any,
    leeres_register: dict[str, Subscriber],
    org: Any,
    autorin: Any,
    kollege: Any,
    dokument: Motion,
    client_for: Any,
) -> None:
    spec = _abonnement(settings)
    kommentar = _kommentieren(client_for, kollege, org, dokument)

    [ereignis] = Event.objects.all()
    assert (ereignis.type, ereignis.aggregate_type, ereignis.aggregate_id) == (
        "work.document.commented",
        "Document",
        dokument.pk,
    )
    assert ereignis.visibility == "intern" and ereignis.tenant_ref == f"org:{org.pk}"
    assert ereignis.payload == {"document": str(dokument.pk), "organization": str(org.pk), "comment": str(kommentar.pk)}
    assert ereignis.actor_ref == f"user:{kollege.user_id}"
    assert not Notification.objects.exists() and not mail.outbox, "nicht mehr in der Anfrage"

    assert _zustellen(spec) == 1
    [benachrichtigung] = _benachrichtigungen(autorin, NotificationType.MOTION_COMMENT)
    assert benachrichtigung.event_key == f"{ereignis.event_id}:{autorin.pk}"
    assert benachrichtigung.actor == kollege and "Bitte Zahlen ergänzen" in benachrichtigung.message
    assert not _benachrichtigungen(kollege, NotificationType.MOTION_COMMENT), "nicht an sich selbst"
    assert [m.to for m in mail.outbox] == [["autorin@example.org"]]


def test_federfuehrung_wird_ereignis(
    settings: Any,
    leeres_register: dict[str, Subscriber],
    org: Any,
    autorin: Any,
    kollege: Any,
    dokument: Motion,
    client_for: Any,
) -> None:
    spec = _abonnement(settings)
    antwort = _xhr(
        client_for(autorin.user),
        _url("document_meta", org, dokument),
        {"action": "set_responsible", "responsible": str(kollege.id)},
    )
    assert antwort.status_code == 200

    [ereignis] = Event.objects.all()
    assert ereignis.type == "work.document.assigned"
    assert ereignis.payload["responsible"] == str(kollege.pk)
    assert not Notification.objects.exists()

    _zustellen(spec)
    [benachrichtigung] = _benachrichtigungen(kollege, NotificationType.MOTION_ASSIGNED)
    assert benachrichtigung.actor == autorin and "Radweg Hafenstraße" in benachrichtigung.message

    # Dieselbe Federführung noch einmal: kein neues Ereignis
    _xhr(
        client_for(autorin.user),
        _url("document_meta", org, dokument),
        {"action": "set_responsible", "responsible": str(kollege.id)},
    )
    assert Event.objects.count() == 1


def test_freigabe_angefragt_und_entschieden(
    settings: Any,
    leeres_register: dict[str, Subscriber],
    org: Any,
    autorin: Any,
    kollege: Any,
    dokument: Motion,
    client_for: Any,
) -> None:
    spec = _abonnement(settings)
    antwort = _xhr(
        client_for(autorin.user),
        _url("document_approval_request", org, dokument),
        {"approver": str(kollege.id), "approval_type": "chair"},
    )
    assert antwort.status_code == 200
    freigabe = MotionApproval.objects.get(motion=dokument)
    [angefragt] = Event.objects.all()
    assert angefragt.type == "work.document.approval_requested"
    assert angefragt.payload["approval"] == str(freigabe.pk)

    _zustellen(spec)
    [anfrage] = _benachrichtigungen(kollege, NotificationType.MOTION_APPROVAL_REQUESTED)
    assert anfrage.metadata == {"motion_id": str(dokument.pk), "approval_id": str(freigabe.pk)}

    antwort = _xhr(
        client_for(kollege.user),
        _url("document_approval_decide", org, dokument, approval_id=freigabe.id),
        {"decision": "approve"},
    )
    assert antwort.status_code == 200
    entschieden = Event.objects.get(type="work.document.approval_decided")
    assert entschieden.payload["approved"] is True
    assert not _benachrichtigungen(autorin, NotificationType.MOTION_APPROVAL_DECIDED)

    _zustellen(spec)
    [ergebnis] = _benachrichtigungen(autorin, NotificationType.MOTION_APPROVAL_DECIDED)
    assert ergebnis.title == "Freigabe erteilt" and ergebnis.actor == kollege
    assert not _benachrichtigungen(kollege, NotificationType.MOTION_APPROVAL_DECIDED), "nicht, wer entschieden hat"


def test_schon_entschiedene_freigabe_wird_nicht_mehr_angefragt(
    settings: Any,
    leeres_register: dict[str, Subscriber],
    org: Any,
    autorin: Any,
    kollege: Any,
    dokument: Motion,
    client_for: Any,
) -> None:
    spec = _abonnement(settings)
    _xhr(
        client_for(autorin.user),
        _url("document_approval_request", org, dokument),
        {"approver": str(kollege.id), "approval_type": "chair"},
    )
    # Vor der Zustellung entschieden: Die Anfrage braucht niemand mehr
    MotionApproval.objects.filter(motion=dokument).update(approved=True)

    _zustellen(spec)
    assert not _benachrichtigungen(kollege, NotificationType.MOTION_APPROVAL_REQUESTED)


def test_freigabe_fuer_eine_person(
    settings: Any,
    leeres_register: dict[str, Subscriber],
    org: Any,
    autorin: Any,
    kollege: Any,
    client_for: Any,
) -> None:
    spec = _abonnement(settings)
    privat = Motion.objects.create(organization=org, author=autorin, title="Haushaltsrede", visibility="private")
    antwort = client_for(autorin.user).post(
        _url("document_share_update", org, privat),
        {"visibility": "shared", "add_user_email": "kollege@example.org", "level": "comment"},
    )
    assert antwort.status_code == 204

    [ereignis] = Event.objects.all()
    assert ereignis.type == "work.document.shared"
    assert ereignis.payload == {
        "document": str(privat.pk),
        "organization": str(org.pk),
        "member": str(kollege.pk),
        "level": "comment",
    }
    assert not Notification.objects.exists()

    _zustellen(spec)
    [benachrichtigung] = _benachrichtigungen(kollege, NotificationType.MOTION_SHARED)
    assert "Haushaltsrede" in benachrichtigung.message and "Kommentieren" in benachrichtigung.message


def test_entzogene_freigabe_ergibt_keine_benachrichtigung(
    settings: Any,
    leeres_register: dict[str, Subscriber],
    org: Any,
    autorin: Any,
    kollege: Any,
    client_for: Any,
) -> None:
    """Bis zur Zustellung wieder entzogen: Die Person erfährt den Titel nicht mehr."""
    spec = _abonnement(settings)
    privat = Motion.objects.create(organization=org, author=autorin, title="Haushaltsrede", visibility="private")
    client_for(autorin.user).post(
        _url("document_share_update", org, privat),
        {"visibility": "shared", "add_user_email": "kollege@example.org", "level": "view"},
    )
    MotionShare.objects.filter(motion=privat).delete()
    Motion.objects.filter(pk=privat.pk).update(visibility="private")

    _zustellen(spec)
    assert not Notification.objects.exists() and not mail.outbox


def test_geloeschtes_dokument_ergibt_nichts(
    settings: Any,
    leeres_register: dict[str, Subscriber],
    org: Any,
    autorin: Any,
    kollege: Any,
    dokument: Motion,
    client_for: Any,
) -> None:
    spec = _abonnement(settings)
    _kommentieren(client_for, kollege, org, dokument)
    dokument.delete()

    assert _zustellen(spec) == 1
    assert not Notification.objects.exists() and not mail.outbox


def test_dokument_im_papierkorb_ergibt_nichts(
    settings: Any,
    leeres_register: dict[str, Subscriber],
    org: Any,
    autorin: Any,
    kollege: Any,
    dokument: Motion,
    client_for: Any,
) -> None:
    spec = _abonnement(settings)
    _xhr(
        client_for(autorin.user),
        _url("document_meta", org, dokument),
        {"action": "set_responsible", "responsible": str(kollege.id)},
    )
    Motion.objects.filter(pk=dokument.pk).update(status="deleted")

    assert _zustellen(spec) == 1
    assert not Notification.objects.exists() and not mail.outbox


def test_ohne_zugang_keine_federfuehrung_und_keine_freigabeanfrage(
    settings: Any,
    leeres_register: dict[str, Subscriber],
    org: Any,
    autorin: Any,
    kollege: Any,
    client_for: Any,
) -> None:
    """Bis zur Zustellung hat die Person den Zugang zum privaten Dokument wieder verloren: kein Titel an sie."""
    spec = _abonnement(settings)
    privat = Motion.objects.create(organization=org, author=autorin, title="Haushaltsrede", visibility="private")
    client = client_for(autorin.user)
    antwort = _xhr(
        client, _url("document_meta", org, privat), {"action": "set_responsible", "responsible": str(kollege.id)}
    )
    assert antwort.status_code == 200
    antwort = _xhr(
        client, _url("document_approval_request", org, privat), {"approver": str(kollege.id), "approval_type": "chair"}
    )
    assert antwort.status_code == 200
    assert sorted(Event.objects.values_list("type", flat=True)) == [
        "work.document.approval_requested",
        "work.document.assigned",
    ]

    # Federführung zurückgenommen, Freigabe für die Anfrage entzogen, Dokument wieder privat
    Motion.objects.filter(pk=privat.pk).update(responsible=None, visibility="private")
    MotionShare.objects.filter(motion=privat).delete()

    assert _zustellen(spec) == 2
    assert not _benachrichtigungen(kollege, NotificationType.MOTION_ASSIGNED)
    assert not _benachrichtigungen(kollege, NotificationType.MOTION_APPROVAL_REQUESTED)
    assert not mail.outbox


def test_doppelte_zustellung_benachrichtigt_einmal(
    settings: Any,
    leeres_register: dict[str, Subscriber],
    org: Any,
    autorin: Any,
    kollege: Any,
    dokument: Motion,
    client_for: Any,
) -> None:
    """Zustellung mindestens einmal: Nachspielen legt weder Benachrichtigung noch Mail doppelt an."""
    spec = _abonnement(settings)
    _kommentieren(client_for, kollege, org, dokument)
    _xhr(
        client_for(autorin.user),
        _url("document_meta", org, dokument),
        {"action": "set_responsible", "responsible": str(kollege.id)},
    )
    assert _zustellen(spec) == 2
    vorher = sorted(Notification.objects.values_list("recipient_id", "notification_type", "event_key"))
    mails = len(mail.outbox)

    erstes = Event.objects.order_by("seq").first()
    assert erstes is not None and erstes.seq is not None
    rewind(abonnement.NAME, erstes.seq)
    assert deliver_batch(spec).delivered == 2

    assert sorted(Notification.objects.values_list("recipient_id", "notification_type", "event_key")) == vorher
    assert len(mail.outbox) == mails
    assert not ParkedEvent.objects.exists()


def test_kommentar_ohne_zuordenbare_autorin_benachrichtigt_trotzdem(
    settings: Any,
    leeres_register: dict[str, Subscriber],
    org: Any,
    autorin: Any,
    kollege: Any,
    dokument: Motion,
    client_for: Any,
) -> None:
    """Die kommentierende Mitgliedschaft ist bis zur Zustellung gelöscht: Die Autorin erfährt es trotzdem."""
    spec = _abonnement(settings)
    kommentar = _kommentieren(client_for, kollege, org, dokument)
    MotionComment.objects.filter(pk=kommentar.pk).update(author=None)

    _zustellen(spec)
    [benachrichtigung] = _benachrichtigungen(autorin, NotificationType.MOTION_COMMENT)
    assert benachrichtigung.actor is None


# -- schatten: vergleichen, nichts anlegen ------------------------------------------------------


def test_schatten_vergleicht_mit_dem_bisherigen_weg(
    settings: Any,
    leeres_register: dict[str, Subscriber],
    org: Any,
    autorin: Any,
    kollege: Any,
    dokument: Motion,
    client_for: Any,
) -> None:
    spec = _abonnement(settings, "schatten")
    gleich = _zaehler("work.document.commented", "gleich")
    _kommentieren(client_for, kollege, org, dokument)

    assert Event.objects.filter(type="work.document.commented").count() == 1
    assert len(_benachrichtigungen(autorin, NotificationType.MOTION_COMMENT)) == 1, "bisheriger Weg"
    assert len(mail.outbox) == 1

    assert _zustellen(spec) == 1
    assert len(_benachrichtigungen(autorin, NotificationType.MOTION_COMMENT)) == 1, "Schatten legt nichts an"
    assert len(mail.outbox) == 1
    assert _zaehler("work.document.commented", "gleich") == gleich + 1


def test_umschalten_ohne_doppelte_benachrichtigung(
    settings: Any,
    leeres_register: dict[str, Subscriber],
    org: Any,
    autorin: Any,
    kollege: Any,
    dokument: Motion,
    client_for: Any,
) -> None:
    """Abonnement schon aktiv, Anwendung noch im Schatten: der bisherige Weg hat benachrichtigt, das Abonnement nicht."""
    spec = _abonnement(settings, "aktiv")
    settings.WORK_NOTIFICATION_SUBSCRIPTION = "schatten"
    antwort = _xhr(
        client_for(kollege.user),
        _url("document_approval_request", org, dokument),
        {"approver": str(autorin.id), "approval_type": "chair"},
    )
    assert antwort.status_code == 200
    assert len(_benachrichtigungen(autorin, NotificationType.MOTION_APPROVAL_REQUESTED)) == 1

    assert _zustellen(spec) == 1
    assert len(_benachrichtigungen(autorin, NotificationType.MOTION_APPROVAL_REQUESTED)) == 1
    assert len(mail.outbox) == 1


# -- Zugang am Stand des Versands -----------------------------------------------------------------


def _abwesend_mit_vertretung(org: Any, abwesend: Any, vertretung: Any) -> None:
    from datetime import timedelta

    from django.utils import timezone

    from apps.work.organization.models import MemberAbsence

    heute = timezone.now().date()
    MemberAbsence.objects.create(
        organization=org,
        membership=abwesend,
        start_date=heute - timedelta(days=1),
        end_date=heute + timedelta(days=1),
        deputy=vertretung,
        notify_deputy=True,
    )


def test_vertretung_nur_mit_zugang_zum_dokument(org: Any, make_member: Any, autorin: Any, kollege: Any) -> None:
    """Weiterleitung an die Vertretung nur, wenn sie das Dokument selbst öffnen darf (nicht nur bei Unterlagen
    aus „Nichtöffentliche Vorgänge“)."""
    from apps.work.notifications.services import NotificationHub

    vertretung = make_member(org, MITGLIED, email="vertretung@example.org")
    _abwesend_mit_vertretung(org, kollege, vertretung)
    geteilt = Motion.objects.create(
        organization=org, author=autorin, title="Haushaltsrede", visibility="shared", responsible=kollege
    )

    NotificationHub.notify_motion_assigned(geteilt, kollege, autorin)
    assert _benachrichtigungen(kollege, NotificationType.MOTION_ASSIGNED)
    assert not _benachrichtigungen(vertretung, NotificationType.MOTION_ASSIGNED)

    MotionShare.objects.create(
        motion=geteilt, scope="user", user=vertretung.user, level="view", created_by=autorin.user
    )
    NotificationHub.notify_motion_assigned(geteilt, kollege, autorin)
    [weitergeleitet] = _benachrichtigungen(vertretung, NotificationType.MOTION_ASSIGNED)
    assert weitergeleitet.title.startswith("[Vertretung]")


def test_wartende_mail_nach_entzug_der_freigabe_wird_verworfen(
    settings: Any, org: Any, autorin: Any, kollege: Any
) -> None:
    """Die Mail liegt schon im Postausgang, dann wird die Freigabe entzogen: Der Versand prüft den Zugang erneut."""
    from apps.common.mail.outbox import VERWORFEN_VOR_VERSAND
    from apps.common.models import MailOutbox
    from apps.events.task_runner import run_pending
    from apps.work.notifications.services import NotificationHub

    settings.TASKS = {"default": {**settings.TASKS["default"], "BACKEND": "apps.events.tasks_backend.JournalBackend"}}
    settings.MAIL_QUEUE = ["work.benachrichtigung"]
    geteilt = Motion.objects.create(organization=org, author=autorin, title="Haushaltsrede", visibility="shared")
    freigabe = MotionShare.objects.create(
        motion=geteilt, scope="user", user=kollege.user, level="view", created_by=autorin.user
    )
    assert NotificationHub.notify_document_shared(geteilt, kollege, "view", autorin) is not None
    assert run_pending(limit=1) == 1, "Auftrag zur Benachrichtigung legt die Mail in den Postausgang"
    assert MailOutbox.objects.get().status == MailOutbox.Status.WARTEND

    freigabe.delete()
    assert run_pending() == 1
    assert not mail.outbox
    zeile = MailOutbox.objects.get()
    assert (zeile.status, zeile.error_code) == (MailOutbox.Status.FEHLGESCHLAGEN, VERWORFEN_VOR_VERSAND)
