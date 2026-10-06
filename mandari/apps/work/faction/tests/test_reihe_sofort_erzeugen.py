# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungsreihe: Termine sofort beim Anlegen und Ändern erzeugen (Issue #896).

Vorher kamen die Termine erst mit dem stündlichen Zeitplan ``fraktionssitzungen_erzeugen``; bis dahin zeigte Work
„Noch keine Fraktionssitzungen vorhanden“. Der Zeitplan bleibt für das Weiterrollen zuständig. Zeitplanlauf und
sofortige Erzeugung teilen sich eine Sperre je Reihe, angelegt wird nichts doppelt.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, time, timedelta
from typing import Any

import pytest
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from apps.work.faction import generation
from apps.work.faction.models import FactionMeeting, FactionMeetingException, FactionMeetingSchedule

CaptureCallbacks = Callable[..., Any]
HORIZONT = 21


@pytest.fixture(autouse=True)
def _horizont(settings: Any) -> None:
    settings.FACTION_SCHEDULE_HORIZON_DAYS = HORIZONT


@pytest.fixture
def manager(org: Any, make_member: Any) -> Any:
    return make_member(org, ["faction.manage", "faction.view_public"], email="vorsitz@example.org")


def _url(org: Any) -> str:
    return reverse("work:organization_faction_settings", kwargs={"org_slug": org.slug})


def _morgen() -> date:
    return timezone.localdate() + timedelta(days=1)


def _soll_termine() -> list[date]:
    """Wöchentlich am Wochentag von morgen: morgen und die zwei folgenden Wochen liegen im Horizont von 21 Tagen."""
    return [_morgen() + timedelta(weeks=woche) for woche in range(3)]


def _termine(reihe: FactionMeetingSchedule) -> dict[date, FactionMeeting]:
    return {m.scheduled_date: m for m in FactionMeeting.objects.filter(schedule=reihe) if m.scheduled_date}


def _reihe(org: Any, **felder: Any) -> FactionMeetingSchedule:
    return FactionMeetingSchedule.objects.create(
        organization=org, name="Fraktionssitzung", weekday=_morgen().weekday(), time=time(18, 0), **felder
    )


@pytest.mark.django_db
def test_neue_reihe_hat_sofort_termine(
    org: Any, manager: Any, client_for: Any, django_capture_on_commit_callbacks: CaptureCallbacks
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        response = client_for(manager.user).post(
            _url(org),
            {
                "section": "add_schedule",
                "name": "Fraktionssitzung",
                "weekday": str(_morgen().weekday()),
                "time": "18:00",
            },
        )

    assert response.status_code == 302
    reihe = FactionMeetingSchedule.objects.get(organization=org)
    termine = _termine(reihe)
    assert sorted(termine) == _soll_termine()
    assert {m.status for m in termine.values()} == {"planned"}
    reihe.refresh_from_db()
    assert reihe.generated_until == timezone.localdate() + timedelta(days=HORIZONT)


@pytest.mark.django_db
def test_pause_eintragen_sagt_sofort_ab_und_legt_spaetere_termine_entfallend_an(
    org: Any, manager: Any, client_for: Any, django_capture_on_commit_callbacks: CaptureCallbacks
) -> None:
    reihe = _reihe(org)
    erster, zweiter, dritter = _soll_termine()
    # Erzeugt bis zum zweiten Termin; der dritte liegt hinter dem Wasserstand
    generation.generate_meetings_now(reihe)
    FactionMeeting.objects.filter(schedule=reihe, scheduled_date=dritter).delete()
    FactionMeetingSchedule.objects.filter(pk=reihe.pk).update(generated_until=zweiter)

    with django_capture_on_commit_callbacks(execute=True):
        client_for(manager.user).post(
            _url(org),
            {
                "section": "add_exception",
                "schedule_id": str(reihe.id),
                "original_date": zweiter.isoformat(),
                "end_date": dritter.isoformat(),
                "reason": "Ferien",
            },
        )

    termine = _termine(reihe)
    assert termine[erster].status == "planned"
    assert termine[zweiter].status == "cancelled", "schon angelegter Termin entfällt sofort"
    assert termine[dritter].status == "cancelled", "Termin hinter dem Wasserstand sofort als „Entfällt“"
    assert "Ferien" in termine[dritter].cancellation_reason


@pytest.mark.django_db
def test_ausnahme_entfernen_erzeugt_sofort_weiter(
    org: Any, manager: Any, client_for: Any, django_capture_on_commit_callbacks: CaptureCallbacks
) -> None:
    reihe = _reihe(org)
    ausnahme = FactionMeetingException.objects.create(
        schedule=reihe, original_date=timezone.localdate() - timedelta(days=30), reason="Alt"
    )

    with django_capture_on_commit_callbacks(execute=True):
        client_for(manager.user).post(_url(org), {"section": "delete_exception", "exception_id": str(ausnahme.id)})

    assert not FactionMeetingException.objects.filter(id=ausnahme.id).exists()
    assert sorted(_termine(reihe)) == _soll_termine()


@pytest.mark.django_db
def test_wiederaufnahme_erzeugt_sofort_pausieren_nicht(
    org: Any, manager: Any, client_for: Any, django_capture_on_commit_callbacks: CaptureCallbacks
) -> None:
    reihe = _reihe(org, is_active=False)

    with django_capture_on_commit_callbacks(execute=True):
        client_for(manager.user).post(_url(org), {"section": "toggle_schedule", "schedule_id": str(reihe.id)})
    assert sorted(_termine(reihe)) == _soll_termine()

    FactionMeeting.objects.filter(schedule=reihe).delete()
    FactionMeetingSchedule.objects.filter(pk=reihe.pk).update(generated_until=None)
    with django_capture_on_commit_callbacks(execute=True):
        client_for(manager.user).post(_url(org), {"section": "toggle_schedule", "schedule_id": str(reihe.id)})
    reihe.refresh_from_db()
    assert reihe.is_active is False
    assert not FactionMeeting.objects.filter(schedule=reihe).exists(), "pausierte Reihe erzeugt nichts"


@pytest.mark.django_db
def test_laufende_erzeugung_der_reihe_wird_nicht_doppelt_gestartet(org: Any) -> None:
    reihe = _reihe(org)
    key = generation._schedule_lock_key(reihe.pk)
    cache.add(key, "1")
    try:
        assert generation.generate_meetings_now(reihe) is None
        assert generation.run_faction_schedule_pass()["schedules"] == 0
        assert not FactionMeeting.objects.filter(schedule=reihe).exists()
    finally:
        cache.delete(key)

    generation.generate_meetings_now(reihe)
    stats = generation.run_faction_schedule_pass()

    assert stats["created"] == 0
    assert FactionMeeting.objects.filter(schedule=reihe).count() == len(_soll_termine())


@pytest.mark.django_db
def test_zeitplanlauf_liest_den_wasserstand_frisch(org: Any) -> None:
    """Hat eine Anfrage die Reihe inzwischen erzeugt, kommen danach gelöschte Termine nicht zurück (Issue #871)."""
    reihe = _reihe(org)
    veraltet = FactionMeetingSchedule.objects.get(pk=reihe.pk)
    generation.generate_meetings_now(reihe)
    FactionMeeting.objects.filter(schedule=reihe, scheduled_date=_soll_termine()[1]).delete()

    generation._generate_locked(veraltet)

    assert sorted(_termine(reihe)) == [_soll_termine()[0], _soll_termine()[2]]


@pytest.mark.django_db
def test_fehler_bei_der_sofortigen_erzeugung_verhindert_das_anlegen_nicht(
    org: Any,
    manager: Any,
    client_for: Any,
    django_capture_on_commit_callbacks: CaptureCallbacks,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _kaputt(*args: Any, **kwargs: Any) -> dict[str, int]:
        raise RuntimeError("Testfehler")

    monkeypatch.setattr(generation, "generate_meetings_for_schedule", _kaputt)

    with django_capture_on_commit_callbacks(execute=True):
        response = client_for(manager.user).post(
            _url(org), {"section": "add_schedule", "name": "Reihe", "weekday": "0", "time": "18:00"}
        )

    assert response.status_code == 302
    reihe = FactionMeetingSchedule.objects.get(organization=org)
    assert not FactionMeeting.objects.filter(schedule=reihe).exists()
    assert cache.add(generation._schedule_lock_key(reihe.pk), "1"), "Sperre wieder frei"
    cache.delete(generation._schedule_lock_key(reihe.pk))


@pytest.mark.django_db
def test_neue_ris_regel_sagt_schon_angelegte_termine_ab(
    org: Any, manager: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Die Termine gibt es meist schon, wenn die Regel dazukommt; wie bei Pausen nur nicht eingeladene."""
    reihe = _reihe(org)
    generation.generate_meetings_now(reihe)
    erster, zweiter, dritter = _soll_termine()
    FactionMeeting.objects.filter(schedule=reihe, scheduled_date=erster).update(invitation_sent=True, status="invited")

    def _regel(schedule: Any, datum: date, rules: Any = None) -> str | None:
        return "Entfällt nach Sitzung von Rat" if datum in (erster, zweiter) else None

    monkeypatch.setattr(generation, "check_ris_rules", _regel)

    assert generation.cancel_meetings_by_ris_rules(reihe) == 1

    termine = _termine(reihe)
    assert termine[erster].status == "invited", "Eingeladene sagt der Vorsitz selbst ab"
    assert termine[zweiter].status == "cancelled"
    assert termine[zweiter].cancellation_reason == "Entfällt nach Sitzung von Rat"
    assert termine[zweiter].attendances.exists(), "Anwesenheiten bleiben erhalten"
    assert termine[dritter].status == "planned"
