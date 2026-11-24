# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rückweg des Protokolldienstes (docs/PROTOKOLLDIENST.md): Entwürfe je TOP als Vorschlag für die Niederschrift.

- Schnittstelle ``/api/mandari/v1/protokolldienst/{tenant_slug}/``: nur mit dem Rückgabeschlüssel des Mandanten, nur
  für dessen Sitzungen; Zuordnung über OParl-Kennung oder TOP-Nummer; Wiederholung derselben Fassung ohne Doppel.
- Vorschläge ändern die Niederschrift nicht; übernommen wird auf Wunsch der Protokollführung, mit denselben Regeln
  wie beim Bearbeiten (Ergebnis nur, solange keins festgestellt ist; nichtöffentliche TOPs in die NÖ-Felder).
- Einstellungsseite und Niederschrift-Seite in beiden Rahmen.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest
from django.test import Client
from django.utils import timezone

from apps.session.models import (
    SessionAgendaItem,
    SessionAuditLog,
    SessionMeeting,
    SessionNiederschriftVorschlag,
    SessionOrganization,
    SessionProtocol,
    SessionProtokolldienst,
    SessionTenant,
)
from apps.session.protokolldienst import einstellungen, vorschlaege
from apps.session.tests._niederschrift import client, nutzer

pytestmark = pytest.mark.django_db

Vorschlag = SessionNiederschriftVorschlag
DOKUMENT = "0b8f4c1e-2d3a-4e5f-9a6b-7c8d9e0f1a2b"
ENTWURF = "Die Verwaltung erläutert die Vorlage ENTWURFSTEXT."
NOE_ENTWURF = "GEHEIMENTWURF zum nichtöffentlichen Punkt"


class Welt:
    def __init__(self, slug: str = "rueck", *, angebunden: bool = True) -> None:
        self.tenant = SessionTenant.objects.create(name=f"Stadt {slug}", slug=slug)
        gremium = SessionOrganization.objects.create(tenant=self.tenant, name="Rat")
        self.meeting = SessionMeeting.objects.create(
            tenant=self.tenant, name="Ratssitzung", organization=gremium, start=timezone.now(), is_public=True
        )
        self.top = SessionAgendaItem.objects.create(meeting=self.meeting, number="5", name="Radweg", order=1)
        self.top_noe = SessionAgendaItem.objects.create(
            meeting=self.meeting, number="N1", name="Grundstück", order=2, is_public=False
        )
        self.schluessel = ""
        if angebunden:
            konfiguration, _ = einstellungen.speichern(
                self.tenant, enabled=True, base_url="https://protokoll.example.org", api_key="mvk_x"
            )
            self.schluessel = einstellungen.rueckgabe_schluessel_erzeugen(konfiguration)

    @property
    def url(self) -> str:
        return f"/api/mandari/v1/protokolldienst/{self.tenant.slug}/niederschrift/vorschlaege/"

    def body(self, **anders: Any) -> dict[str, Any]:
        daten: dict[str, Any] = {
            "meeting": einstellungen.sitzung_kennung(self.meeting),
            "source_document": DOKUMENT,
            "revision": 1,
            "released_at": "2026-11-24T10:15:00+01:00",
            "items": [
                {
                    "agenda_item": einstellungen.top_kennung(self.top),
                    "draft_text": ENTWURF,
                    "resolution_text": "Der Rat beschließt den Radweg.",
                    "vote": {"yes": 3, "no": 1, "abstain": 0, "outcome": "adopted"},
                },
                {"number": "N1", "draft_text": NOE_ENTWURF},
                {"number": "99", "draft_text": "gibt es nicht"},
            ],
        }
        daten.update(anders)
        return daten

    def senden(self, body: dict[str, Any] | None = None, *, schluessel: str | None = None, **kopf: str) -> Any:
        return Client().post(
            self.url,
            body if body is not None else self.body(),
            content_type="application/json",
            headers={"Authorization": f"Bearer {self.schluessel if schluessel is None else schluessel}", **kopf},
        )


@pytest.fixture
def w() -> Welt:
    return Welt()


# =============================================================================
# Schnittstelle
# =============================================================================


def test_vorschlaege_werden_angenommen(w: Welt) -> None:
    antwort = w.senden()
    assert antwort.status_code == 201, antwort.content
    daten = antwort.json()
    assert daten["accepted"] == 2 and daten["unassigned"] == [2]
    assert daten["meeting"] == einstellungen.sitzung_kennung(w.meeting)
    vorschlag = Vorschlag.objects.get(agenda_item=w.top)
    assert (vorschlag.status, vorschlag.revision, vorschlag.vote_result) == (Vorschlag.OFFEN, 1, "approved")
    assert (vorschlag.votes_yes, vorschlag.votes_no, vorschlag.votes_abstain) == (3, 1, 0)
    # Entwürfe verschlüsselt, die Niederschrift unverändert
    assert ENTWURF.encode() not in bytes(cast(Any, vorschlag.draft_text_encrypted))
    w.top.refresh_from_db()
    assert w.top.protocol_note == "" and w.top.vote_result == "pending"


def test_wiederholung_derselben_fassung_ohne_doppel(w: Welt) -> None:
    erste = w.senden().json()
    zweite = w.senden().json()
    assert erste["reference"] == zweite["reference"] and erste["content_hash"] == zweite["content_hash"]
    assert Vorschlag.objects.count() == 2
    # Dieselbe Fassung mit anderem Inhalt: abgelehnt
    anders = w.body()
    anders["items"][0]["draft_text"] = "anders"
    assert w.senden(anders).status_code == 422


def test_neuere_fassung_ersetzt_offene_vorschlaege(w: Welt) -> None:
    w.senden()
    assert w.senden(w.body(revision=2), **{"Idempotency-Key": "fassung-2"}).status_code == 201
    assert list(Vorschlag.objects.filter(agenda_item=w.top).values_list("revision", "status").order_by("revision")) == [
        (1, Vorschlag.ERSETZT),
        (2, Vorschlag.OFFEN),
    ]
    ersetzt = Vorschlag.objects.get(agenda_item=w.top, revision=1)
    assert ersetzt.draft_text_encrypted is None, "Texte ersetzter Vorschläge werden gelöscht"


@pytest.mark.parametrize("schluessel", ["", "falsch", "x" * 300])
def test_ohne_gueltigen_schluessel_401(w: Welt, schluessel: str) -> None:
    antwort = w.senden(schluessel=schluessel)
    assert antwort.status_code == 401
    assert antwort["Content-Type"].startswith("application/problem+json")
    assert "WWW-Authenticate" in antwort
    assert not Vorschlag.objects.exists()


def test_ausgeschaltete_anbindung_401(w: Welt) -> None:
    SessionProtokolldienst.objects.filter(tenant=w.tenant).update(enabled=False)
    assert w.senden().status_code == 401


def test_schluessel_gilt_nur_fuer_den_eigenen_mandanten(w: Welt) -> None:
    fremd = Welt("fremd")
    # Schlüssel des einen Mandanten an der Adresse des anderen
    assert fremd.senden(schluessel=w.schluessel).status_code == 401
    # Sitzung eines anderen Mandanten mit dem eigenen Schlüssel: unbekannt
    body = w.body(meeting=einstellungen.sitzung_kennung(fremd.meeting))
    assert w.senden(body).status_code == 404
    assert not Vorschlag.objects.exists()


def test_genehmigte_niederschrift_409(w: Welt) -> None:
    SessionProtocol.objects.create(meeting=w.meeting, status="approved")
    antwort = w.senden()
    assert antwort.status_code == 409 and antwort.json()["type"].endswith("niederschrift-genehmigt")


def test_kein_top_zuzuordnen_422(w: Welt) -> None:
    antwort = w.senden(w.body(items=[{"number": "99", "draft_text": "x"}]))
    assert antwort.status_code == 422 and antwort.json()["unassigned"] == [0]


def test_einstieg_prueft_den_schluessel(w: Welt) -> None:
    kopf = {"Authorization": f"Bearer {w.schluessel}"}
    antwort = Client().get(f"/api/mandari/v1/protokolldienst/{w.tenant.slug}/", headers=kopf)
    assert antwort.status_code == 200
    assert antwort.json()["vorschlaege"].endswith(w.url)
    assert Client().get(f"/api/mandari/v1/protokolldienst/{w.tenant.slug}/").status_code == 401


# =============================================================================
# Übernehmen und Verwerfen
# =============================================================================

RECHTE = {"view_meetings", "view_protocols", "edit_protocols", "view_non_public_meetings"}


def _vorschlag(w: Welt, item: SessionAgendaItem) -> SessionNiederschriftVorschlag:
    w.senden()
    return Vorschlag.objects.get(agenda_item=item, status=Vorschlag.OFFEN)


def test_uebernehmen_schreibt_texte_und_ergebnis(w: Welt) -> None:
    vorschlag = _vorschlag(w, w.top)
    hinweise = vorschlaege.uebernehmen(w.meeting, vorschlag.pk, permissions=RECHTE, session_user=None)
    assert hinweise == []
    w.top.refresh_from_db()
    assert w.top.protocol_note == ENTWURF
    assert w.top.resolution_text == "Der Rat beschließt den Radweg."
    assert (w.top.vote_result, w.top.votes_yes, w.top.votes_no, w.top.votes_abstain) == ("approved", 3, 1, 0)
    vorschlag.refresh_from_db()
    assert vorschlag.status == Vorschlag.UEBERNOMMEN and vorschlag.decided_at is not None
    assert vorschlag.draft_text_encrypted is None and vorschlag.resolution_text_encrypted is None
    eintrag = SessionAuditLog.objects.filter(tenant=w.tenant, object_id=w.top.pk).latest("created_at")
    assert "übernommen" in str(eintrag.changes) and ENTWURF not in str(eintrag.changes)
    # Eine Niederschrift im Entwurf ist dabei entstanden
    assert SessionProtocol.objects.get(meeting=w.meeting).status == "draft"


def test_festgestelltes_ergebnis_bleibt(w: Welt) -> None:
    SessionAgendaItem.objects.filter(pk=w.top.pk).update(vote_result="rejected", votes_yes=1, votes_no=4)
    vorschlag = _vorschlag(w, w.top)
    hinweise = vorschlaege.uebernehmen(w.meeting, vorschlag.pk, permissions=RECHTE, session_user=None)
    assert any("schon festgestellt" in hinweis for hinweis in hinweise)
    w.top.refresh_from_db()
    assert (w.top.vote_result, w.top.votes_yes, w.top.protocol_note) == ("rejected", 1, ENTWURF)


def test_nichtoeffentlicher_top_in_die_noe_felder(w: Welt) -> None:
    vorschlag = _vorschlag(w, w.top_noe)
    vorschlaege.uebernehmen(w.meeting, vorschlag.pk, permissions=RECHTE, session_user=None)
    w.top_noe.refresh_from_db()
    assert w.top_noe.protocol_note == ""
    assert cast(Any, w.top_noe).get_protocol_note_decrypted() == NOE_ENTWURF


def test_ohne_noe_recht_kein_nichtoeffentlicher_vorschlag(w: Welt) -> None:
    vorschlag = _vorschlag(w, w.top_noe)
    ohne = RECHTE - {"view_non_public_meetings"}
    with pytest.raises(vorschlaege.VorschlagError):
        vorschlaege.uebernehmen(w.meeting, vorschlag.pk, permissions=ohne, session_user=None)
    anzeige = vorschlaege.offene(w.meeting, can_view_np=False)
    assert [a.item.number for a in anzeige] == ["5"]


def test_verwerfen_aendert_die_niederschrift_nicht(w: Welt) -> None:
    vorschlag = _vorschlag(w, w.top)
    vorschlaege.verwerfen(w.meeting, vorschlag.pk, permissions=RECHTE, session_user=None)
    vorschlag.refresh_from_db()
    assert vorschlag.status == Vorschlag.VERWORFEN
    w.top.refresh_from_db()
    assert w.top.protocol_note == "" and w.top.vote_result == "pending"
    with pytest.raises(vorschlaege.VorschlagError):
        vorschlaege.uebernehmen(w.meeting, vorschlag.pk, permissions=RECHTE, session_user=None)


def test_nach_der_genehmigung_nicht_mehr(w: Welt) -> None:
    vorschlag = _vorschlag(w, w.top)
    SessionProtocol.objects.create(meeting=w.meeting, status="approved")
    with pytest.raises(vorschlaege.VorschlagError):
        vorschlaege.uebernehmen(w.meeting, vorschlag.pk, permissions=RECHTE, session_user=None)


# =============================================================================
# Seiten
# =============================================================================


@pytest.mark.parametrize("neu", [False, True])
def test_niederschrift_zeigt_vorschlaege_und_uebernimmt(w: Welt, neu: bool) -> None:
    SessionTenant.objects.filter(pk=w.tenant.pk).update(session_new_design=neu)
    w.senden()
    SessionProtocol.objects.create(meeting=w.meeting, status="draft")
    protokoll = nutzer(w.tenant, "protokoll", "view_meetings", "view_protocols", "edit_protocols")
    c = client(protokoll)
    seite = c.get(f"/session/{w.tenant.slug}/meetings/{w.meeting.pk}/protocol/")
    html = seite.content.decode()
    assert seite.status_code == 200
    assert "Vorschläge des Protokolldienstes" in html and "ENTWURFSTEXT" in html
    assert "GEHEIMENTWURF" not in html, "nichtöffentlicher Vorschlag nur mit NÖ-Recht"
    assert 'data-ai-generated="true"' in html
    vorschlag = Vorschlag.objects.get(agenda_item=w.top)
    antwort = c.post(
        f"/session/{w.tenant.slug}/meetings/{w.meeting.pk}/protocol/vorschlaege/{vorschlag.pk}/uebernehmen/"
    )
    assert antwort.status_code == 302
    w.top.refresh_from_db()
    assert w.top.protocol_note == ENTWURF


def test_ohne_bearbeitungsrecht_keine_entscheidung(w: Welt) -> None:
    w.senden()
    leser = nutzer(w.tenant, "leser", "view_meetings", "view_protocols")
    vorschlag = Vorschlag.objects.get(agenda_item=w.top)
    antwort = client(leser).post(
        f"/session/{w.tenant.slug}/meetings/{w.meeting.pk}/protocol/vorschlaege/{vorschlag.pk}/uebernehmen/"
    )
    assert antwort.status_code in (302, 403)
    vorschlag.refresh_from_db()
    assert vorschlag.status == Vorschlag.OFFEN


@pytest.mark.parametrize("neu", [False, True])
def test_einstellungsseite(neu: bool) -> None:
    w = Welt(f"einst-{int(neu)}", angebunden=False)
    SessionTenant.objects.filter(pk=w.tenant.pk).update(session_new_design=neu)
    verwaltung = nutzer(w.tenant, "verwaltung", "manage_settings")
    c = client(verwaltung)
    adresse = f"/session/{w.tenant.slug}/settings/protokolldienst/"
    assert c.get(adresse).status_code == 200
    # Adresse im eigenen Netz: abgewiesen, nichts gespeichert
    c.post(adresse, {"aktion": "speichern", "base_url": "https://10.0.0.1", "api_key": "k", "enabled": "on"})
    assert not SessionProtokolldienst.objects.filter(tenant=w.tenant, base_url__contains="10.0.0.1").exists()
    c.post(adresse, {"aktion": "speichern", "base_url": "https://protokoll.example.org", "api_key": "geheim"})
    konfiguration = SessionProtokolldienst.objects.get(tenant=w.tenant)
    assert konfiguration.base_url == "https://protokoll.example.org" and not konfiguration.enabled
    c.post(adresse, {"aktion": "schluessel"})
    seite = c.get(adresse).content.decode()
    konfiguration.refresh_from_db()
    assert konfiguration.rueckgabe_token_prefix in seite
    roh = seite.split('data-testid="rueckgabe-schluessel">', 1)[1].split("<", 1)[0]
    assert einstellungen.rueckgabe_schluessel_passt(konfiguration, roh)
    # Nur einmal sichtbar, und nie der API-Schlüssel des Dienstes
    nochmal = c.get(adresse).content.decode()
    assert roh not in nochmal and "geheim" not in nochmal
    eintraege = SessionAuditLog.objects.filter(tenant=w.tenant, model_name__icontains="protokolldienst")
    assert eintraege.exists() and roh not in str(list(eintraege.values_list("changes", flat=True)))


def test_einstellungen_nur_mit_recht() -> None:
    w = Welt("ohne-recht", angebunden=False)
    leser = nutzer(w.tenant, "leser", "view_meetings")
    antwort = client(leser).get(f"/session/{w.tenant.slug}/settings/protokolldienst/")
    assert antwort.status_code in (302, 403)


def test_unbekannter_vorschlag(w: Welt) -> None:
    with pytest.raises(vorschlaege.VorschlagError):
        vorschlaege.verwerfen(w.meeting, uuid.uuid4(), permissions=RECHTE, session_user=None)
