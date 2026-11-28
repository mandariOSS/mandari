# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Datenabgleich mit den Schnittstellen der Kommunen (Issue #1525).

- Interne Konsistenz im Messfenster: TOPs, deren Beratung fehlt; TOPs, für die Work keine Vorlage findet (Beratung ohne
  Rückverweis auf den TOP, wie bei SD.NET RIM); Beratungen ohne Vorlage; Vorlagen ohne Dateien, obwohl die Rohdaten
  welche nennen; lückenhafte Listen aus den Vermerken des Ingestors.
- Ampel je Kommune aus den abgelegten Befunden, Datenstand, Abgleich mit der Quelle (``totalElements``).
- Betriebsmonitor: Eine kritische Kommune macht die Quelle kritisch und löst den Alarm aus; Prüfung ``datenabgleich``
  der Statusseite.
- Befehl ``datenabgleich``: Tabelle, Ablage unter Zeilensperre, ``--report`` legt nichts ab.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from io import StringIO
from typing import Any

import pytest
from django.core import mail
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlFile,
    OParlMeeting,
    OParlPaper,
    OParlSource,
)
from insight_core.services import datenabgleich, source_health
from insight_core.services.source_health import evaluate_source, send_health_alerts

BASIS = "https://ris.beispiel.example/oparl/v1.1/body/1"


@pytest.fixture
def quelle(db: Any) -> OParlSource:
    jetzt = timezone.now()
    return OParlSource.objects.create(
        name="Beispielstadt",
        url="https://ris.beispiel.example/oparl/v1.1/system",
        last_sync=jetzt - timedelta(minutes=10),
        last_successful_sync=jetzt - timedelta(minutes=10),
        last_successful_full_sync=jetzt - timedelta(days=1),
        sync_config={
            "body_sync_started": {BASIS: (jetzt - timedelta(hours=2)).isoformat()},
            "slow_lists": {f"{BASIS}/consultation": {"reason": "Zeitüberschreitung", "since": "2026-09-27T00:00:00"}},
            "sync_window": {"start": "23:00", "end": "01:00"},
        },
    )


@pytest.fixture
def kommune(quelle: OParlSource) -> OParlBody:
    return OParlBody.objects.create(
        external_id=BASIS,
        source=quelle,
        name="Beispielstadt",
        raw_json={
            "paper": f"{BASIS}/paper",
            "meeting": f"{BASIS}/meeting",
            "consultation": f"{BASIS}/consultation",
            "agendaItem": f"{BASIS}/agendaitem",
            "file": f"{BASIS}/file",
        },
    )


def _sitzung(body: OParlBody, nummer: int, start: datetime) -> OParlMeeting:
    return OParlMeeting.objects.create(external_id=f"{BASIS}/meeting/{nummer}", body=body, name="Rat", start=start)


def _top(sitzung: OParlMeeting, nummer: int, beratung: str | None = None) -> OParlAgendaItem:
    raw: dict[str, Any] = {"id": f"{BASIS}/agendaitem/{nummer}"}
    if beratung is not None:
        raw["consultation"] = beratung
    return OParlAgendaItem.objects.create(
        external_id=f"{BASIS}/agendaitem/{nummer}", meeting=sitzung, name=f"TOP {nummer}", raw_json=raw
    )


def _vorlage(body: OParlBody, nummer: int, **raw: Any) -> OParlPaper:
    return OParlPaper.objects.create(
        external_id=f"{BASIS}/paper/{nummer}", body=body, name=f"Vorlage {nummer}", raw_json=raw
    )


def _beratung(
    body: OParlBody, nummer: int, *, top: OParlAgendaItem | None = None, vorlage: OParlPaper | None = None
) -> OParlConsultation:
    return OParlConsultation.objects.create(
        external_id=f"{BASIS}/consultation/{nummer}",
        body=body,
        paper=vorlage,
        agenda_item_external_id=top.external_id if top else None,
    )


@pytest.fixture
def bestand(kommune: OParlBody) -> dict[str, Any]:
    """Eine Sitzung im Fenster mit allen Fällen, eine außerhalb."""
    jetzt = timezone.now()
    sitzung = _sitzung(kommune, 1, jetzt + timedelta(days=5))
    alt = _sitzung(kommune, 2, jetzt - timedelta(days=30))
    gut = _vorlage(kommune, 1, mainFile={"id": f"{BASIS}/file/1"})
    OParlFile.objects.create(external_id=f"{BASIS}/file/1", body=kommune, paper=gut, name="Vorlage.pdf")
    ohne_datei = _vorlage(kommune, 2, auxiliaryFile=[{"id": f"{BASIS}/file/2"}])
    ohne_angabe = _vorlage(kommune, 3)

    a = _top(sitzung, 1, f"{BASIS}/consultation/1")  # in Ordnung
    _beratung(kommune, 1, top=a, vorlage=gut)
    _top(sitzung, 2, f"{BASIS}/consultation/2")  # Beratung fehlt
    c = _top(sitzung, 3, f"{BASIS}/consultation/3")  # Beratung ohne Rückverweis (SD.NET): Work findet nichts
    _beratung(kommune, 3, vorlage=ohne_datei)
    d = _top(sitzung, 4)  # ohne Beratungsverweis, aber mit Beratung ohne Vorlage
    _beratung(kommune, 4, top=d)
    e = _top(sitzung, 5, f"{BASIS}/consultation/5")
    _beratung(kommune, 5, top=e, vorlage=ohne_angabe)
    _top(alt, 6, f"{BASIS}/consultation/6")  # außerhalb des Fensters
    return {"c": c}


def test_interne_konsistenz_zaehlt_die_luecken_im_fenster(kommune: OParlBody, bestand: dict[str, Any]) -> None:
    befund = datenabgleich.pruefe_kommune(kommune)

    assert befund["tops"] == 5
    assert befund["mit_beratung"] == 4
    assert befund["beratung_fehlt"] == 1
    assert befund["work_ohne_vorlage"] == 2, "Beratung fehlt bzw. nennt den TOP nicht"
    assert befund["top_luecke"] == 2
    assert befund["beispiele"] == [f"{BASIS}/agendaitem/2", f"{BASIS}/agendaitem/3"]
    assert befund["beratungen"] == 4
    assert befund["beratung_ohne_vorlage"] == 1
    assert befund["vorlagen"] == 3
    assert befund["vorlage_ohne_dateien"] == 1, "nur die Vorlage, deren Rohdaten Dateien nennen"
    assert befund["listen"] == ["Beratungen ausgesetzt (Zeitüberschreitung, Lücke ab 2026-09-27)"]
    assert befund["name"] == "Beispielstadt"


def test_beratung_einer_fremden_quelle_zaehlt_nicht(kommune: OParlBody, bestand: dict[str, Any]) -> None:
    """Wie Work: Ein Rückverweis gilt nur innerhalb der Quelle (``hub.ris.selectors``)."""
    fremd = OParlSource.objects.create(name="Fremd", url="https://fremd.example/oparl/system")
    fremde = OParlBody.objects.create(external_id="https://fremd.example/body/1", source=fremd, name="Fremd")
    OParlConsultation.objects.create(
        external_id="https://fremd.example/consultation/1",
        body=fremde,
        paper=_vorlage(fremde, 9),
        agenda_item_external_id=bestand["c"].external_id,
    )

    assert datenabgleich.pruefe_kommune(kommune)["work_ohne_vorlage"] == 2


def _befund(**werte: Any) -> dict[str, Any]:
    befund = {
        "at": timezone.now().isoformat(),
        "name": "Beispielstadt",
        "tops": 100,
        "mit_beratung": 50,
        "beratung_fehlt": 0,
        "work_ohne_vorlage": 0,
        "top_luecke": 0,
        "beratungen": 50,
        "beratung_ohne_vorlage": 0,
        "vorlagen": 40,
        "vorlage_ohne_dateien": 0,
        "listen": [],
        "beispiele": [],
    }
    befund.update(werte)
    return befund


def _mit(quelle: OParlSource, **eintraege: Any) -> OParlSource:
    quelle.sync_config = {**quelle.sync_config, **eintraege}
    quelle.save()
    return quelle


@pytest.mark.parametrize(
    ("luecke", "erwartet"),
    [(0, "ok"), (2, "ok"), (3, "warning"), (9, "warning"), (10, "critical"), (40, "critical")],
)
def test_ampel_der_top_luecke(quelle: OParlSource, luecke: int, erwartet: str) -> None:
    _mit(quelle, datenabgleich={BASIS: _befund(top_luecke=luecke, work_ohne_vorlage=luecke)})

    (kommune,) = datenabgleich.bewerte_kommunen(quelle)

    assert kommune.status == erwartet
    if erwartet != "ok":
        assert kommune.gruende[0].startswith(f"{luecke} von 50 TOPs mit Beratungsverweis ohne Vorlage")


def test_kleiner_anteil_bleibt_ok(quelle: OParlSource) -> None:
    """Zehn Lücken bei 1000 TOPs (1 %) sind kein Alarm, nur ein Hinweis."""
    _mit(quelle, datenabgleich={BASIS: _befund(mit_beratung=1000, top_luecke=10)})

    (kommune,) = datenabgleich.bewerte_kommunen(quelle)

    assert kommune.status == "ok"
    assert kommune.hinweise and "10 von 1000" in kommune.hinweise[0]


@pytest.mark.parametrize(
    ("alter", "erwartet"),
    [(timedelta(hours=30), "ok"), (timedelta(days=4), "warning"), (timedelta(days=8), "critical")],
)
def test_datenstand_der_kommune(quelle: OParlSource, alter: timedelta, erwartet: str) -> None:
    """Der spätere Zeitpunkt aus Änderungsfenster der Kommune und lückenlosem Vollabgleich zählt."""
    jetzt = timezone.now()
    quelle.last_successful_full_sync = jetzt - alter - timedelta(days=5)
    _mit(
        quelle,
        body_sync_started={BASIS: (jetzt - alter).isoformat()},
        datenabgleich={BASIS: _befund()},
    )

    (kommune,) = datenabgleich.bewerte_kommunen(quelle, jetzt)

    assert kommune.status == erwartet
    assert kommune.stand == jetzt - alter


def test_vollabgleich_deckt_das_haengende_fenster(quelle: OParlSource) -> None:
    jetzt = timezone.now()
    quelle.last_successful_full_sync = jetzt - timedelta(hours=20)
    _mit(quelle, body_sync_started={BASIS: (jetzt - timedelta(days=9)).isoformat()}, datenabgleich={BASIS: _befund()})

    assert datenabgleich.bewerte_kommunen(quelle, jetzt)[0].status == "ok"


def test_veraltete_befunde_zaehlen_nicht(quelle: OParlSource) -> None:
    alt = (timezone.now() - timedelta(days=3)).isoformat()
    _mit(
        quelle,
        datenabgleich={BASIS: _befund(at=alt, top_luecke=40)},
        datenabgleich_quelle={BASIS: {"at": (timezone.now() - timedelta(days=4)).isoformat(), "listen": {}}},
    )

    (kommune,) = datenabgleich.bewerte_kommunen(quelle)

    assert kommune.status == "ok"
    assert any("veraltet" in hinweis for hinweis in kommune.hinweise)


def test_abgleich_mit_der_quelle(quelle: OParlSource) -> None:
    jetzt = timezone.now().isoformat()
    _mit(
        quelle,
        datenabgleich_quelle={
            BASIS: {
                "at": jetzt,
                "name": "Beispielstadt",
                "listen": {
                    "paper": {"quelle": 64982, "bestand": 1835},
                    "meeting": {"quelle": 1000, "bestand": 960},
                    "consultation": {"fehler": "HTTP 504", "bestand": 10},
                    "agendaitem": {"quelle": None, "bestand": 500},
                    "file": {"quelle": 100, "bestand": 300},
                },
            }
        },
    )

    (kommune,) = datenabgleich.bewerte_kommunen(quelle)

    assert kommune.status == "critical"
    assert kommune.gruende == ["Vorlagen: Quelle 64982, Bestand 1835 (63147 fehlen, 97%)"]
    assert any("Sitzungen: Quelle 1000, Bestand 960" in h for h in kommune.hinweise), "4 % ist nur ein Hinweis"
    assert any("Beratungen: Liste der Quelle nicht abrufbar (HTTP 504)" in h for h in kommune.hinweise)
    assert any("Dateien: Bestand 300 größer als Quelle 100" in h for h in kommune.hinweise)


def test_kritische_kommune_macht_die_quelle_kritisch_mit_alarm(quelle: OParlSource, settings: Any) -> None:
    settings.INSIGHT_ALERT_EMAILS = ["betrieb@example.org"]
    _mit(quelle, datenabgleich={BASIS: _befund(top_luecke=37, beratung_fehlt=37, work_ohne_vorlage=20)})

    item = evaluate_source(quelle)

    assert item["status"] == "critical"
    assert any(r.startswith("Datenabgleich Beispielstadt kritisch: 37 von 50 TOPs") for r in item["reasons"])
    assert "manage.py datenabgleich" in item["recommendation"]
    ergebnis = send_health_alerts(uebersicht=True)
    assert ergebnis["alerts"] == [], "Datenlücken sind keine Störung des Abrufs"
    assert ergebnis["gaps"] == ["Beispielstadt"]
    assert mail.outbox[0].subject == "[mandari] Datenlücken: Beispielstadt"
    assert "Datenabgleich Beispielstadt kritisch" in mail.outbox[0].body
    quelle.refresh_from_db()
    assert quelle.datenabgleich_alert_sent_at is not None
    assert quelle.health_alert_sent_at is None


def test_datenluecken_nur_im_taeglichen_uebersichtslauf(
    quelle: OParlSource, settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.INSIGHT_ALERT_EMAILS = ["betrieb@example.org"]
    _mit(quelle, datenabgleich={BASIS: _befund(top_luecke=37, beratung_fehlt=37, work_ohne_vorlage=20)})
    monkeypatch.setattr(source_health, "ist_uebersichtslauf", lambda now: False)

    assert send_health_alerts()["mail"] is False
    assert mail.outbox == []

    monkeypatch.setattr(source_health, "ist_uebersichtslauf", lambda now: True)
    assert send_health_alerts()["gaps"] == ["Beispielstadt"]
    assert len(mail.outbox) == 1
    assert send_health_alerts()["mail"] is False, "dieselbe Quelle frühestens nach 7 Tagen wieder"


def test_uebersichtslauf_nach_dem_datenabgleich(settings: Any) -> None:
    settings.INSIGHT_DATENABGLEICH_MAIL_HOUR = 7
    winter = datetime(2026, 11, 28, 6, 15, tzinfo=UTC)  # 07:15 MEZ
    sommer = datetime(2026, 6, 28, 5, 15, tzinfo=UTC)  # 07:15 MESZ

    assert source_health.ist_uebersichtslauf(winter)
    assert source_health.ist_uebersichtslauf(sommer)
    assert not source_health.ist_uebersichtslauf(winter + timedelta(hours=1))


def test_datenluecken_schalten_eine_stoerung_nicht_stumm(quelle: OParlSource, settings: Any) -> None:
    settings.INSIGHT_ALERT_EMAILS = ["betrieb@example.org"]
    _mit(quelle, datenabgleich={BASIS: _befund(top_luecke=37, beratung_fehlt=37, work_ohne_vorlage=20)})
    send_health_alerts(uebersicht=True)
    quelle.refresh_from_db()
    quelle.last_error_kind = OParlSource.ERROR_KIND_UA_BLOCKED
    quelle.save(update_fields=["last_error_kind"])
    mail.outbox.clear()

    ergebnis = send_health_alerts(uebersicht=True)

    assert ergebnis["alerts"] == ["Beispielstadt"]
    assert ergebnis["gaps"] == []
    assert mail.outbox[0].subject == "[mandari] Quelle gestört: Beispielstadt"


def test_entwarnung_wenn_die_luecken_geschlossen_sind(quelle: OParlSource, settings: Any) -> None:
    settings.INSIGHT_ALERT_EMAILS = ["betrieb@example.org"]
    _mit(quelle, datenabgleich={BASIS: _befund(top_luecke=37, beratung_fehlt=37, work_ohne_vorlage=20)})
    send_health_alerts(uebersicht=True)
    quelle.refresh_from_db()  # sonst überschreibt das Speichern den eben gesetzten Zeitstempel
    _mit(quelle, datenabgleich={BASIS: _befund()})
    mail.outbox.clear()

    assert send_health_alerts(uebersicht=False)["mail"] is False, "Entwarnung der Lücken nur im Übersichtslauf"
    ergebnis = send_health_alerts(uebersicht=True)

    assert ergebnis["gaps_closed"] == ["Beispielstadt"]
    assert ergebnis["recoveries"] == []
    assert "Beispielstadt: Datenlücken nicht mehr kritisch" in mail.outbox[0].body
    quelle.refresh_from_db()
    assert quelle.datenabgleich_alert_sent_at is None


@pytest.mark.django_db(transaction=True)
def test_migration_uebernimmt_bisherige_hinweise_auf_datenluecken() -> None:
    vorher = ("insight_core", "0076_beratung_punkt_abgeleitet")
    nachher = ("insight_core", "0077_datenluecken_gemeldet")
    executor = MigrationExecutor(connection)
    executor.migrate([vorher])
    try:
        alt = MigrationExecutor(connection).loader.project_state([vorher]).apps.get_model("insight_core", "OParlSource")
        jetzt = timezone.now()
        luecke = alt.objects.create(
            name="Lücke", url="https://ris.example.org/luecke/system", last_sync=jetzt, health_alert_sent_at=jetzt
        )
        gesperrt = alt.objects.create(
            name="Gesperrt",
            url="https://ris.example.org/gesperrt/system",
            last_sync=jetzt,
            last_error_kind="ua_blocked",
            health_alert_sent_at=jetzt,
        )
        veraltet = alt.objects.create(
            name="Veraltet",
            url="https://ris.example.org/veraltet/system",
            last_sync=jetzt - timedelta(days=9),
            health_alert_sent_at=jetzt,
        )

        MigrationExecutor(connection).migrate([nachher])
        neu = (
            MigrationExecutor(connection).loader.project_state([nachher]).apps.get_model("insight_core", "OParlSource")
        )
        stand = {
            q.pk: (q.health_alert_sent_at is not None, q.datenabgleich_alert_sent_at is not None)
            for q in neu.objects.all()
        }
        assert stand[luecke.pk] == (False, True)
        assert stand[gesperrt.pk] == stand[veraltet.pk] == (True, False), "Störungen behalten ihren Alarm"

        MigrationExecutor(connection).migrate([vorher])
        zurueck = (
            MigrationExecutor(connection).loader.project_state([vorher]).apps.get_model("insight_core", "OParlSource")
        )
        assert zurueck.objects.get(pk=luecke.pk).health_alert_sent_at is not None
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())


def test_auffaellige_kommune_ist_eine_warnung(quelle: OParlSource) -> None:
    _mit(quelle, datenabgleich={BASIS: _befund(top_luecke=4)})

    item = evaluate_source(quelle)

    assert item["status"] == "warning"


def test_ohne_befund_aendert_sich_nichts(quelle: OParlSource) -> None:
    item = evaluate_source(quelle)

    assert item["status"] == "ok"
    assert item["reasons"] == []
    assert datenabgleich.check_datenabgleich().ok


def test_pruefung_der_statusseite(quelle: OParlSource) -> None:
    _mit(quelle, datenabgleich={BASIS: _befund(top_luecke=37)})

    pruefung = datenabgleich.check_datenabgleich()

    assert not pruefung.ok
    assert pruefung.detail == "1 Kommunen kritisch, 0 auffällig, 0 ok"


def test_befehl_legt_ab_und_zeigt_die_tabelle(quelle: OParlSource, kommune: OParlBody, bestand: dict[str, Any]) -> None:
    aus = StringIO()
    call_command("datenabgleich", "--trotz-zeitplan", stdout=aus)

    quelle.refresh_from_db()
    abgelegt = quelle.sync_config[datenabgleich.SCHLUESSEL_INTERN][BASIS]
    assert abgelegt["top_luecke"] == 2
    assert quelle.sync_config["sync_window"] == {"start": "23:00", "end": "01:00"}, "übrige Einstellungen bleiben"
    text = aus.getvalue()
    assert "Beispielstadt" in text
    assert "· 2 von 4 TOPs mit Beratungsverweis ohne Vorlage" in text, "unter der Schwelle: Hinweis"
    assert "· Beratungen ausgesetzt (Zeitüberschreitung, Lücke ab 2026-09-27)" in text
    assert "Beispiele: " in text
    assert "0 kritisch, 0 auffällig, 1 ok (abgelegt)" in text


def test_report_legt_nichts_ab(quelle: OParlSource, kommune: OParlBody, bestand: dict[str, Any]) -> None:
    aus = StringIO()
    call_command("datenabgleich", "--report", stdout=aus)

    quelle.refresh_from_db()
    assert datenabgleich.SCHLUESSEL_INTERN not in quelle.sync_config
    assert "2 von 4 TOPs" in aus.getvalue(), "die Tabelle bewertet die eben ermittelten Befunde"


def test_kommune_filtert_und_behaelt_die_uebrigen(quelle: OParlSource, kommune: OParlBody) -> None:
    andere = "https://ris.beispiel.example/oparl/v1.1/body/2"
    _mit(quelle, datenabgleich={andere: _befund(name="Nachbarort")})

    call_command("datenabgleich", "--kommune", "beispielstadt", "--trotz-zeitplan", stdout=StringIO())

    quelle.refresh_from_db()
    assert set(quelle.sync_config[datenabgleich.SCHLUESSEL_INTERN]) == {BASIS, andere}

    call_command("datenabgleich", "--trotz-zeitplan", stdout=StringIO())
    quelle.refresh_from_db()
    assert set(quelle.sync_config[datenabgleich.SCHLUESSEL_INTERN]) == {BASIS}, "Lauf über alle ersetzt"


def test_zurueckgenommene_beratung_zaehlt_nicht_als_vorlage(kommune: OParlBody, bestand: dict[str, Any]) -> None:
    """Wie ``OParlAgendaItem.get_consultations``: Eine zurückgenommene Beratung versorgt den TOP nicht."""
    from insight_core.models import REASON_WITHDRAWN

    OParlConsultation.objects.filter(external_id=f"{BASIS}/consultation/1").update(
        deleted=True, deletion_reason=REASON_WITHDRAWN
    )

    befund = datenabgleich.pruefe_kommune(kommune)

    assert befund["work_ohne_vorlage"] == 3


@pytest.mark.parametrize(
    ("haupt", "weitere", "erwartet"),
    [
        ("", [{"id": "x"}], True),
        ("", "", False),
        (None, None, False),
        ('{"id": "x"}', None, True),
        ("https://ris.example/file/1", None, True),
        (None, "[]", False),
    ],
)
def test_nennt_dateien(haupt: Any, weitere: Any, erwartet: bool) -> None:
    assert datenabgleich._nennt_dateien(haupt, weitere) is erwartet


def test_gescheiterte_pruefung_behaelt_den_befund(
    quelle: OParlSource, kommune: OParlBody, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ein Fehler der Prüfung darf einen kritischen Befund nicht löschen; er bleibt als Warnung sichtbar."""
    _mit(quelle, datenabgleich={BASIS: _befund(top_luecke=37)})

    def kaputt(*_: Any, **__: Any) -> dict[str, Any]:
        raise RuntimeError("Abfrage gescheitert")

    monkeypatch.setattr(datenabgleich, "pruefe_kommune", kaputt)
    datenabgleich.pruefe_alle()

    quelle.refresh_from_db()
    eintrag = quelle.sync_config[datenabgleich.SCHLUESSEL_INTERN][BASIS]
    assert eintrag["top_luecke"] == 37
    assert eintrag["fehler"] == "RuntimeError"
    (bewertung,) = datenabgleich.bewerte_kommunen(quelle)
    assert bewertung.status == "critical"
    assert any(grund.startswith("interne Prüfung gescheitert (RuntimeError") for grund in bewertung.gruende)


def test_loeschmarkierungen_und_unsichere_dateien_alarmieren_nicht(quelle: OParlSource) -> None:
    _mit(
        quelle,
        datenabgleich_quelle={
            BASIS: {
                "at": timezone.now().isoformat(),
                "listen": {
                    "paper": {"quelle": 1000, "bestand": 700, "mit_geloeschten": 1000},
                    "file": {"quelle": 1000, "bestand": 100, "mit_geloeschten": 100, "nur_hinweis": True},
                },
            }
        },
    )

    (kommune,) = datenabgleich.bewerte_kommunen(quelle)

    assert kommune.status == "ok"
    assert any("Zuordnung zur Kommune unsicher" in hinweis for hinweis in kommune.hinweise)


def test_alter_fehlervermerk_ist_nur_ein_hinweis(quelle: OParlSource) -> None:
    alt = (timezone.now() - timedelta(days=3)).isoformat()
    _mit(quelle, datenabgleich={BASIS: _befund(at=alt, fehlgeschlagen=alt, fehler="RuntimeError")})

    (kommune,) = datenabgleich.bewerte_kommunen(quelle)

    assert kommune.status == "ok"
    assert any(hinweis.startswith("interne Prüfung gescheitert") for hinweis in kommune.hinweise)
