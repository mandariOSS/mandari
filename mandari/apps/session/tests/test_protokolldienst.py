# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Anbindung an einen Protokolldienst (docs/PROTOKOLLDIENST.md).

- Einstellungen: Adresse nur https und öffentlich (oder freigegeben), API-Schlüssel verschlüsselt, Rückgabeschlüssel
  nur als Hash.
- Cockpit → Postausgang: TOP-Aufruf, Unterbrechung und Ergebnis, nur öffentliche Angaben; der nichtöffentliche Teil
  und die Einwohnerfragestunde als Pause; Pausen überlappen nie; das Cockpit wartet nie auf den Dienst.
- Zustellung im Hintergrund gegen einen nachgebauten Dienst (httpx.MockTransport): Zuordnung über die OParl-Kennung,
  Kennungen der TOPs, Wiederholung, dauerhafte Fehler.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, cast
from unittest import mock

import httpx
import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.events.models import Task as TaskRow
from apps.events.models import TaskStatus
from apps.events.task_runner import run_pending
from apps.session.models import (
    SessionAgendaItem,
    SessionMeeting,
    SessionOrganization,
    SessionProtokolldienst,
    SessionProtokolldienstMeldung,
    SessionTenant,
)
from apps.session.protokolldienst import auftraege, client, einstellungen, meldungen
from apps.session.services import cockpit_service
from apps.session.tests._niederschrift import nutzer

pytestmark = pytest.mark.django_db

Meldung = SessionProtokolldienstMeldung
DIENST = "https://protokoll.example.org"
SCHLUESSEL = "mvk_geheimer-schluessel-des-dienstes"
DIENST_ID = "6c1f0e2a-1111-4222-8333-444455556666"
AUFGABE = "apps.session.protokolldienst.auftraege.zustellen"


@dataclass
class Welt:
    tenant: SessionTenant
    meeting: SessionMeeting
    tops: dict[str, SessionAgendaItem]


def _welt(slug: str = "pd", *, angebunden: bool = True, is_public: bool = True) -> Welt:
    tenant = SessionTenant.objects.create(name=f"Stadt {slug}", slug=slug)
    gremium = SessionOrganization.objects.create(tenant=tenant, name="Rat")
    meeting = SessionMeeting.objects.create(
        tenant=tenant, name="Ratssitzung", organization=gremium, start=timezone.now(), is_public=is_public
    )
    tops = {}
    for order, (nummer, public, kind) in enumerate(
        [("1", True, ""), ("2", True, ""), ("3", True, SessionAgendaItem.KIND_RESIDENTS_QUESTIONS), ("N1", False, "")]
    ):
        tops[nummer] = SessionAgendaItem.objects.create(
            meeting=meeting, number=nummer, name=f"TOP {nummer}", order=order, is_public=public, kind=kind
        )
    if angebunden:
        einstellungen.speichern(tenant, enabled=True, base_url=DIENST, api_key=SCHLUESSEL)
    return Welt(tenant, meeting, tops)


@pytest.fixture
def w() -> Welt:
    return _welt()


def _tun(w: Welt, aktion: str, **daten: Any) -> None:
    w.meeting.refresh_from_db()
    cockpit_service.perform(
        w.meeting,
        aktion,
        {key: str(value) for key, value in daten.items()},
        permissions={"view_meetings", "conduct_meetings", "view_non_public_meetings"},
        session_user=None,
    )


def _meldungen(w: Welt) -> list[tuple[str, dict[str, Any]]]:
    return [(m.kind, m.payload) for m in Meldung.objects.filter(meeting=w.meeting).order_by("occurred_at")]


# =============================================================================
# Einstellungen
# =============================================================================


@pytest.mark.parametrize(
    "adresse",
    [
        "http://protokoll.example.org",
        "https://localhost",
        "https://127.0.0.1",
        "https://10.0.0.5",
        "https://protokolldienst",
        "https://protokolldienst.localhost",
        "ftp://protokoll.example.org",
        "https://nutzer:pw@protokoll.example.org",
        "https://protokoll.example.org/?a=1",
        "",
    ],
)
def test_adresse_nur_https_und_oeffentlich(adresse: str) -> None:
    with pytest.raises(ValidationError):
        einstellungen.adresse_pruefen(adresse)


def test_adresse_ohne_schraegstrich_am_ende() -> None:
    assert einstellungen.adresse_pruefen(" https://protokoll.example.org/ ") == "https://protokoll.example.org"


def test_freigegebene_hosts_auch_im_eigenen_netz(settings: Any) -> None:
    settings.PROTOKOLLDIENST_ERLAUBTE_HOSTS = ["protokolldienst"]
    assert einstellungen.adresse_pruefen("https://protokolldienst:8443") == "https://protokolldienst:8443"
    with pytest.raises(ValidationError):
        einstellungen.adresse_pruefen("https://protokoll.example.org")


def test_http_nur_mit_ausdruecklicher_freigabe(settings: Any) -> None:
    """Die Hostfreigabe allein erlaubt kein http: Der API-Schlüssel ginge im Klartext über das Netz."""
    settings.PROTOKOLLDIENST_ERLAUBTE_HOSTS = ["protokolldienst"]
    with pytest.raises(ValidationError):
        einstellungen.adresse_pruefen("http://protokolldienst:8000")
    settings.PROTOKOLLDIENST_HTTP_ERLAUBT = True
    assert einstellungen.adresse_pruefen("http://protokolldienst:8000") == "http://protokolldienst:8000"
    # Nie für Hosts ohne Freigabe
    settings.PROTOKOLLDIENST_ERLAUBTE_HOSTS = []
    with pytest.raises(ValidationError):
        einstellungen.adresse_pruefen("http://protokoll.example.org")


def test_client_sendet_den_schluessel_nur_ueber_https(w: Welt, settings: Any) -> None:
    settings.PROTOKOLLDIENST_ERLAUBTE_HOSTS = ["protokolldienst"]
    SessionProtokolldienst.objects.filter(tenant=w.tenant).update(base_url="http://protokolldienst:8000")
    konfiguration = einstellungen.aktiv(w.tenant)
    assert konfiguration is not None
    with pytest.raises(client.ProtokolldienstError) as fehler:
        client.neuer_client(konfiguration)
    assert (fehler.value.code, fehler.value.dauerhaft) == ("nur_https", True)
    settings.PROTOKOLLDIENST_HTTP_ERLAUBT = True
    with client.neuer_client(konfiguration) as http:
        assert http.headers["Authorization"] == f"Bearer {SCHLUESSEL}"


def test_schluessel_verschluesselt_und_einschalten_nur_vollstaendig() -> None:
    tenant = SessionTenant.objects.create(name="Stadt", slug="schluessel")
    with pytest.raises(ValidationError):
        einstellungen.speichern(tenant, enabled=True, base_url=DIENST, api_key=None)
    konfiguration, aenderungen = einstellungen.speichern(tenant, enabled=True, base_url=DIENST, api_key=SCHLUESSEL)
    assert aenderungen == {"adresse": DIENST, "api_schluessel": "neu hinterlegt", "eingeschaltet": True}
    roh = bytes(
        cast(Any, SessionProtokolldienst.objects.values_list("api_key_encrypted", flat=True).get(tenant=tenant))
    )
    assert SCHLUESSEL.encode() not in roh
    assert einstellungen.api_schluessel(konfiguration) == SCHLUESSEL
    # Leeres Schlüsselfeld behält den gespeicherten
    konfiguration, aenderungen = einstellungen.speichern(tenant, enabled=True, base_url=DIENST, api_key=None)
    assert aenderungen == {} and einstellungen.api_schluessel(konfiguration) == SCHLUESSEL


def test_rueckgabeschluessel_nur_als_hash() -> None:
    w = _welt()
    konfiguration = einstellungen.aktiv(w.tenant)
    assert konfiguration is not None
    roh = einstellungen.rueckgabe_schluessel_erzeugen(konfiguration)
    konfiguration.refresh_from_db()
    assert roh not in (konfiguration.rueckgabe_token_hash, konfiguration.rueckgabe_token_prefix)
    assert konfiguration.rueckgabe_token_prefix == roh[:8]
    assert einstellungen.rueckgabe_schluessel_passt(konfiguration, roh)
    assert not einstellungen.rueckgabe_schluessel_passt(konfiguration, roh + "x")
    neu = einstellungen.rueckgabe_schluessel_erzeugen(konfiguration)
    assert not einstellungen.rueckgabe_schluessel_passt(konfiguration, roh)
    assert einstellungen.rueckgabe_schluessel_passt(konfiguration, neu)


def test_speichern_schreibt_den_rueckgabeschluessel_nie_aus_altem_stand() -> None:
    """Speichern mit einem Stand von vor dem neuen Rückgabeschlüssel macht den widerrufenen nicht wieder gültig."""
    w = _welt("gleichzeitig")
    alt = einstellungen.fuer_mandant(w.tenant)
    assert alt is not None
    widerrufen = einstellungen.rueckgabe_schluessel_erzeugen(alt)
    neu = einstellungen.rueckgabe_schluessel_erzeugen(cast(Any, einstellungen.fuer_mandant(w.tenant)))
    with mock.patch.object(einstellungen, "fuer_mandant", return_value=alt):
        einstellungen.speichern(w.tenant, enabled=False, base_url=DIENST, api_key=None)
    konfiguration = SessionProtokolldienst.objects.get(tenant=w.tenant)
    assert not konfiguration.enabled
    assert einstellungen.rueckgabe_schluessel_passt(konfiguration, neu)
    assert not einstellungen.rueckgabe_schluessel_passt(konfiguration, widerrufen)


def test_kennungen_der_oparl_schnittstelle(w: Welt) -> None:
    kennung = einstellungen.sitzung_kennung(w.meeting)
    assert kennung.endswith(f"/session/{w.tenant.slug}/api/oparl/meeting/{w.meeting.pk}/")
    assert einstellungen.kennung_aufloesen(kennung, "meeting") == w.meeting.pk
    assert einstellungen.kennung_aufloesen(kennung, "agendaitem") is None
    assert einstellungen.kennung_aufloesen(str(w.meeting.pk), "meeting") == w.meeting.pk
    top = einstellungen.top_kennung(w.tops["1"])
    assert einstellungen.kennung_aufloesen(top, "agendaitem") == w.tops["1"].pk


# =============================================================================
# Cockpit → Postausgang
# =============================================================================


def test_ohne_anbindung_keine_meldung() -> None:
    w = _welt("ohne", angebunden=False)
    _tun(w, "top_aufrufen", item=w.tops["1"].pk)
    assert not Meldung.objects.exists()


def test_ausgeschaltete_anbindung_meldet_nichts(w: Welt) -> None:
    SessionProtokolldienst.objects.filter(tenant=w.tenant).update(enabled=False)
    _tun(w, "top_aufrufen", item=w.tops["1"].pk)
    assert not Meldung.objects.exists()


def test_nichtoeffentliche_sitzung_meldet_nichts() -> None:
    w = _welt("noe-sitzung", is_public=False)
    _tun(w, "top_aufrufen", item=w.tops["1"].pk)
    _tun(w, "sitzung_unterbrechen")
    assert not Meldung.objects.exists()


def test_top_aufruf_meldet_nur_die_nummer(w: Welt) -> None:
    _tun(w, "top_aufrufen", item=w.tops["1"].pk)
    assert _meldungen(w) == [(Meldung.TOP_AUFGERUFEN, {"number": "1"})]


def test_nichtoeffentlicher_teil_nur_als_pause(w: Welt) -> None:
    _tun(w, "top_aufrufen", item=w.tops["1"].pk)
    _tun(w, "top_aufrufen", item=w.tops["N1"].pk)
    _tun(w, "top_beenden", item=w.tops["N1"].pk)
    _tun(w, "top_aufrufen", item=w.tops["2"].pk)
    assert _meldungen(w) == [
        (Meldung.TOP_AUFGERUFEN, {"number": "1"}),
        (Meldung.PAUSE_BEGONNEN, {"reason": "nichtoeffentlich"}),
        (Meldung.PAUSE_BEENDET, {"reason": "nichtoeffentlich"}),
        (Meldung.TOP_AUFGERUFEN, {"number": "2"}),
    ]
    assert "N1" not in json.dumps([p for _, p in _meldungen(w)])


def test_einwohnerfragestunde_mit_zeitmarke_und_pause(w: Welt) -> None:
    _tun(w, "top_aufrufen", item=w.tops["3"].pk)
    _tun(w, "top_aufrufen", item=w.tops["1"].pk)
    assert _meldungen(w) == [
        (Meldung.TOP_AUFGERUFEN, {"number": "3"}),
        (Meldung.PAUSE_BEGONNEN, {"reason": "einwohnerfragestunde"}),
        (Meldung.PAUSE_BEENDET, {"reason": "einwohnerfragestunde"}),
        (Meldung.TOP_AUFGERUFEN, {"number": "1"}),
    ]


def test_unterbrechung_als_pause(w: Welt) -> None:
    _tun(w, "top_aufrufen", item=w.tops["1"].pk)
    _tun(w, "sitzung_unterbrechen")
    w.meeting.refresh_from_db()
    assert w.meeting.interrupted_at is not None
    _tun(w, "unterbrechung_beenden")
    w.meeting.refresh_from_db()
    assert w.meeting.interrupted_at is None
    assert _meldungen(w)[1:] == [
        (Meldung.PAUSE_BEGONNEN, {"reason": "unterbrechung"}),
        (Meldung.PAUSE_BEENDET, {"reason": "unterbrechung"}),
    ]


def test_pausen_ueberlappen_nie(w: Welt) -> None:
    """Unterbrechung im nichtöffentlichen Teil: eine Pause, die erst mit dem nächsten öffentlichen TOP endet."""
    _tun(w, "top_aufrufen", item=w.tops["N1"].pk)
    _tun(w, "sitzung_unterbrechen")
    _tun(w, "unterbrechung_beenden")
    assert _meldungen(w) == [(Meldung.PAUSE_BEGONNEN, {"reason": "nichtoeffentlich"})]
    _tun(w, "top_aufrufen", item=w.tops["2"].pk)
    assert [kind for kind, _ in _meldungen(w)] == [
        Meldung.PAUSE_BEGONNEN,
        Meldung.PAUSE_BEENDET,
        Meldung.TOP_AUFGERUFEN,
    ]


def test_nichtoeffentlicher_top_waehrend_der_unterbrechung_bleibt_geschuetzt(w: Welt) -> None:
    """
    Unterbrechung, dann ein nichtöffentlicher TOP (keine neue Meldung, die Pause ist offen): Eine weitere
    Unterbrechung samt Fortsetzung beendet die Pause nicht, auch nicht nach dem Ende des TOP.
    """
    _tun(w, "top_aufrufen", item=w.tops["1"].pk)
    _tun(w, "sitzung_unterbrechen")
    _tun(w, "top_aufrufen", item=w.tops["N1"].pk)
    _tun(w, "sitzung_unterbrechen")
    _tun(w, "unterbrechung_beenden")
    _tun(w, "top_beenden", item=w.tops["N1"].pk)
    _tun(w, "sitzung_unterbrechen")
    _tun(w, "unterbrechung_beenden")
    assert [kind for kind, _ in _meldungen(w)] == [Meldung.TOP_AUFGERUFEN, Meldung.PAUSE_BEGONNEN]
    _tun(w, "top_aufrufen", item=w.tops["2"].pk)
    assert [kind for kind, _ in _meldungen(w)] == [
        Meldung.TOP_AUFGERUFEN,
        Meldung.PAUSE_BEGONNEN,
        Meldung.PAUSE_BEENDET,
        Meldung.TOP_AUFGERUFEN,
    ]


def test_aufruf_beendet_die_unterbrechung(w: Welt) -> None:
    _tun(w, "top_aufrufen", item=w.tops["1"].pk)
    _tun(w, "sitzung_unterbrechen")
    _tun(w, "top_aufrufen", item=w.tops["2"].pk)
    w.meeting.refresh_from_db()
    assert w.meeting.interrupted_at is None
    assert [kind for kind, _ in _meldungen(w)] == [
        Meldung.TOP_AUFGERUFEN,
        Meldung.PAUSE_BEGONNEN,
        Meldung.PAUSE_BEENDET,
        Meldung.TOP_AUFGERUFEN,
    ]


def test_unterbrechung_nur_in_der_laufenden_sitzung_und_ohne_abstimmung(w: Welt) -> None:
    with pytest.raises(cockpit_service.CockpitError):
        _tun(w, "sitzung_unterbrechen")
    _tun(w, "top_aufrufen", item=w.tops["1"].pk)
    _tun(w, "sitzung_unterbrechen")
    with pytest.raises(cockpit_service.CockpitError):
        _tun(w, "sitzung_unterbrechen")
    with pytest.raises(cockpit_service.CockpitError):
        _tun(w, "abstimmung_oeffnen", item=w.tops["1"].pk)
    _tun(w, "sitzung_schliessen")
    w.meeting.refresh_from_db()
    assert w.meeting.interrupted_at is None


def test_abstimmung_nur_summen(w: Welt) -> None:
    _tun(w, "top_aufrufen", item=w.tops["2"].pk)
    _tun(w, "abstimmung_oeffnen", item=w.tops["2"].pk, voting_method="summary")
    _tun(w, "abstimmung_schliessen", item=w.tops["2"].pk, vote_result="approved", votes_yes=5, votes_no=0)
    kind, payload = _meldungen(w)[-1]
    assert kind == Meldung.ABSTIMMUNG
    assert payload == {"number": "2", "outcome": "adopted", "yes": 5, "no": 0, "abstain": 0, "unanimous": True}


def test_vertagen_meldet_das_ergebnis(w: Welt) -> None:
    _tun(w, "top_vertagen", item=w.tops["2"].pk)
    assert _meldungen(w) == [(Meldung.ABSTIMMUNG, {"number": "2", "outcome": "postponed"})]


def test_ergebnis_im_nichtoeffentlichen_teil_bleibt_intern(w: Welt) -> None:
    _tun(w, "top_aufrufen", item=w.tops["N1"].pk)
    _tun(w, "abstimmung_oeffnen", item=w.tops["N1"].pk, voting_method="summary")
    _tun(w, "abstimmung_schliessen", item=w.tops["N1"].pk, vote_result="rejected", votes_yes=1, votes_no=4)
    assert [kind for kind, _ in _meldungen(w)] == [Meldung.PAUSE_BEGONNEN]


def test_zustellung_nach_dem_commit_im_journal(w: Welt, django_capture_on_commit_callbacks: Any) -> None:
    """Das Cockpit wartet nie auf den Dienst: Nach dem Commit liegt nur ein Auftrag im Journal."""
    with mock.patch.object(client, "neuer_client") as aufruf:
        with django_capture_on_commit_callbacks(execute=True):
            _tun(w, "top_aufrufen", item=w.tops["1"].pk)
        aufruf.assert_not_called()
    auftrag = TaskRow.objects.get(task_path=AUFGABE)
    assert auftrag.args["args"] == [str(w.meeting.pk)] and set(auftrag.args["kwargs"]) == {"eingereiht"}
    assert auftrag.queue == "adapter" and auftrag.max_attempts == 12


# =============================================================================
# Zustellung gegen einen nachgebauten Dienst
# =============================================================================


@dataclass
class Dienst:
    """Nachgebaute Ereignis-Schnittstelle: Sitzung unter ihrer OParl-Kennung, TOPs 1 und 2."""

    oparl_id: str
    status: int = 200
    kennt_sitzung: bool = True
    anfragen: list[httpx.Request] = field(default_factory=list)
    ereignisse: list[dict[str, Any]] = field(default_factory=list)
    abgelehnt: set[int] = field(default_factory=set)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.anfragen.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": "nein"})
        pfad = request.url.path
        if request.method == "GET" and pfad == "/api/v1/meetings":
            liste = [{"id": "andere", "oparl_id": "https://andere.example.org/meeting/1/"}]
            if self.kennt_sitzung:
                liste.append({"id": DIENST_ID, "oparl_id": self.oparl_id.rstrip("/")})
            return httpx.Response(200, json=liste)
        if request.method == "GET" and pfad == f"/api/v1/meetings/{DIENST_ID}":
            return httpx.Response(200, json={"id": DIENST_ID, "agenda_items": [{"id": "dienst-top-1", "number": "1"}]})
        if request.method == "POST" and pfad == f"/api/v1/meetings/{DIENST_ID}/events":
            events = json.loads(request.content)["events"]
            self.ereignisse.extend(events)
            return httpx.Response(
                200,
                json=[
                    {
                        "index": i,
                        "id": None if i in self.abgelehnt else i + 1,
                        "created": i not in self.abgelehnt,
                        "error": "TOP gehört nicht zu dieser Sitzung." if i in self.abgelehnt else None,
                    }
                    for i in range(len(events))
                ],
            )
        return httpx.Response(404)


@pytest.fixture
def dienst(w: Welt, monkeypatch: pytest.MonkeyPatch) -> Iterator[Dienst]:
    nachbau = Dienst(oparl_id=einstellungen.sitzung_kennung(w.meeting))

    def neuer_client(konfiguration: SessionProtokolldienst) -> httpx.Client:
        return httpx.Client(
            transport=httpx.MockTransport(nachbau),
            headers={"Authorization": f"Bearer {einstellungen.api_schluessel(konfiguration)}"},
        )

    monkeypatch.setattr(client, "neuer_client", neuer_client)
    yield nachbau


def _zustellen(w: Welt, capture: Callable[..., Any], *aktionen: tuple[str, dict[str, Any]]) -> int:
    with capture(execute=True):
        for aktion, daten in aktionen:
            _tun(w, aktion, **daten)
    return run_pending(["adapter"])


def test_zustellung_ordnet_sitzung_und_tops_zu(
    w: Welt, dienst: Dienst, django_capture_on_commit_callbacks: Any
) -> None:
    _zustellen(
        w,
        django_capture_on_commit_callbacks,
        ("top_aufrufen", {"item": w.tops["1"].pk}),
        ("top_vertagen", {"item": w.tops["1"].pk}),
    )
    assert all(r.headers["Authorization"] == f"Bearer {SCHLUESSEL}" for r in dienst.anfragen)
    assert [e["type"] for e in dienst.ereignisse] == ["agenda_item_changed", "vote_result"]
    assert dienst.ereignisse[0]["payload"] == {"number": "1", "agenda_item": "dienst-top-1"}
    assert dienst.ereignisse[1]["payload"] == {"outcome": "postponed", "agenda_item": "dienst-top-1"}
    assert all(e["source"] == "session" and e["external_id"].startswith("mandari-session:") for e in dienst.ereignisse)
    assert set(Meldung.objects.values_list("status", flat=True)) == {Meldung.ZUGESTELLT}
    konfiguration = SessionProtokolldienst.objects.get(tenant=w.tenant)
    assert konfiguration.last_delivery_at is not None and konfiguration.last_error == ""


def test_wiederholung_bis_der_dienst_die_sitzung_kennt(
    w: Welt, dienst: Dienst, django_capture_on_commit_callbacks: Any
) -> None:
    dienst.kennt_sitzung = False
    _zustellen(w, django_capture_on_commit_callbacks, ("top_aufrufen", {"item": w.tops["2"].pk}))
    zeile = Meldung.objects.get()
    assert (zeile.status, zeile.error_code, zeile.attempts) == (Meldung.WARTEND, "sitzung_unbekannt", 1)
    assert TaskRow.objects.get(task_path=AUFGABE).status == TaskStatus.WARTEND

    dienst.kennt_sitzung = True
    TaskRow.objects.update(run_after=timezone.now() - timedelta(minutes=1))
    assert run_pending(["adapter"]) == 1
    zeile.refresh_from_db()
    assert zeile.status == Meldung.ZUGESTELLT
    # TOP 2 kennt der Dienst nicht: der Wechsel geht mit der Nummer
    assert dienst.ereignisse[-1]["payload"] == {"number": "2"}


def test_abgelehnter_schluessel_ist_endgueltig(
    w: Welt, dienst: Dienst, django_capture_on_commit_callbacks: Any
) -> None:
    dienst.status = 401
    _zustellen(w, django_capture_on_commit_callbacks, ("top_aufrufen", {"item": w.tops["1"].pk}))
    assert Meldung.objects.get().status == Meldung.FEHLGESCHLAGEN
    assert TaskRow.objects.get(task_path=AUFGABE).status == TaskStatus.FEHLGESCHLAGEN
    assert SessionProtokolldienst.objects.get(tenant=w.tenant).last_error == "anmeldung"


def test_ergebnis_ohne_top_beim_dienst(w: Welt, dienst: Dienst, django_capture_on_commit_callbacks: Any) -> None:
    _zustellen(w, django_capture_on_commit_callbacks, ("top_vertagen", {"item": w.tops["2"].pk}))
    zeile = Meldung.objects.get()
    assert (zeile.status, zeile.error_code) == (Meldung.FEHLGESCHLAGEN, "top_unbekannt")
    assert not dienst.ereignisse


def test_abgewiesene_meldung(w: Welt, dienst: Dienst, django_capture_on_commit_callbacks: Any) -> None:
    dienst.abgelehnt = {0}
    _zustellen(w, django_capture_on_commit_callbacks, ("top_aufrufen", {"item": w.tops["1"].pk}))
    zeile = Meldung.objects.get()
    assert (zeile.status, zeile.error_code) == (Meldung.FEHLGESCHLAGEN, "abgelehnt")


def test_nach_dem_letzten_versuch_fehlgeschlagen(
    w: Welt, dienst: Dienst, django_capture_on_commit_callbacks: Any, settings: Any
) -> None:
    optionen = dict(settings.TASKS["default"]["OPTIONS"])
    optionen["tasks"] = {**optionen.get("tasks", {}), AUFGABE: {"max_attempts": 1}}
    settings.TASKS = {"default": {**settings.TASKS["default"], "OPTIONS": optionen}}
    dienst.status = 503
    _zustellen(w, django_capture_on_commit_callbacks, ("top_aufrufen", {"item": w.tops["1"].pk}))
    zeile = Meldung.objects.get()
    assert (zeile.status, zeile.error_code) == (Meldung.FEHLGESCHLAGEN, "dienstfehler")


def test_letzter_versuch_schliesst_nur_die_eigenen_meldungen_ab(
    w: Welt, dienst: Dienst, django_capture_on_commit_callbacks: Any, settings: Any
) -> None:
    """Eine Meldung, die nach dem Einreihen des Auftrags entstand, hat ihren eigenen Auftrag und bleibt wartend."""
    optionen = dict(settings.TASKS["default"]["OPTIONS"])
    optionen["tasks"] = {**optionen.get("tasks", {}), AUFGABE: {"max_attempts": 1}}
    settings.TASKS = {"default": {**settings.TASKS["default"], "OPTIONS": optionen}}
    dienst.status = 503
    with django_capture_on_commit_callbacks(execute=True):
        _tun(w, "top_aufrufen", item=w.tops["1"].pk)
    eingereiht = parse_datetime(TaskRow.objects.get(task_path=AUFGABE).args["kwargs"]["eingereiht"])
    assert eingereiht is not None
    # Ohne Ausführen der Commit-Rückrufe: Der eigene Auftrag der neuen Meldung käme erst danach
    _tun(w, "top_aufrufen", item=w.tops["2"].pk)
    Meldung.objects.filter(agenda_item=w.tops["2"]).update(created_at=eingereiht + timedelta(seconds=1))
    run_pending(["adapter"])
    alt = Meldung.objects.get(agenda_item=w.tops["1"])
    neu = Meldung.objects.get(agenda_item=w.tops["2"])
    assert (alt.status, alt.error_code) == (Meldung.FEHLGESCHLAGEN, "dienstfehler")
    assert (neu.status, neu.error_code, neu.attempts) == (Meldung.WARTEND, "dienstfehler", 1)


def test_ausgeschaltet_vor_der_zustellung(w: Welt, dienst: Dienst, django_capture_on_commit_callbacks: Any) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        _tun(w, "top_aufrufen", item=w.tops["1"].pk)
    SessionProtokolldienst.objects.filter(tenant=w.tenant).update(enabled=False)
    run_pending(["adapter"])
    assert Meldung.objects.get().error_code == "ausgeschaltet"
    assert not dienst.anfragen


def test_client_verbindet_nur_mit_geprueften_zielen(w: Welt, settings: Any) -> None:
    from insight_core.services.safe_fetch import StrictPinnedTransport

    konfiguration = einstellungen.aktiv(w.tenant)
    assert konfiguration is not None
    with client.neuer_client(konfiguration) as http:
        assert isinstance(http._transport, StrictPinnedTransport)
        assert http.headers["Authorization"] == f"Bearer {SCHLUESSEL}"
        assert not http.follow_redirects
    settings.PROTOKOLLDIENST_ERLAUBTE_HOSTS = ["protokoll.example.org"]
    with client.neuer_client(konfiguration) as http:
        assert not isinstance(http._transport, StrictPinnedTransport)


def test_grosse_antwort_wird_beim_lesen_abgebrochen() -> None:
    """Die Grenze gilt beim Lesen: Nach gut ``MAX_ANTWORT`` Bytes bricht der Client ab und liest nicht weiter."""
    stueck, gelesen = 64 * 1024, []

    class Strom(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            for _ in range(4 * client.MAX_ANTWORT // stueck):
                gelesen.append(stueck)
                yield b"x" * stueck

    http = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Strom())))
    with pytest.raises(client.ProtokolldienstError) as fehler:
        client._aufruf(http, "GET", f"{DIENST}/api/v1/meetings")
    assert (fehler.value.code, fehler.value.dauerhaft) == ("abgelehnt", True)
    assert sum(gelesen) <= client.MAX_ANTWORT + stueck


@pytest.mark.parametrize(
    ("status", "code", "dauerhaft"),
    [
        (401, "anmeldung", True),
        (403, "anmeldung", True),
        (422, "abgelehnt", True),
        (429, "ueberlastet", False),
        (502, "dienstfehler", False),
        (404, "sitzung_unbekannt", False),
    ],
)
def test_fehlercodes(status: int, code: str, dauerhaft: bool) -> None:
    fehler = client._fehler_aus_status(status)
    assert (fehler.code, fehler.dauerhaft) == (code, dauerhaft)


def test_meldungen_ohne_namen_und_texte(w: Welt) -> None:
    """Gegenprobe: In keiner Nutzlast stehen Name eines TOP, Personen oder Einzelstimmen."""
    meldungen.top_aufgerufen(w.meeting, w.tops["1"])
    w.tops["2"].vote_result = "approved"
    w.tops["2"].votes_yes = 3
    w.tops["2"].save()
    meldungen.abstimmung(w.meeting, w.tops["2"])
    erlaubt = {"number", "outcome", "yes", "no", "abstain", "unanimous", "reason"}
    assert all(set(payload) <= erlaubt for _, payload in _meldungen(w))
    assert "TOP 1" not in json.dumps([p for _, p in _meldungen(w)])


def test_zugestellte_meldungen_werden_aufgeraeumt(
    w: Welt, dienst: Dienst, django_capture_on_commit_callbacks: Any
) -> None:
    alt = Meldung.objects.create(
        meeting=w.meeting,
        kind=Meldung.TOP_AUFGERUFEN,
        occurred_at=timezone.now() - auftraege.AUFBEWAHRUNG,
        payload={"number": "1"},
        status=Meldung.ZUGESTELLT,
        delivered_at=timezone.now() - auftraege.AUFBEWAHRUNG * 2,
    )
    _zustellen(w, django_capture_on_commit_callbacks, ("top_aufrufen", {"item": w.tops["1"].pk}))
    assert not Meldung.objects.filter(pk=alt.pk).exists()


def test_nutzer_ohne_steuerungsrecht_unterbricht_nicht(w: Welt) -> None:
    leser = nutzer(w.tenant, "leser", "view_meetings")
    with pytest.raises(cockpit_service.CockpitError):
        cockpit_service.perform(
            w.meeting, "sitzung_unterbrechen", {}, permissions={"view_meetings"}, session_user=leser
        )
