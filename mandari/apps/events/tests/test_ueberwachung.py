# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Überwachung der Ereignistechnik (Issue #510): Metriken und Admin-Seite.

- ``mandari_events_lag_seconds{subscription}``: Rückstand je registriertem Abonnement, nur über die
  Typen, die es zugestellt bekommt; ``mandari_events_subscription_paused`` für den Alarmausschluss
- ``dispatch.set_state``: pausieren und fortsetzen unter der Zeilensperre
- Admin-Seite: nur für Superuser; Pausieren/Fortsetzen, Nachspielen ab Folgenummer oder Zeitpunkt
  (mit Zwischenseite), geparkte Ereignisse als Ketten je Objekt, erneut versuchen und verwerfen (mit
  Bestätigung), Aufträge und Worker lesend – jeder Eingriff im Sicherheitsprotokoll

Mit SQLite und PostgreSQL; ``mandari_events_published_total`` prüft ``test_sequencer.py`` (PostgreSQL).
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, cast

import pytest
from django.contrib.admin import helpers
from django.contrib.messages import get_messages
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from prometheus_client import REGISTRY

from apps.accounts.models import SecurityAuditLog
from apps.common.tests.factories import UserFactory
from apps.events import Delivery, dispatch, subscriber
from apps.events.metrics import subscription_lags
from apps.events.models import (
    Event,
    ParkedEvent,
    ParkedState,
    Subscription,
    SubscriptionState,
    Task,
    WorkerProcess,
)
from apps.events.tests.hilfen import nummeriert

pytestmark = pytest.mark.django_db

NAME = "test.ueberwachung"


def _handler(events: list[Event], delivery: Delivery) -> None:
    """Handler ohne Wirkung; die Tests stellen nicht zu."""


@pytest.fixture
def abo(leeres_register: dict[str, Any]) -> Subscription:
    """Ein registriertes Abonnement für ``test.*`` mit Cursor am Anfang des Journals."""
    subscriber(NAME, types=["test.*"])(_handler)
    return Subscription.objects.create(name=NAME, cursor_seq=0)


def _vor(minuten: int) -> Any:
    return timezone.now() - timedelta(minutes=minuten)


def _alt(ereignis: Event, minuten: int) -> Event:
    Event.objects.filter(pk=ereignis.pk).update(recorded_at=_vor(minuten))
    ereignis.refresh_from_db()
    return ereignis


def _wert(name: str, **labels: str) -> float | None:
    return REGISTRY.get_sample_value(name, labels)


# =============================================================================
# Metriken
# =============================================================================


def test_rueckstand_ist_das_alter_des_aeltesten_nicht_zugestellten_ereignisses(abo: Subscription) -> None:
    _alt(nummeriert(type="anderes.objekt.geaendert"), 60)  # bekommt das Abonnement nicht
    aeltestes = _alt(nummeriert(), 10)
    _alt(nummeriert(), 2)

    rueckstand = _wert("mandari_events_lag_seconds", subscription=NAME)

    assert rueckstand is not None and 9 * 60 <= rueckstand <= 11 * 60
    # Zugestellt bis zum ältesten: Es zählt das nächste
    Subscription.objects.filter(name=NAME).update(cursor_seq=aeltestes.seq)
    rueckstand = _wert("mandari_events_lag_seconds", subscription=NAME)
    assert rueckstand is not None and 60 <= rueckstand <= 3 * 60


def test_aktuelles_abonnement_hat_keinen_rueckstand(abo: Subscription) -> None:
    letztes = _alt(nummeriert(), 30)
    Subscription.objects.filter(name=NAME).update(cursor_seq=letztes.seq)
    nummeriert(type="anderes.objekt.geaendert")

    assert _wert("mandari_events_lag_seconds", subscription=NAME) == 0.0
    assert _wert("mandari_events_subscription_paused", subscription=NAME) == 0.0


def test_pausiertes_abonnement_ist_erkennbar(abo: Subscription) -> None:
    _alt(nummeriert(), 30)
    dispatch.set_state(NAME, SubscriptionState.PAUSIERT)

    assert _wert("mandari_events_subscription_paused", subscription=NAME) == 1.0
    rueckstand = _wert("mandari_events_lag_seconds", subscription=NAME)
    assert rueckstand is not None and rueckstand >= 29 * 60


def test_abonnement_ohne_handler_meldet_keinen_rueckstand(leeres_register: dict[str, Any]) -> None:
    """Eine Zeile ohne registrierten Handler bekommt nie wieder etwas zugestellt; sie darf nicht dauerhaft alarmieren."""
    Subscription.objects.create(name="test.verwaist", cursor_seq=0)
    _alt(nummeriert(), 30)

    assert _wert("mandari_events_lag_seconds", subscription="test.verwaist") is None
    assert _wert("mandari_events_subscription_paused", subscription="test.verwaist") == 0.0
    (stand,) = subscription_lags()
    assert stand.lag_seconds is None


# =============================================================================
# Zustand setzen
# =============================================================================


def test_zustand_setzen_gibt_den_vorigen_zurueck(abo: Subscription) -> None:
    assert dispatch.set_state(NAME, SubscriptionState.PAUSIERT) == SubscriptionState.AKTIV
    assert dispatch.set_state(NAME, SubscriptionState.PAUSIERT) == SubscriptionState.PAUSIERT
    assert dispatch.set_state(NAME, SubscriptionState.SCHATTEN) == SubscriptionState.PAUSIERT
    assert Subscription.objects.get(name=NAME).state == SubscriptionState.SCHATTEN
    assert dispatch.set_state("gibt.es.nicht", SubscriptionState.AKTIV) is None
    with pytest.raises(ValueError):
        dispatch.set_state(NAME, "irgendwas")


# =============================================================================
# Admin-Seite
# =============================================================================


@pytest.fixture
def admin(abo: Subscription) -> Client:
    client = Client()
    client.force_login(cast(Any, UserFactory)(email="betrieb@example.org", is_staff=True, is_superuser=True))
    return client


def _kette(anzahl: int = 3) -> list[ParkedEvent]:
    """Geparkte Ereignisse eines Objekts: das erste tot, die übrigen blockiert dahinter."""
    objekt = uuid.uuid4()
    kette = []
    for nummer in range(anzahl):
        ereignis = nummeriert(aggregate_id=objekt)
        kette.append(
            ParkedEvent.objects.create(
                subscription=NAME,
                event_seq=cast(int, ereignis.seq),
                aggregate_id=objekt,
                state=ParkedState.TOT if nummer == 0 else ParkedState.BLOCKIERT,
                attempts=8 if nummer == 0 else 0,
                error_code="builtins.RuntimeError" if nummer == 0 else None,
            )
        )
    return kette


def _aktion(client: Client, modell: str, aktion: str, objekte: list[Any], **weitere: str) -> Any:
    return client.post(
        reverse(f"admin:events_{modell}_changelist"),
        {"action": aktion, helpers.ACTION_CHECKBOX_NAME: [str(obj.pk) for obj in objekte], **weitere},
    )


def _eingriffe() -> list[dict[str, Any]]:
    return [eintrag.details for eintrag in SecurityAuditLog.objects.filter(event="betrieb").order_by("seq")]


@pytest.mark.parametrize("seite", ["subscription", "parkedevent", "task", "workerprocess"])
def test_nur_administratoren_sehen_die_seiten(abo: Subscription, seite: str) -> None:
    mitarbeiter = Client()
    mitarbeiter.force_login(cast(Any, UserFactory)(email="mitarbeit@example.org", is_staff=True))

    assert mitarbeiter.get(reverse(f"admin:events_{seite}_changelist")).status_code == 403


def test_mitarbeiter_ohne_adminrechte_koennen_nicht_eingreifen(abo: Subscription) -> None:
    mitarbeiter = Client()
    mitarbeiter.force_login(cast(Any, UserFactory)(email="mitarbeit@example.org", is_staff=True))

    antwort = _aktion(mitarbeiter, "subscription", "pausieren", [abo])

    assert antwort.status_code == 403
    assert Subscription.objects.get(name=NAME).state == SubscriptionState.AKTIV
    assert _eingriffe() == []


def test_abonnements_mit_rueckstand_und_geparkten_ereignissen(admin: Client) -> None:
    _alt(nummeriert(), 30)
    _kette(3)

    inhalt = admin.get(reverse("admin:events_subscription_changelist")).content.decode()

    assert NAME in inhalt
    assert "30 min" in inhalt
    assert "0 / 2 / 1" in inhalt  # wiederholen / blockiert / tot


def test_pausieren_und_fortsetzen_mit_eintrag_im_sicherheitsprotokoll(admin: Client) -> None:
    assert _aktion(admin, "subscription", "pausieren", [Subscription.objects.get(name=NAME)]).status_code == 302
    assert Subscription.objects.get(name=NAME).state == SubscriptionState.PAUSIERT

    # Fortsetzen im Schattenbetrieb wirkt nur auf pausierte, ein zweites Fortsetzen ändert nichts
    _aktion(admin, "subscription", "fortsetzen_im_schatten", [Subscription.objects.get(name=NAME)])
    _aktion(admin, "subscription", "fortsetzen", [Subscription.objects.get(name=NAME)])
    assert Subscription.objects.get(name=NAME).state == SubscriptionState.SCHATTEN

    assert _eingriffe() == [
        {"aktion": "abonnement_zustand", "abonnement": NAME, "vorher": "aktiv", "nachher": "pausiert"},
        {"aktion": "abonnement_zustand", "abonnement": NAME, "vorher": "pausiert", "nachher": "schatten"},
    ]
    eintrag = SecurityAuditLog.objects.filter(event="betrieb").first()
    assert eintrag is not None and eintrag.user_ref is not None and eintrag.entry_hash


def test_geparkte_ereignisse_als_ketten_je_objekt(admin: Client) -> None:
    kopf, *folgende = _kette(3)
    adresse = reverse("admin:events_parkedevent_changelist")

    kopfansicht = admin.get(adresse)
    alle = admin.get(adresse, {"kette": "alle"})
    blockierte = admin.get(adresse, {"state__exact": ParkedState.BLOCKIERT})

    assert list(kopfansicht.context["cl"].result_list) == [kopf]
    assert kopfansicht.context["cl"].result_list[0].n_folgende == 2
    assert kopfansicht.context["cl"].result_list[0].typ == "test.objekt.geaendert"
    assert {obj.pk for obj in alle.context["cl"].result_list} == {kopf.pk, *(obj.pk for obj in folgende)}
    assert {obj.pk for obj in blockierte.context["cl"].result_list} == {obj.pk for obj in folgende}


def test_erneut_versuchen_nur_fuer_den_kopf_der_kette(admin: Client) -> None:
    kopf, folgendes, _ = _kette(3)

    _aktion(admin, "parkedevent", "erneut_versuchen", [kopf, folgendes])

    kopf.refresh_from_db()
    folgendes.refresh_from_db()
    assert (kopf.state, kopf.attempts) == (ParkedState.WIEDERHOLEN, 0)
    assert folgendes.state == ParkedState.BLOCKIERT
    assert _eingriffe() == [
        {
            "aktion": "geparkt_wiederholen",
            "abonnement": NAME,
            "folgenummer": kopf.event_seq,
            "objekt": str(kopf.aggregate_id),
            "zustand": "tot",
            "versuche": 8,
            "fehlercode": "builtins.RuntimeError",
        }
    ]


def test_verwerfen_erst_nach_bestaetigung(admin: Client) -> None:
    kopf, naechstes, _ = _kette(3)

    bestaetigung = _aktion(admin, "parkedevent", "verwerfen", [kopf])

    assert bestaetigung.status_code == 200
    assert "admin/events/parkedevent/verwerfen_bestaetigen.html" in [t.name for t in bestaetigung.templates]
    assert f"Folgenummer {kopf.event_seq}" in bestaetigung.content.decode()
    assert ParkedEvent.objects.filter(pk=kopf.pk).exists() and _eingriffe() == []

    assert _aktion(admin, "parkedevent", "verwerfen", [kopf], post="ja").status_code == 302

    assert not ParkedEvent.objects.filter(pk=kopf.pk).exists()
    naechstes.refresh_from_db()
    assert naechstes.state == ParkedState.WIEDERHOLEN  # rückt nach und wird sofort zugestellt
    assert [eingriff["aktion"] for eingriff in _eingriffe()] == ["geparkt_verworfen"]


def _cursor(seq: int | None) -> None:
    Subscription.objects.filter(name=NAME).update(cursor_seq=cast(int, seq))


def _cursor_jetzt() -> int:
    return Subscription.objects.get(name=NAME).cursor_seq


def _meldungen(antwort: Any) -> list[str]:
    return [str(meldung) for meldung in get_messages(antwort.wsgi_request)]


def test_mitarbeiter_ohne_adminrechte_koennen_nicht_nachspielen(abo: Subscription) -> None:
    letztes = nummeriert()
    _cursor(letztes.seq)
    mitarbeiter = Client()
    mitarbeiter.force_login(cast(Any, UserFactory)(email="mitarbeit@example.org", is_staff=True))

    antwort = _aktion(mitarbeiter, "subscription", "nachspielen", [abo], post="ja", ab_folgenummer="1")

    assert antwort.status_code == 403
    assert _cursor_jetzt() == letztes.seq
    assert _eingriffe() == []


def test_nachspielen_ab_folgenummer_erst_nach_formular(admin: Client) -> None:
    erstes, zweites, drittes = nummeriert(), nummeriert(), nummeriert()
    _cursor(drittes.seq)
    abo = Subscription.objects.get(name=NAME)

    formular = _aktion(admin, "subscription", "nachspielen", [abo])

    assert formular.status_code == 200
    assert "admin/events/subscription/nachspielen.html" in [t.name for t in formular.templates]
    assert f"Cursor {drittes.seq}" in formular.content.decode()
    assert _cursor_jetzt() == drittes.seq and _eingriffe() == []

    antwort = _aktion(admin, "subscription", "nachspielen", [abo], post="ja", ab_folgenummer=str(zweites.seq))

    assert antwort.status_code == 302
    assert _cursor_jetzt() == erstes.seq  # zweites und drittes werden erneut zugestellt
    assert _eingriffe() == [
        {
            "aktion": "abonnement_nachspielen",
            "abonnement": NAME,
            "vorher": drittes.seq,
            "nachher": erstes.seq,
            "geparkt_aufgehoben": 0,
        }
    ]
    eintrag = SecurityAuditLog.objects.get(event="betrieb")
    assert eintrag.user_ref is not None and eintrag.entry_hash
    assert any(f"ab Folgenummer {zweites.seq}" in meldung for meldung in _meldungen(antwort))


def test_nachspielen_ab_zeitpunkt(admin: Client) -> None:
    _alt(nummeriert(), 60)
    ab_hier = _alt(nummeriert(), 10)
    letztes = nummeriert()
    _cursor(letztes.seq)
    seit = timezone.localtime(_vor(30)).strftime("%Y-%m-%d %H:%M")

    antwort = _aktion(admin, "subscription", "nachspielen", [Subscription.objects.get(name=NAME)], post="ja", seit=seit)

    assert antwort.status_code == 302
    assert _cursor_jetzt() == cast(int, ab_hier.seq) - 1
    assert [eingriff["aktion"] for eingriff in _eingriffe()] == ["abonnement_nachspielen"]


@pytest.mark.parametrize(
    "angaben",
    [{}, {"ab_folgenummer": "1", "seit": "2026-10-01 00:00"}, {"ab_folgenummer": "0"}, {"seit": "kein Datum"}],
)
def test_nachspielen_braucht_genau_eine_gueltige_angabe(admin: Client, angaben: dict[str, str]) -> None:
    letztes = nummeriert()
    _cursor(letztes.seq)

    antwort = _aktion(admin, "subscription", "nachspielen", [Subscription.objects.get(name=NAME)], post="ja", **angaben)

    assert antwort.status_code == 200
    assert antwort.context["form"].errors
    assert _cursor_jetzt() == letztes.seq and _eingriffe() == []


def test_nachspielen_setzt_den_cursor_nur_zurueck(admin: Client) -> None:
    erstes, _, drittes = nummeriert(), nummeriert(), nummeriert()
    _cursor(erstes.seq)

    antwort = _aktion(
        admin,
        "subscription",
        "nachspielen",
        [Subscription.objects.get(name=NAME)],
        post="ja",
        ab_folgenummer=str(drittes.seq),
    )

    assert antwort.status_code == 302
    assert _cursor_jetzt() == erstes.seq  # Ereignisse überspringen gibt es nicht
    assert _eingriffe() == []
    assert any("Unverändert" in meldung and NAME in meldung for meldung in _meldungen(antwort))


def test_auftraege_lesend(admin: Client) -> None:
    auftrag = Task.objects.create(queue="mail", task_path="apps.common.email.senden", args={"id": "1"})

    liste = admin.get(reverse("admin:events_task_changelist"))
    detail = admin.get(reverse("admin:events_task_change", args=[auftrag.pk]))

    assert liste.status_code == detail.status_code == 200
    assert "apps.common.email.senden" in liste.content.decode()
    assert admin.post(reverse("admin:events_task_delete", args=[auftrag.pk]), {"post": "yes"}).status_code == 403
    assert Task.objects.filter(pk=auftrag.pk).exists()


def test_worker_lesend_mit_zustand(admin: Client) -> None:
    """Laufende und veraltete Worker-Prozesse (Issue #510), nur lesend."""
    WorkerProcess.objects.create(holder="host:1:lebt", roles=["sequencer", "dispatch"], queues=[])
    veraltet = WorkerProcess.objects.create(holder="host:2:alt", roles=["tasks"], queues=["ocr", "ai"])
    WorkerProcess.objects.filter(pk=veraltet.pk).update(seen_at=timezone.now() - timedelta(minutes=10))

    liste = admin.get(reverse("admin:events_workerprocess_changelist"))
    inhalt = liste.content.decode()

    assert liste.status_code == 200
    assert "host:1:lebt" in inhalt and "sequencer, dispatch" in inhalt and "alle" in inhalt
    assert "host:2:alt" in inhalt and "ocr, ai" in inhalt
    assert inhalt.count(">lebt<") == 1 and inhalt.count(">veraltet<") == 1
    loeschen = reverse("admin:events_workerprocess_delete", args=[veraltet.pk])
    assert admin.post(loeschen, {"post": "yes"}).status_code == 403
    assert WorkerProcess.objects.count() == 2
