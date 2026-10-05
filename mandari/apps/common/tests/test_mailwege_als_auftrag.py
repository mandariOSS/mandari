# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Jeder Mailweg funktioniert auch als Auftrag (Issue #528).

Mit ``MAIL_QUEUE=*`` und ``JournalBackend`` verlässt keine dieser Mails die Anfrage: Sie liegt im
Postausgang, bis der Runner sie versendet – mit unverändertem Inhalt, Empfänger und Anhang. Ohne
Schalter laufen dieselben Wege wie bisher sofort (deren eigene Tests in den Modulen).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import timedelta
from io import StringIO
from typing import Any
from unittest import mock

import pytest
from django.core import mail as django_mail
from django.core.cache import cache
from django.core.management import call_command
from django.tasks import task_backends
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts import security_notifications
from apps.accounts.models import User
from apps.common.mail_backends import build_backend
from apps.common.models import MailOutbox, ProblemReport, SiteSettings
from apps.events.models import Task as TaskRow
from apps.events.task_runner import run_pending
from apps.events.tasks_backend import JournalBackend

pytestmark = pytest.mark.django_db

PASSWORT = "Sicheres-Passwort-2026!"


@pytest.fixture(autouse=True)
def journal(settings: Any) -> Iterator[JournalBackend]:
    settings.TASKS = {"default": {**settings.TASKS["default"], "BACKEND": "apps.events.tasks_backend.JournalBackend"}}
    settings.MAIL_QUEUE = ["*"]
    cache.delete(SiteSettings.CACHE_KEY)
    backend = task_backends["default"]
    assert isinstance(backend, JournalBackend)
    yield backend
    cache.delete(SiteSettings.CACHE_KEY)


def _an(adresse: str) -> list[Any]:
    return [nachricht for nachricht in django_mail.outbox if adresse in nachricht.to]


def _erst_im_runner(adresse: str, kind: str) -> Any:
    """Die Mail liegt im Postausgang, nicht im Versand; erst der Runner versendet sie."""
    assert not _an(adresse), "Mail ging noch in der Anfrage hinaus"
    assert MailOutbox.objects.filter(kind=kind, status=MailOutbox.Status.WARTEND).exists(), kind
    run_pending()
    [nachricht] = _an(adresse)
    assert MailOutbox.objects.filter(kind=kind, status=MailOutbox.Status.VERSENDET).exists()
    return nachricht


def test_passwort_zuruecksetzen(client: Client) -> None:
    User.objects.create_user(email="person@example.org", password=PASSWORT)  # type: ignore[no-untyped-call]
    antwort = client.post(reverse("accounts:password_reset"), {"email": "person@example.org"})
    assert antwort.status_code in (200, 302)

    nachricht = _erst_im_runner("person@example.org", "konto.passwort")
    assert "/accounts/" in nachricht.body and nachricht.alternatives


def test_sicherheitshinweis_zweiter_faktor(django_capture_on_commit_callbacks: Callable[..., Any]) -> None:
    konto = User.objects.create_user(email="konto@example.org", password=PASSWORT)  # type: ignore[no-untyped-call]
    with django_capture_on_commit_callbacks(execute=True):
        security_notifications.notify(konto, security_notifications.TWO_FACTOR_ENABLED, "2FA aktiviert", "Text")

    nachricht = _erst_im_runner("konto@example.org", "konto.sicherheit")
    assert nachricht.subject == "Sicherheitshinweis: 2FA aktiviert"


def test_registrierung_mit_bestaetigung(client: Client, org: Any) -> None:
    org.registration_enabled = True
    org.save()
    client.post(
        reverse("accounts:self_register", kwargs={"org_slug": org.slug}),
        {
            "email": "neu@example.org",
            "first_name": "Eva",
            "last_name": "M",
            "password1": PASSWORT,
            "password2": PASSWORT,
        },
    )
    nachricht = _erst_im_runner("neu@example.org", "work.zugang.registration_confirm")
    assert "Bitte bestätige deine Registrierung" in nachricht.subject


def test_einladung_der_organisation_ueber_ihr_smtp(org: Any, make_member: Any) -> None:
    from apps.work.organization import services

    einladend = make_member(org, ["members.invite"], email="einladend@example.org")
    org.mail_sender_mode = "smtp"
    org.smtp_host = "smtp.example.org"
    org.smtp_from_email = "fraktion@example.org"
    org.save()
    services.invite_member(org, einladend.user, "eingeladen@example.org", [], "Hallo")

    with mock.patch(
        "apps.common.mail.config.organization_backend",
        return_value=build_backend("django.core.mail.backends.locmem.EmailBackend"),
    ):
        nachricht = _erst_im_runner("eingeladen@example.org", "work.zugang.invitation")
    assert "fraktion@example.org" in nachricht.from_email


def _fraktionssitzung(org: Any, make_member: Any, status: str) -> tuple[Any, Any]:
    from apps.work.faction.models import FactionAgendaItem, FactionAttendance, FactionMeeting

    vorsitz = make_member(org, ["faction.view_public"], email="mitglied@example.org")
    sitzung = FactionMeeting.objects.create(
        organization=org, title="Fraktionssitzung", start=timezone.now() + timedelta(days=2), created_by=vorsitz
    )
    FactionAgendaItem.objects.create(meeting=sitzung, number="1", title="Haushalt", visibility="public")
    FactionAttendance.objects.create(meeting=sitzung, membership=vorsitz, status=status)
    return sitzung, vorsitz


def test_einladung_zur_fraktionssitzung_mit_anhaengen(org: Any, make_member: Any) -> None:
    from apps.work.faction.services import FactionMeetingEmailService

    sitzung, _ = _fraktionssitzung(org, make_member, "invited")
    assert FactionMeetingEmailService().send_invitations(sitzung) == 1

    zeile = MailOutbox.objects.get(kind="work.fraktion.einladung")
    assert zeile.organization_id == org.pk and zeile.payload_encrypted and not zeile.payload_platform_encrypted
    nachricht = _erst_im_runner("mitglied@example.org", "work.fraktion.einladung")
    namen = {anhang[0] for anhang in nachricht.attachments}
    assert namen == {"tagesordnung.pdf", "sitzung.ics"}
    pdf = next(anhang[1] for anhang in nachricht.attachments if anhang[0] == "tagesordnung.pdf")
    assert bytes(pdf).startswith(b"%PDF")


def test_erinnerung_an_die_fraktionssitzung(org: Any, make_member: Any) -> None:
    from apps.work.faction.services import FactionMeetingEmailService

    sitzung, _ = _fraktionssitzung(org, make_member, "confirmed")
    assert FactionMeetingEmailService().send_reminder(sitzung) == 1
    nachricht = _erst_im_runner("mitglied@example.org", "work.fraktion.erinnerung")
    assert nachricht.subject.startswith("Erinnerung:")


def test_benachrichtigung_ueber_den_weg_der_organisation(org: Any, make_member: Any) -> None:
    from apps.work.notifications.models import NotificationType
    from apps.work.notifications.services import NotificationHub

    empfaengerin = make_member(org, ["dashboard.view"], email="empfaengerin@example.org")
    org.mail_sender_mode = "smtp"
    org.smtp_host = "smtp.example.org"
    org.smtp_from_email = "fraktion@example.org"
    org.save()
    NotificationHub.send(
        recipient=empfaengerin, notification_type=NotificationType.TASK_ASSIGNED, title="Neue Aufgabe", message="m"
    )
    assert not django_mail.outbox and TaskRow.objects.filter(queue="mail").exists()

    with mock.patch(
        "apps.common.mail.config.organization_backend",
        return_value=build_backend("django.core.mail.backends.locmem.EmailBackend"),
    ) as verbindung:
        run_pending()
    verbindung.assert_called_once()
    [nachricht] = _an("empfaengerin@example.org")
    assert "fraktion@example.org" in nachricht.from_email


def test_einladung_in_den_sitzungsdienst() -> None:
    call_command(
        "session_create_tenant",
        "--profile",
        "stadtstaat_bezirk",
        "--name",
        "Bezirksversammlung Musterbezirk",
        "--slug",
        "musterbezirk",
        "--admin-email",
        "sitzungsdienst@example.org",
        stdout=StringIO(),
    )
    nachricht = _erst_im_runner("sitzungsdienst@example.org", "session.zugang")
    assert "Musterbezirk über mandari" in nachricht.from_email


def test_rueckmeldung_zur_fehlermeldung(admin_client: Client) -> None:
    meldung = ProblemReport.objects.create(message="Seite lädt nicht", email="melder@example.org")
    admin_client.post(
        reverse("admin:common_problemreport_changelist"),
        {"action": "mark_resolved_and_notify", "_selected_action": [str(meldung.pk)]},
    )
    nachricht = _erst_im_runner("melder@example.org", "plattform.fehlermeldung")
    assert meldung.reference in nachricht.subject


def test_sicherheitshinweis_geht_nur_einmal_raus(django_capture_on_commit_callbacks: Callable[..., Any]) -> None:
    konto = User.objects.create_user(email="einmal@example.org", password=PASSWORT)  # type: ignore[no-untyped-call]
    with django_capture_on_commit_callbacks(execute=True):
        security_notifications.notify(konto, security_notifications.PASSWORD_CHANGED, "Passwort geändert", "Text")
    hinweis = konto.security_notifications.get()
    security_notifications.send_mail(hinweis.pk)  # etwa ein zweiter Aufruf nach einem Abbruch

    assert MailOutbox.objects.filter(kind="konto.sicherheit").count() == 1
    run_pending()
    assert len(_an("einmal@example.org")) == 1
