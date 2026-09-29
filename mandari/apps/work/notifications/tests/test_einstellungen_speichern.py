# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Benachrichtigungseinstellungen (Issue #423).

Das Speichern schrieb bisher alle Benachrichtigungsarten, auch die, die das Formular nicht zeigte.
Deren Checkboxen fehlten im POST, die Arten waren danach abgeschaltet – etwa die Einladung zur
Fraktionssitzung. Jetzt: Formular und Speichern nutzen dieselbe Liste, die Registrierungsanfrage ist
immer aktiv, und die Migration work/0060 repariert den Bestand.
"""

from __future__ import annotations

import importlib
import re
from datetime import timedelta
from typing import Any

import pytest
from django.apps import apps as django_apps
from django.core import mail
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from apps.work.faction.invitations import dispatch_invitations
from apps.work.faction.models import FactionAttendance, FactionMeeting
from apps.work.notifications.models import (
    ALWAYS_ACTIVE_TYPES,
    CONFIGURABLE_TYPES,
    PREFERENCE_CATEGORIES,
    Notification,
    NotificationPreference,
    NotificationType,
)
from apps.work.organization import services

VORHER = ("work", "0059_mitglied_entfernen_inhalte_erhalten")
NACHHER = ("work", "0060_benachrichtigungsarten_zuruecksetzen")

#: Die neun Arten, die das Formular bis Issue #423 nicht zeigte
NIE_ANGEZEIGT = (
    "motion_assigned",
    "motion_due_soon",
    "motion_approval_req",
    "motion_approval_dec",
    "faction_inv_release",
    "faction_invitation",
    "faction_prop_decided",
    "faction_prot_approved",
    "registration_request",
)


@pytest.fixture
def mitglied(org: Any, make_member: Any) -> Any:
    return make_member(org, ["dashboard.view", "faction.view"], email="mitglied@example.org")


def test_jede_art_ist_steuerbar_oder_immer_aktiv() -> None:
    alle = {ntype.value for ntype in NotificationType}
    assert len(CONFIGURABLE_TYPES) == len(set(CONFIGURABLE_TYPES)), "Art doppelt im Formular"
    assert set(CONFIGURABLE_TYPES) | ALWAYS_ACTIVE_TYPES == alle
    assert not set(CONFIGURABLE_TYPES) & ALWAYS_ACTIVE_TYPES
    assert all(types for types in PREFERENCE_CATEGORIES.values())


@pytest.mark.django_db
def test_speichern_laesst_nicht_angezeigte_arten_unveraendert(mitglied: Any) -> None:
    prefs = services.ensure_notification_preferences(mitglied)
    prefs.type_settings = {
        "registration_request": {"in_app": True, "email": False},
        "unbekannte_art": {"in_app": False},
    }
    prefs.save()

    prefs = services.save_notification_preferences(
        mitglied, {"email_enabled": "on", "type_task_assigned_in_app": "on", "type_motion_assigned_email": "on"}
    )

    assert prefs.type_settings["registration_request"] == {"in_app": True, "email": False}
    assert prefs.type_settings["unbekannte_art"] == {"in_app": False}
    # Angezeigte Arten folgen dem Formular, auch die neu aufgenommenen
    assert prefs.type_settings["task_assigned"] == {"in_app": True, "email": False}
    assert prefs.type_settings["faction_invitation"] == {"in_app": False, "email": False}
    assert prefs.type_settings["motion_assigned"] == {"in_app": False, "email": True}
    assert prefs.type_settings["faction_prot_approved"] == {"in_app": False, "email": False}
    assert set(prefs.type_settings) == set(CONFIGURABLE_TYPES) | {"registration_request", "unbekannte_art"}


@pytest.mark.django_db
def test_immer_aktive_arten_ignorieren_gespeicherte_werte(mitglied: Any) -> None:
    assert {"registration_request"} == ALWAYS_ACTIVE_TYPES
    prefs = services.ensure_notification_preferences(mitglied)
    prefs.type_settings = {"registration_request": {"in_app": False, "email": False}}
    prefs.save()

    assert prefs.is_type_enabled("registration_request", "in_app") is True
    assert prefs.is_type_enabled("registration_request", "email") is True
    prefs.email_enabled = False
    assert prefs.is_type_enabled("registration_request", "email") is False, "globaler E-Mail-Schalter gilt weiter"


@pytest.mark.django_db
def test_formular_zeigt_die_bisher_fehlenden_arten(org: Any, mitglied: Any, client_for: Any) -> None:
    seite = client_for(mitglied.user).get(f"/work/{org.slug}/profile/notifications/").content.decode()
    for ntype in CONFIGURABLE_TYPES:
        assert f'name="type_{ntype}_in_app"' in seite, ntype
    for ntype in ALWAYS_ACTIVE_TYPES:
        assert f'name="type_{ntype}_in_app"' not in seite, ntype
    assert "Die förmliche Einladungs-Mail kommt unabhängig davon." in seite


def _angekreuzt(seite: str) -> dict[str, str]:
    """Formularwerte wie beim Absenden im Browser: alle angekreuzten Checkboxen (``name`` vor ``checked``)."""
    werte = {"email_digest": "instant"}
    for tag in re.findall(r"<input[^>]*>", seite):
        name = re.search(r'name="([^"]+)"', tag)
        if name and 'type="checkbox"' in tag and re.search(r"\schecked\b", tag):
            werte[name.group(1)] = "on"
    return werte


def _einladen(org: Any, mitglied: Any, make_member: Any) -> None:
    vorsitz = make_member(org, ["faction.invite"], email="vorsitz@example.org")
    meeting = FactionMeeting.objects.create(
        organization=org, title="Fraktionssitzung", start=timezone.now() + timedelta(days=7), created_by=vorsitz
    )
    FactionAttendance.objects.create(meeting=meeting, membership=mitglied, status="invited")
    dispatch_invitations(meeting)


@pytest.mark.django_db
def test_einladung_zur_fraktionssitzung_kommt_nach_dem_speichern_an(
    org: Any, mitglied: Any, make_member: Any, client_for: Any
) -> None:
    client = client_for(mitglied.user)
    url = f"/work/{org.slug}/profile/notifications/"
    werte = _angekreuzt(client.get(url).content.decode())
    assert werte.get("type_faction_invitation_in_app") == "on"
    werte.pop("type_task_assigned_in_app")  # etwas anderes abwählen und speichern

    assert client.post(url, werte).status_code == 302
    prefs = NotificationPreference.objects.get(membership=mitglied)
    assert prefs.type_settings["task_assigned"]["in_app"] is False
    assert prefs.type_settings["faction_invitation"]["in_app"] is True

    _einladen(org, mitglied, make_member)

    assert Notification.objects.filter(
        recipient=mitglied, notification_type=NotificationType.FACTION_INVITATION
    ).exists()


@pytest.mark.django_db
def test_abgewaehlte_einladung_nur_ohne_hinweis_die_mail_kommt(
    org: Any, mitglied: Any, make_member: Any, client_for: Any
) -> None:
    """Abwählen schaltet nur den Hinweis in mandari ab, die förmliche Einladungs-Mail geht trotzdem raus."""
    client = client_for(mitglied.user)
    url = f"/work/{org.slug}/profile/notifications/"
    werte = _angekreuzt(client.get(url).content.decode())
    werte.pop("type_faction_invitation_in_app")
    werte.pop("type_faction_invitation_email")
    assert client.post(url, werte).status_code == 302
    mail.outbox = []

    _einladen(org, mitglied, make_member)

    assert not Notification.objects.filter(
        recipient=mitglied, notification_type=NotificationType.FACTION_INVITATION
    ).exists()
    assert any(mitglied.user.email in nachricht.to for nachricht in mail.outbox)


# ---------------------------------------------------------------------------
# Datenmigration work/0060
# ---------------------------------------------------------------------------


def _alt_gespeichert() -> dict[str, dict[str, bool]]:
    """So sah type_settings nach einmaligem Speichern im alten Formular aus: alle Arten gesetzt."""
    werte = {ntype.value: {"in_app": False, "email": False} for ntype in NotificationType}
    werte["task_assigned"] = {"in_app": True, "email": False}
    return werte


@pytest.mark.django_db
def test_migrationsfunktion_ist_wiederholbar(org: Any, mitglied: Any, make_member: Any) -> None:
    prefs = services.ensure_notification_preferences(mitglied)
    prefs.type_settings = _alt_gespeichert()
    prefs.save()
    weiteres = make_member(org, ["dashboard.view"], email="weiteres@example.org")
    leer = NotificationPreference.objects.create(membership=weiteres, type_settings={})

    migration = importlib.import_module("apps.work.migrations.0060_benachrichtigungsarten_zuruecksetzen")
    migration.zuruecksetzen(django_apps, None)
    migration.zuruecksetzen(django_apps, None)

    prefs.refresh_from_db()
    assert not set(NIE_ANGEZEIGT) & set(prefs.type_settings)
    assert prefs.type_settings["task_assigned"] == {"in_app": True, "email": False}
    assert prefs.type_settings["motion_shared"] == {"in_app": False, "email": False}
    for ntype in NIE_ANGEZEIGT:
        assert prefs.is_type_enabled(ntype, "in_app") is True
    leer.refresh_from_db()
    assert leer.type_settings == {}


@pytest.mark.django_db(transaction=True)
def test_migration_repariert_den_bestand(mitglied: Any) -> None:
    """Echter Lauf: vor der Migration angelegte Einstellungen, danach gilt für die neun Arten der Standard."""
    executor = MigrationExecutor(connection)
    executor.migrate([VORHER])
    try:
        historisch = executor.loader.project_state([VORHER]).apps.get_model("work", "NotificationPreference")
        pk = historisch.objects.create(membership_id=mitglied.pk, type_settings=_alt_gespeichert()).pk

        executor = MigrationExecutor(connection)
        executor.migrate([NACHHER])

        prefs = NotificationPreference.objects.get(pk=pk)
        assert not set(NIE_ANGEZEIGT) & set(prefs.type_settings)
        assert prefs.type_settings["task_assigned"] == {"in_app": True, "email": False}
        assert prefs.is_type_enabled("faction_invitation", "in_app") is True
        assert prefs.is_type_enabled("motion_approval_req", "in_app") is True
        assert prefs.is_type_enabled("motion_shared", "in_app") is False
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
