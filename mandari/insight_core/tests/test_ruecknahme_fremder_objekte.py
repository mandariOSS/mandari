# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zurückgenommene Objekte fremder Ratsinformationssysteme verlassen das Bürgerportal vollständig.

- Eine Markierung mit einem Grund der Rücknahme (zurückgezogen, nicht mehr öffentlich, aus Datenschutzgründen
  entfernt) wirkt wie eine Rücknahme durch mandari Session: Die Seite antwortet mit 410 ohne Inhalt, andere
  Seiten nennen das Objekt nicht mehr. In der Quelle Gelöschtes bleibt mit Hinweis sichtbar.
- Anlagen eines zurückgenommenen Vorgangs oder einer zurückgenommenen Sitzung werden nicht ausgeliefert, auch
  nicht ihr erkannter Text über die Schnittstelle.
- Das Foto einer Person verschwindet mit ihrer Zeile, bei einer Entfernung aus Datenschutzgründen sofort.
- Der Digest nennt keinen Vorgang, den es beim Versand nicht mehr gibt.
- Die Verwaltungsaktion „Aus der Veröffentlichung zurücknehmen“ markiert mit Grund, nimmt die Anlagen mit und
  meldet die Rücknahme im Änderungsfeed.
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import Any, cast

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.db import transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.events.models import Event
from insight_core.models import (
    DEPUBLISHED_REASONS,
    REASON_DELETED_AT_SOURCE,
    REASON_PRIVACY,
    REASON_WITHDRAWN,
    InsightSubscriber,
    OParlBody,
    OParlFile,
    OParlMeeting,
    OParlMembership,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
    OParlSource,
    SubscriptionAlert,
    withdrawn_q,
)
from insight_core.services import file_reconcile
from insight_core.services.search_service import ElasticsearchService

pytestmark = pytest.mark.django_db

RIS = "https://ris.fremd.example/oparl"
PDF = b"%PDF-1.4 Lageplan mit Namen"
PERSON = "Anna Beispielperson"
VORGANG = "Bauantrag Musterweg"


@pytest.fixture(autouse=True)
def _leerer_cache() -> Any:
    cache.clear()
    yield
    cache.clear()


def _datei(tmp_path: Path, name: str, **felder: Any) -> OParlFile:
    pfad = tmp_path / f"{name}.pdf"
    pfad.write_bytes(PDF)
    return OParlFile.objects.create(
        external_id=f"{RIS}/file/{name}",
        name=name,
        file_name=f"{name}.pdf",
        mime_type="application/pdf",
        local_path=str(pfad),
        local_status="ok",
        text_content="Erkannter Text mit Namen",
        **felder,
    )


@pytest.fixture
def welt(tmp_path: Path) -> dict[str, Any]:
    """Bestand eines fremden RIS: Gremium mit Mitglied, Vorgang und Sitzung mit je einer Anlage."""
    source = OParlSource.objects.create(name="Fremd-RIS", url=f"{RIS}/system")
    body = OParlBody.objects.create(
        external_id=f"{RIS}/body/1", source=source, name="Beispielstadt", slug="beispielstadt", is_listed=True
    )
    rat = OParlOrganization.objects.create(external_id=f"{RIS}/organization/1", body=body, name="Rat der Stadt")
    person = OParlPerson.objects.create(
        external_id=f"{RIS}/person/1",
        body=body,
        name=PERSON,
        given_name="Anna",
        family_name="Beispielperson",
        email="anna@beispielstadt.example",
    )
    OParlMembership.objects.create(external_id=f"{RIS}/membership/1", person=person, organization=rat)
    vorgang = OParlPaper.objects.create(external_id=f"{RIS}/paper/1", body=body, name=VORGANG, reference="V/1")
    sitzung = OParlMeeting.objects.create(
        external_id=f"{RIS}/meeting/1", body=body, name="Sitzung des Rates", start=timezone.now()
    )
    return {
        "body": body,
        "rat": rat,
        "person": person,
        "vorgang": vorgang,
        "sitzung": sitzung,
        "anlage": _datei(tmp_path, "lageplan", body=body, paper=vorgang),
        "niederschrift": _datei(tmp_path, "niederschrift", body=body, meeting=sitzung),
        "frei": _datei(tmp_path, "satzung", body=body),
    }


def _seite(name: str, obj: Any) -> str:
    return reverse(f"insight_core:insight:{name}", args=[obj.pk])


def _vorschau(datei: OParlFile, **parameter: str) -> Any:
    return Client().get(f"/insight/dokumente/{datei.pk}/preview/", parameter)


# --- Detailseiten und Verweise ----------------------------------------------------------------------------


@pytest.mark.parametrize("grund", sorted(DEPUBLISHED_REASONS))
@pytest.mark.parametrize(
    ("schluessel", "seite"),
    [
        ("person", "person_detail"),
        ("vorgang", "paper_detail"),
        ("sitzung", "meeting_detail"),
        ("rat", "organization_detail"),
    ],
)
def test_zurueckgenommenes_objekt_antwortet_ohne_inhalt(
    welt: dict[str, Any], schluessel: str, seite: str, grund: str
) -> None:
    objekt = welt[schluessel]
    assert Client().get(_seite(seite, objekt)).status_code == 200

    objekt.mark_deleted(reason=grund)

    antwort = Client().get(_seite(seite, objekt))
    assert antwort.status_code == 410
    assert antwort["X-Robots-Tag"] == "noindex"
    html = antwort.content.decode()
    assert objekt.name not in html and "anna@beispielstadt.example" not in html
    # Der Hinweis nennt für Objekte fremder Systeme keinen Urheber
    assert "Die Verwaltung hat" not in html and "aus der Veröffentlichung zurückgenommen" in html


def test_in_der_quelle_geloeschtes_bleibt_mit_hinweis_sichtbar(welt: dict[str, Any]) -> None:
    welt["person"].mark_deleted(reason=REASON_DELETED_AT_SOURCE)
    OParlPaper.objects.filter(pk=welt["vorgang"].pk).update(deleted=True, deleted_at=timezone.now())

    assert Client().get(_seite("person_detail", welt["person"])).status_code == 200
    assert Client().get(_seite("paper_detail", welt["vorgang"])).status_code == 200
    assert not OParlPerson.objects.filter(withdrawn_q()).exists()
    assert not OParlPaper.objects.filter(withdrawn_q()).exists()


def test_entfernte_person_fehlt_auf_der_seite_des_gremiums(welt: dict[str, Any]) -> None:
    seite = _seite("organization_detail", welt["rat"])
    assert PERSON in Client().get(seite).content.decode()

    welt["person"].mark_deleted(reason=REASON_PRIVACY)

    assert PERSON not in Client().get(seite).content.decode()


def test_abfrage_trifft_nur_zurueckgenommenes(welt: dict[str, Any]) -> None:
    """Unmarkierte Zeilen haben keinen Grund: Sie bleiben in jeder Liste, auch über Beziehungen."""
    zweite = OParlPerson.objects.create(external_id=f"{RIS}/person/2", body=welt["body"], name="Bernd Beispiel")
    OParlMembership.objects.create(external_id=f"{RIS}/membership/2", person=zweite, organization=welt["rat"])
    welt["person"].mark_deleted(reason=REASON_PRIVACY)

    assert list(OParlPerson.objects.exclude(withdrawn_q())) == [zweite]
    assert list(OParlPerson.objects.filter(withdrawn_q())) == [welt["person"]]
    sichtbar = OParlMembership.objects.exclude(withdrawn_q("person")).values_list("person_id", flat=True)
    assert list(sichtbar) == [zweite.pk]
    # Ein leerer Fremdschlüssel zählt nicht als zurückgenommen
    assert OParlFile.objects.exclude(withdrawn_q("paper")).count() == 3


# --- Anlagen ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("eltern", "datei"), [("vorgang", "anlage"), ("sitzung", "niederschrift")])
def test_anlage_eines_zurueckgenommenen_eintrags_wird_nicht_ausgeliefert(
    welt: dict[str, Any], eltern: str, datei: str
) -> None:
    vorher = _vorschau(welt[datei])
    assert vorher.status_code == 200 and vorher["X-Mandari-Cache"] == "hit"

    welt[eltern].mark_deleted(reason=REASON_PRIVACY)

    assert file_reconcile.parent_withdrawn(welt[datei])
    for parameter in ({}, {"download": "1"}):
        antwort = _vorschau(welt[datei], **parameter)
        assert antwort.status_code == 410
        assert antwort["Cache-Control"] == "no-store"
        assert PDF not in antwort.content and "X-Accel-Redirect" not in antwort
    # Andere Anlagen bleiben abrufbar
    assert _vorschau(welt["frei"]).status_code == 200
    assert not file_reconcile.parent_withdrawn(welt["frei"])


def test_schnittstelle_gibt_den_text_solcher_anlagen_nicht_aus(welt: dict[str, Any], settings: Any) -> None:
    settings.OPARL_API_RATE_LIMIT = 0
    settings.OPARL_API_CACHE_SECONDS = 0
    adresse = f"/oparl/v1/file/{welt['anlage'].pk}"
    assert Client().get(adresse).json()["text"] == "Erkannter Text mit Namen"

    welt["vorgang"].mark_deleted(reason=REASON_PRIVACY)

    antwort = Client().get(adresse)
    assert antwort.status_code == 200 and "text" not in antwort.json()
    assert "text" in Client().get(f"/oparl/v1/file/{welt['frei'].pk}").json()


# --- Personenfoto -----------------------------------------------------------------------------------------


@pytest.fixture
def foto(welt: dict[str, Any], settings: Any, tmp_path: Path) -> Any:
    """Person mit lokal gespeichertem Foto; liefert (Person, Ablage, Dateiname)."""
    settings.MEDIA_ROOT = str(tmp_path / "media")
    person = welt["person"]
    person.photo.save(f"{person.pk}.jpg", ContentFile(b"\xff\xd8\xff Foto"), save=False)
    person.photo_status = "ok"
    person.save(update_fields=["photo", "photo_status"])
    return person, person.photo.storage, person.photo.name


def test_endgueltiges_loeschen_entfernt_das_foto(foto: Any, django_capture_on_commit_callbacks: Any) -> None:
    person, ablage, name = foto
    person.mark_deleted()
    assert ablage.exists(name), "nur markiert: Die Markierung lässt sich aufheben"

    with django_capture_on_commit_callbacks(execute=True):
        call_command("purge_deleted", "--ids", str(person.pk), "--yes", stdout=StringIO())

    assert not OParlPerson.objects.filter(pk=person.pk).exists()
    assert not ablage.exists(name)


def test_foto_bleibt_wenn_das_loeschen_zurueckgerollt_wird(foto: Any, django_capture_on_commit_callbacks: Any) -> None:
    person, ablage, name = foto
    kennung = person.pk  # delete() leert sie am Objekt

    with django_capture_on_commit_callbacks(execute=True):
        try:
            with transaction.atomic():
                person.delete()
                raise RuntimeError("Abbruch nach dem Löschen")
        except RuntimeError:
            pass

    assert OParlPerson.objects.filter(pk=kennung).exists()
    assert ablage.exists(name)


def test_dateifehler_haelt_das_loeschen_nicht_auf(
    foto: Any, django_capture_on_commit_callbacks: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    person, ablage, _name = foto

    def gesperrt(name: str) -> None:
        raise PermissionError("Datei in Benutzung")

    monkeypatch.setattr(ablage, "delete", gesperrt)
    person.mark_deleted()

    with django_capture_on_commit_callbacks(execute=True):
        call_command("purge_deleted", "--ids", str(person.pk), "--yes", stdout=StringIO())

    assert not OParlPerson.objects.filter(pk=person.pk).exists()


def test_entfernung_aus_datenschutzgruenden_nimmt_das_foto_sofort(
    foto: Any, django_capture_on_commit_callbacks: Any
) -> None:
    person, ablage, name = foto

    with django_capture_on_commit_callbacks(execute=True):
        person.mark_deleted(reason=REASON_PRIVACY)

    assert not ablage.exists(name)
    person.refresh_from_db()
    assert not person.photo and person.photo_status == "missing"
    assert person.photo_src is None


def test_aufhebbare_markierung_behaelt_das_foto(foto: Any, django_capture_on_commit_callbacks: Any) -> None:
    """Zurückgezogenes kommt beim Reaktivieren eines Mandanten wieder – mit Foto."""
    person, ablage, name = foto

    with django_capture_on_commit_callbacks(execute=True):
        person.mark_deleted(reason=REASON_WITHDRAWN)

    person.refresh_from_db()
    assert ablage.exists(name) and person.photo.name == name


# --- Digest -----------------------------------------------------------------------------------------------


@pytest.fixture
def abo(welt: dict[str, Any], settings: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Bestätigtes Abo mit je einem offenen Hinweis auf zwei Vorgänge; ``mails`` sammelt den Versand."""
    settings.INSIGHT_SUBSCRIPTIONS_ENABLED = True
    settings.INSIGHT_DIGEST_ENABLED = True
    abonnent = InsightSubscriber.objects.create(
        email="leserin@example.org", body=welt["body"], keyword="Bauantrag", keyword_active=True, confirmed=True
    )
    anderer = OParlPaper.objects.create(external_id=f"{RIS}/paper/2", body=welt["body"], name="Radwegekonzept")
    hinweise = {
        vorgang.pk: SubscriptionAlert.objects.create(
            subscriber=abonnent,
            alert_type="keyword",
            entity_type="paper",
            entity_id=vorgang.pk,
            entity_title=vorgang.name or "",
            entity_url=f"/insight/vorgaenge/{vorgang.pk}/",
        )
        for vorgang in (welt["vorgang"], anderer)
    }
    mails: list[str] = []

    def senden(**angaben: Any) -> bool:
        mails.append(f"{angaben['body']}\n{angaben['html_body']}")
        return True

    monkeypatch.setattr("apps.common.mail.send", senden)
    return {"abonnent": abonnent, "anderer": anderer, "hinweise": hinweise, "mails": mails}


def _digest(*argumente: str) -> None:
    call_command("send_digest", *argumente, stdout=StringIO())


def test_digest_nennt_keinen_inzwischen_markierten_vorgang(welt: dict[str, Any], abo: dict[str, Any]) -> None:
    # Wie der Ingestor: Markierung ohne die Anwendung
    OParlPaper.objects.filter(pk=welt["vorgang"].pk).update(deleted=True, deleted_at=timezone.now())

    _digest()

    assert len(abo["mails"]) == 1
    assert VORGANG not in abo["mails"][0] and "Radwegekonzept" in abo["mails"][0]
    assert not SubscriptionAlert.objects.filter(entity_id=welt["vorgang"].pk).exists()
    assert SubscriptionAlert.objects.get(entity_id=abo["anderer"].pk).sent_in_digest is not None


def test_digest_ohne_mail_wenn_kein_hinweis_uebrig_bleibt(welt: dict[str, Any], abo: dict[str, Any]) -> None:
    OParlPaper.objects.filter(body=welt["body"]).update(deleted=True, deleted_at=timezone.now())

    _digest()

    assert abo["mails"] == []
    assert not SubscriptionAlert.objects.exists()


def test_probelauf_des_digests_verwirft_nichts(welt: dict[str, Any], abo: dict[str, Any]) -> None:
    OParlPaper.objects.filter(pk=welt["vorgang"].pk).update(deleted=True, deleted_at=timezone.now())

    _digest("--dry-run")

    assert abo["mails"] == []
    assert SubscriptionAlert.objects.count() == 2


def test_ruecknahme_und_endgueltiges_loeschen_verwerfen_die_hinweise(welt: dict[str, Any], abo: dict[str, Any]) -> None:
    welt["vorgang"].mark_deleted(reason=REASON_WITHDRAWN)
    assert not SubscriptionAlert.objects.filter(entity_id=welt["vorgang"].pk).exists()

    anderer = abo["anderer"]
    anderer.mark_deleted()
    assert SubscriptionAlert.objects.filter(entity_id=anderer.pk).exists(), "in der Quelle gelöscht: erst beim Versand"
    call_command("purge_deleted", "--ids", str(anderer.pk), "--yes", stdout=StringIO())
    assert not SubscriptionAlert.objects.exists()


# --- Verwaltungsaktion ------------------------------------------------------------------------------------


@pytest.fixture
def verwaltung(db: Any, settings: Any) -> Client:
    settings.INGESTOR_EVENTS_ENABLED = True
    konto = get_user_model()(email="admin@example.org", is_staff=True, is_superuser=True, is_active=True)
    konto.set_password("geheim-123")
    konto.save()
    client = Client()
    client.force_login(konto)
    return client


def _aktion(verwaltung: Client, modell: str, *objekte: Any) -> None:
    antwort = verwaltung.post(
        reverse(f"admin:insight_core_{modell}_changelist"),
        {"action": "retract_selected", "_selected_action": [str(objekt.pk) for objekt in objekte]},
    )
    assert antwort.status_code == 302


def test_aktion_nimmt_vorgang_mit_anlagen_zurueck_und_meldet_es(welt: dict[str, Any], verwaltung: Client) -> None:
    vorgang, anlage = welt["vorgang"], welt["anlage"]

    _aktion(verwaltung, "oparlpaper", vorgang)

    vorgang.refresh_from_db()
    anlage.refresh_from_db()
    assert (vorgang.deleted, vorgang.deletion_reason) == (True, REASON_WITHDRAWN)
    assert (anlage.deleted, anlage.deletion_reason) == (True, REASON_WITHDRAWN)
    assert Client().get(_seite("paper_detail", vorgang)).status_code == 410
    assert _vorschau(anlage).status_code == 410
    # Je Objekt eine öffentliche Meldung für Abnehmer des Änderungsfeeds
    gemeldet = Event.objects.filter(type="ris.object.depublished")
    assert {(e.aggregate_type, e.aggregate_id) for e in gemeldet} == {("Paper", vorgang.pk), ("File", anlage.pk)}
    assert all((e.visibility, e.operation) == ("oeffentlich", "delete") for e in gemeldet)
    assert all(e.payload["reason"] == REASON_WITHDRAWN for e in gemeldet)
    # Andere Objekte bleiben unberührt
    assert not OParlFile.objects.get(pk=welt["frei"].pk).deleted


def test_aktion_setzt_den_grund_einer_bestehenden_markierung(welt: dict[str, Any], verwaltung: Client) -> None:
    """Nur das Häkchen gesetzt (ohne Grund): Das Objekt bleibt sichtbar, bis die Aktion es zurücknimmt."""
    person = welt["person"]
    OParlPerson.objects.filter(pk=person.pk).update(deleted=True, deleted_at=timezone.now())
    assert Client().get(_seite("person_detail", person)).status_code == 200

    _aktion(verwaltung, "oparlperson", person)

    person.refresh_from_db()
    assert person.deletion_reason == REASON_WITHDRAWN
    assert Client().get(_seite("person_detail", person)).status_code == 410
    assert not Event.objects.filter(type="ris.object.depublished").exists(), "die Markierung bestand schon"


# --- Suche ------------------------------------------------------------------------------------------------


class _Suchindex:
    """Elasticsearch-Ersatz: Der Index kennt Vorgang und Anlagen noch, gleich was im Bestand geschieht."""

    def __init__(self, dokumente: dict[str, list[dict[str, Any]]]) -> None:
        self.dokumente = dokumente
        self.indices = self

    def exists(self, index: str) -> bool:
        return True

    def search(self, index: str, body: dict[str, Any]) -> dict[str, Any]:
        hits = []
        for quelle in self.dokumente.get(index, []):
            hit: dict[str, Any] = {"_id": quelle["id"], "_score": 2.0}
            if body.get("_source", True) is not False:
                hit["_source"] = dict(quelle)
            hits.append(hit)
        return {"hits": {"hits": hits, "total": {"value": len(hits), "relation": "eq"}, "max_score": 2.0}}

    def count(self, index: str, query: dict[str, Any], **_: Any) -> dict[str, int]:
        return {"count": len(self.dokumente.get(index, []))}


def _gefunden(welt: dict[str, Any]) -> set[str]:
    dienst = cast(Any, ElasticsearchService).__new__(ElasticsearchService)
    dienst.client = _Suchindex(
        {
            "papers": [{"id": str(welt["vorgang"].pk), "type": "paper", "name": VORGANG}],
            "files": [
                {"id": str(welt[name].pk), "type": "file", "name": name, "paper_id": paper}
                for name, paper in (("anlage", str(welt["vorgang"].pk)), ("frei", None))
            ],
        }
    )
    ergebnis = dienst.search_all("Bauantrag", index_names=["papers", "files"])
    return {treffer["id"] for treffer in ergebnis["results"]}


def test_suche_nennt_keine_im_bestand_markierten_treffer(welt: dict[str, Any]) -> None:
    """Der Index behält ein Dokument, wenn sein Entfernen scheitert; die Trefferliste zeigt es trotzdem nicht."""
    vorgang, anlage, frei = (str(welt[name].pk) for name in ("vorgang", "anlage", "frei"))
    assert _gefunden(welt) == {vorgang, anlage, frei}

    # Markierung am Suchindex vorbei (wie nach einem gescheiterten Aufruf des Suchdienstes)
    OParlPaper.objects.filter(pk=vorgang).update(
        deleted=True, deleted_at=timezone.now(), deletion_reason=REASON_WITHDRAWN
    )

    assert _gefunden(welt) == {frei}, "weder der Vorgang noch seine Anlage"


def test_suche_behaelt_die_anlage_eines_in_der_quelle_geloeschten_vorgangs(welt: dict[str, Any]) -> None:
    """In der Quelle Gelöschtes bleibt mit Hinweis sichtbar; seine Anlagen sperrt erst der Abgleich der Dateien."""
    vorgang, anlage, frei = (str(welt[name].pk) for name in ("vorgang", "anlage", "frei"))
    OParlPaper.objects.filter(pk=vorgang).update(deleted=True, deleted_at=timezone.now())

    assert _gefunden(welt) == {anlage, frei}
    OParlFile.objects.filter(pk=anlage).update(source_missing_since=timezone.now())
    assert _gefunden(welt) == {frei}
