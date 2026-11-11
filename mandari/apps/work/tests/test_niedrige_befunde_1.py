# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Kleine Korrekturen an Zugang und Benachrichtigungen.

- Deaktivierte Mitgliedschaften und Konten bekommen keine Benachrichtigungen mehr.
- Eine offene Registrierungsanfrage gibt nur frei, wer Mitglieder einladen darf.
- Backup-Codes des zweiten Faktors: jeder Code einmal, eine neu erzeugte Liste bleibt.
- Die Offline-Seite der PWA ist ohne erfüllte Pflicht zum zweiten Faktor erreichbar und wird ohne Anmeldung geholt.
- Der Briefkopf eines Exports schreibt keinen Text aus dem hochgeladenen PDF ins Protokoll.
"""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.test import Client
from django.utils import timezone

from apps.accounts.models import TwoFactorDevice
from apps.accounts.services import TwoFactorService
from apps.common import pdf
from apps.work.notifications.models import Notification, NotificationType
from apps.work.notifications.services import NotificationHub
from apps.work.organization import services as org_services

pytestmark = pytest.mark.django_db


# --- Benachrichtigungen ----------------------------------------------------------------------------------------


def _benachrichtigen(empfaenger: Any, absender: Any) -> Any:
    return NotificationHub.send(
        empfaenger,
        NotificationType.MEETING_UPDATED,
        "Sitzung verlegt",
        "Die Sitzung beginnt eine Stunde später.",
        actor=absender,
        send_email=False,
    )


def test_deaktivierte_mitgliedschaft_bekommt_keine_benachrichtigung(org: Any, make_member: Any) -> None:
    absender = make_member(org, ["dashboard.view"], email="absender@example.org")
    aktiv = make_member(org, ["dashboard.view"], email="aktiv@example.org")
    deaktiviert = make_member(org, ["dashboard.view"], email="deaktiviert@example.org")
    deaktiviert.is_active = False
    deaktiviert.save(update_fields=["is_active"])
    gesperrt = make_member(org, ["dashboard.view"], email="gesperrt@example.org")
    gesperrt.user.is_active = False
    gesperrt.user.save(update_fields=["is_active"])

    assert _benachrichtigen(aktiv, absender) is not None
    assert _benachrichtigen(deaktiviert, absender) is None
    assert _benachrichtigen(gesperrt, absender) is None
    assert set(Notification.objects.values_list("recipient_id", flat=True)) == {aktiv.pk}


def test_vertretung_einer_deaktivierten_mitgliedschaft_wird_weiter_benachrichtigt(org: Any, make_member: Any) -> None:
    from datetime import timedelta

    from apps.work.organization.models import MemberAbsence

    absender = make_member(org, ["dashboard.view"], email="absender@example.org")
    abwesend = make_member(org, ["dashboard.view"], email="abwesend@example.org")
    vertretung = make_member(org, ["dashboard.view"], email="vertretung@example.org")
    heute = timezone.localdate()
    MemberAbsence.objects.create(
        organization=org,
        membership=abwesend,
        start_date=heute - timedelta(days=1),
        end_date=heute + timedelta(days=7),
        deputy=vertretung,
    )
    abwesend.is_active = False
    abwesend.save(update_fields=["is_active"])

    assert _benachrichtigen(abwesend, absender) is None

    erhalten = Notification.objects.get()
    assert erhalten.recipient_id == vertretung.pk and erhalten.title.startswith("[Vertretung]")

    # Auch über das Ereignis der Zuweisung: Die deaktivierte Mitgliedschaft geht an den NotificationHub
    from types import SimpleNamespace
    from uuid import uuid4

    from apps.work.notifications import abonnement
    from apps.work.tasks.models import Task

    aufgabe = Task.objects.create(organization=org, title="Rede schreiben", created_by=absender, assigned_to=abwesend)
    Notification.objects.all().delete()
    ereignis = SimpleNamespace(
        event_id=uuid4(),
        actor_ref=f"user:{absender.user_id}",
        payload={"task": str(aufgabe.pk), "organization": str(org.pk), "assignee": str(abwesend.pk)},
    )
    abonnement.aufgabe_zugewiesen(ereignis)  # type: ignore[arg-type]
    assert [n.recipient_id for n in Notification.objects.all()] == [vertretung.pk]

    # Eine deaktivierte Vertretung bekommt weiterhin nichts
    Notification.objects.all().delete()
    vertretung.is_active = False
    vertretung.save(update_fields=["is_active"])
    assert _benachrichtigen(abwesend, absender) is None
    assert not Notification.objects.exists()


def test_mail_entfaellt_wenn_das_mitglied_vor_dem_versand_deaktiviert_wurde(
    org: Any, make_member: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apps.common import mail
    from apps.work import background_tasks

    absender = make_member(org, ["dashboard.view"], email="absender@example.org")
    empfaenger = make_member(org, ["dashboard.view"], email="empfaenger@example.org")
    benachrichtigung = _benachrichtigen(empfaenger, absender)
    assert benachrichtigung is not None
    versandt: list[Any] = []
    monkeypatch.setattr(mail, "send", lambda *args, **kwargs: versandt.append(kwargs))
    empfaenger.is_active = False
    empfaenger.save(update_fields=["is_active"])

    background_tasks.send_notification_email_task.func(str(benachrichtigung.pk))

    assert versandt == []
    benachrichtigung.refresh_from_db()
    assert not benachrichtigung.email_sent


# --- Offene Registrierungsanfrage -------------------------------------------------------------------------------


def test_offene_registrierung_gibt_nur_frei_wer_einladen_darf(org: Any, make_member: Any) -> None:
    anfrage = make_member(org, [], email="neu@example.org")
    anfrage.is_active = False
    anfrage.registration_requested_at = timezone.now()
    anfrage.save(update_fields=["is_active", "registration_requested_at"])
    bearbeitet = make_member(org, ["members.view", "members.edit"], email="bearbeitet@example.org")
    laedt_ein = make_member(org, ["members.view", "members.edit", "members.invite"], email="einladen@example.org")

    with pytest.raises(org_services.ServiceError):
        org_services.reactivate_member(org, anfrage, bearbeitet)
    # Ohne aktive handelnde Mitgliedschaft (inzwischen deaktiviert) ebenfalls nicht
    with pytest.raises(org_services.ServiceError):
        org_services._restore_membership(org, anfrage, None)
    # Auch nicht mit einem Objekt aus dem View, dessen Mitgliedschaft inzwischen deaktiviert ist
    veraltet = make_member(org, ["members.view", "members.edit", "members.invite"], email="veraltet@example.org")
    type(veraltet).objects.filter(pk=veraltet.pk).update(is_active=False)
    assert veraltet.is_active and veraltet.has_permission("members.invite")
    with pytest.raises(org_services.ServiceError):
        org_services.reactivate_member(org, anfrage, veraltet)
    anfrage.refresh_from_db()
    assert not anfrage.is_active and anfrage.registration_requested_at is not None

    org_services.reactivate_member(org, anfrage, laedt_ein)
    anfrage.refresh_from_db()
    assert anfrage.is_active and anfrage.registration_requested_at is None


def test_direktes_freischalten_prueft_die_handelnde_mitgliedschaft_erneut(org: Any, make_member: Any) -> None:
    anfrage = make_member(org, [], email="neu@example.org")
    anfrage.is_active = False
    anfrage.registration_requested_at = timezone.now()
    anfrage.save(update_fields=["is_active", "registration_requested_at"])
    rechte = ["members.view", "members.edit", "members.invite"]
    # Objekt aus dem View, die Mitgliedschaft ist inzwischen deaktiviert
    veraltet = make_member(org, rechte, email="veraltet@example.org")
    type(veraltet).objects.filter(pk=veraltet.pk).update(is_active=False)
    ohne_recht = make_member(org, ["members.view", "members.edit"], email="ohne@example.org")
    # Objekt aus dem View mit schon geladenen Rechten, das Recht ist inzwischen entzogen
    from apps.tenants.models import Permission

    entzogen = make_member(org, rechte, email="entzogen@example.org")
    assert entzogen.has_permission("members.invite")
    for rolle in entzogen.roles.all():
        rolle.permissions.remove(*Permission.objects.filter(codename="members.invite"))
    assert entzogen.has_permission("members.invite"), "das alte Objekt kennt den Entzug nicht"

    for handelnd in (veraltet, ohne_recht, entzogen, None):
        with pytest.raises(org_services.ServiceError):
            org_services.approve_registration(anfrage, actor=handelnd)
    anfrage.refresh_from_db()
    assert not anfrage.is_active and anfrage.registration_requested_at is not None

    org_services.approve_registration(anfrage, actor=make_member(org, rechte, email="einladen@example.org"))
    anfrage.refresh_from_db()
    assert anfrage.is_active and anfrage.registration_requested_at is None


def test_reaktivieren_prueft_zurueckkehrende_rechte_am_jetzigen_stand(org: Any, make_member: Any) -> None:
    """Die handelnde Person verliert ein Recht, das die zu reaktivierende Mitgliedschaft mitbringt."""
    from apps.tenants.models import Permission

    ziel = make_member(org, ["tasks.view", "tasks.manage"], email="ziel@example.org")
    ziel.is_active = False
    ziel.save(update_fields=["is_active"])
    rechte = ["members.view", "members.edit", "tasks.view", "tasks.manage"]
    handelnd = make_member(org, rechte, email="handelnd@example.org")
    assert handelnd.has_permission("tasks.manage")
    for rolle in handelnd.roles.all():
        rolle.permissions.remove(*Permission.objects.filter(codename="tasks.manage"))
    assert handelnd.has_permission("tasks.manage"), "das alte Objekt kennt den Entzug nicht"

    with pytest.raises(org_services.ServiceError):
        org_services.reactivate_member(org, ziel, handelnd)
    ziel.refresh_from_db()
    assert not ziel.is_active

    # Mit den Rechten im jetzigen Stand geht es
    org_services.reactivate_member(org, ziel, make_member(org, rechte, email="darf@example.org"))
    ziel.refresh_from_db()
    assert ziel.is_active


# --- Backup-Codes ------------------------------------------------------------------------------------------------


def _geraet(user: Any, codes: list[str]) -> TwoFactorDevice:
    dienst = TwoFactorService()
    return TwoFactorDevice.objects.create(
        user=user,
        secret_encrypted=dienst._encrypt(dienst.generate_secret()),
        backup_codes_encrypted=dienst._encrypt(json.dumps(codes)),
        is_confirmed=True,
        is_active=True,
    )


def test_backup_code_gilt_einmal_auch_aus_altem_lesestand(org: Any, make_member: Any) -> None:
    user = make_member(org, ["dashboard.view"], email="konto@example.org").user
    _geraet(user, ["abcd-1234", "ef56-7890"])
    dienst = TwoFactorService()
    # Zwei Anmeldungen haben das Gerät gelesen, bevor eine den Code einlöst
    erste = TwoFactorDevice.objects.get(user=user)
    zweite = TwoFactorDevice.objects.get(user=user)

    assert dienst._use_backup_code(erste, "abcd-1234") is True
    assert dienst._use_backup_code(zweite, "ABCD1234") is False
    assert dienst._use_backup_code(zweite, "ef56-7890") is True


def test_neu_erzeugte_codes_ueberstehen_ein_speichern_aus_altem_lesestand(org: Any, make_member: Any) -> None:
    user = make_member(org, ["dashboard.view"], email="konto@example.org").user
    _geraet(user, ["abcd-1234"])
    dienst = TwoFactorService()
    alt = TwoFactorDevice.objects.get(user=user)

    neue = dienst.regenerate_backup_codes(user)

    # Der alte Code ist widerrufen, auch für eine Anmeldung, die das Gerät vorher gelesen hat
    assert dienst._use_backup_code(alt, "abcd-1234") is False
    gespeichert = json.loads(dienst._decrypt(TwoFactorDevice.objects.get(user=user).backup_codes_encrypted))
    assert gespeichert == neue and len(neue) == dienst.BACKUP_CODE_COUNT


# --- Offline-Seite der PWA ----------------------------------------------------------------------------------------


def test_offline_seite_ohne_umleitung_zur_einrichtung(org: Any, make_member: Any, settings: Any) -> None:
    settings.TWO_FACTOR_ENFORCEMENT = True
    settings.TWO_FACTOR_EXEMPT_EMAIL_DOMAINS = []
    org.require_2fa = True
    org.save(update_fields=["require_2fa"])
    mitglied = make_member(org, ["dashboard.view"], email="pflicht@example.org")
    client = Client()
    client.force_login(mitglied.user)

    # Andere Seiten leiten zur Einrichtung um, die Offline-Seite nicht
    assert client.get(f"/work/{org.slug}/").status_code == 302
    assert client.get("/offline/").status_code == 200


def test_service_worker_holt_die_offline_seite_ohne_anmeldung() -> None:
    quelltext = (Path(settings.BASE_DIR) / "templates" / "pwa" / "sw.js").read_text(encoding="utf-8")

    assert "credentials: 'omit'" in quelltext and "redirect: 'error'" in quelltext
    # Eine bei der Installation gescheiterte Offline-Seite holt die nächste gelungene Navigation nach
    assert "event.waitUntil(ensureOfflinePage());" in quelltext
    assert "offlineCache.add(" not in quelltext


# --- Briefkopf ------------------------------------------------------------------------------------------------------


def test_briefkopf_fehler_nennt_nur_die_art(caplog: pytest.LogCaptureFixture) -> None:
    inhalt = pdf.html_to_pdf("<html><body><p>Text</p></body></html>")
    kaputt = io.BytesIO(b"%PDF-1.4\nGEHEIMER-NAME\nzweite Zeile ohne Aufbau")

    with caplog.at_level(logging.WARNING, logger="apps.common.pdf"):
        ergebnis = pdf.overlay_pdf_letterhead(inhalt, kaputt)

    assert ergebnis == inhalt
    meldungen = [eintrag.getMessage() for eintrag in caplog.records]
    assert meldungen and all(meldung.startswith("Briefkopf nicht anwendbar: ") for meldung in meldungen)
    assert all("GEHEIM" not in meldung and "\n" not in meldung for meldung in meldungen)
