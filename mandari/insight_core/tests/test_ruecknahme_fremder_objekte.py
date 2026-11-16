# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zurückgenommene Objekte fremder Ratsinformationssysteme verlassen das Bürgerportal vollständig.

- Eine Markierung mit einem Grund der Rücknahme (zurückgezogen, nicht mehr öffentlich, aus Datenschutzgründen
  entfernt) wirkt wie eine Rücknahme durch mandari Session: Die Seite antwortet mit 410 ohne Inhalt, andere
  Seiten nennen das Objekt nicht mehr. In der Quelle Gelöschtes bleibt mit Hinweis sichtbar.
- Anlagen eines zurückgenommenen Vorgangs oder einer zurückgenommenen Sitzung werden nicht ausgeliefert; die
  Schnittstelle gibt sie nur gekürzt aus, der Snapshot nennt sie nicht.
- Das Foto einer Person verschwindet mit ihrer Zeile, bei einer Entfernung aus Datenschutzgründen sofort.
- Der Digest nennt keinen Vorgang, den es beim Versand nicht mehr gibt oder der markiert ist. Hinweise auf
  zurückgenommene Vorgänge verwirft er; solche auf nur in der Quelle gelöschte überspringt er.
- Die Verwaltungsaktion „Dauerhaft aus der Veröffentlichung zurücknehmen“ fragt nach, markiert mit dem Grund, den
  der Abgleich mit der Quelle stehen lässt, nimmt die Anlagen mit und meldet die Rücknahme im Änderungsfeed.
  Objekte aus mandari Session lässt sie unverändert.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from io import StringIO
from pathlib import Path
from typing import Any, cast

import pytest
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.db import transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.events.models import Event
from hub.api import aggregator, snapshot
from insight_core.models import (
    DEPUBLISHED_REASONS,
    REASON_DELETED_AT_SOURCE,
    REASON_NOT_PUBLIC,
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


def test_schnittstelle_gibt_solche_anlagen_nur_gekuerzt_aus(welt: dict[str, Any], settings: Any) -> None:
    """Nur der Vorgang trägt die Markierung (von Hand gesetzt, ohne die Aktion): Die Anlage folgt ihm."""
    settings.OPARL_API_RATE_LIMIT = 0
    settings.OPARL_API_CACHE_SECONDS = 0
    adresse = f"/oparl/v1/file/{welt['anlage'].pk}"
    vorher = Client().get(adresse).json()
    assert vorher["text"] == "Erkannter Text mit Namen" and vorher["fileName"] == "lageplan.pdf"

    welt["vorgang"].mark_deleted(reason=REASON_PRIVACY)

    antwort = Client().get(adresse)
    assert antwort.status_code == 200
    gekuerzt = antwort.json()
    assert gekuerzt["deleted"] is True and gekuerzt["id"] == vorher["id"]
    assert set(gekuerzt) == {"id", "type", "created", "modified", "deleted"}, "weder Text noch Name noch Adressen"
    assert "text" in Client().get(f"/oparl/v1/file/{welt['frei'].pk}").json()


def test_snapshot_nennt_solche_anlagen_nicht(welt: dict[str, Any]) -> None:
    def dateien() -> set[str]:
        objekte = snapshot.objects(aggregator.snapshot_for_measurement(welt["body"].pk))
        return {str(o["id"]).rsplit("/", 1)[1] for o in objekte if str(o["type"]).endswith("/File")}

    anlage, niederschrift, frei = (str(welt[name].pk) for name in ("anlage", "niederschrift", "frei"))
    assert dateien() == {anlage, niederschrift, frei}

    welt["vorgang"].mark_deleted(reason=REASON_PRIVACY)

    assert dateien() == {niederschrift, frei}


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


@pytest.mark.parametrize(
    "fehler",
    [PermissionError("Datei in Benutzung"), RuntimeError("Ablage nicht erreichbar")],
    ids=["dateisystem", "entfernte_ablage"],
)
def test_fehler_der_ablage_haelt_das_loeschen_nicht_auf(
    foto: Any, django_capture_on_commit_callbacks: Any, monkeypatch: pytest.MonkeyPatch, fehler: Exception
) -> None:
    """Welche Fehler eine Ablage wirft, legt sie selbst fest (eine entfernte Ablage etwa keinen ``OSError``)."""
    person, ablage, _name = foto

    def gesperrt(name: str) -> None:
        raise fehler

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


def _markiert(vorgang: OParlPaper, grund: str | None = None) -> None:
    """Markierung am Modell vorbei, wie der Ingestor sie setzt."""
    OParlPaper.objects.filter(pk=vorgang.pk).update(deleted=True, deleted_at=timezone.now(), deletion_reason=grund)


def test_digest_ueberspringt_einen_in_der_quelle_geloeschten_vorgang(welt: dict[str, Any], abo: dict[str, Any]) -> None:
    _markiert(welt["vorgang"])

    _digest()

    assert len(abo["mails"]) == 1
    assert VORGANG not in abo["mails"][0] and "Radwegekonzept" in abo["mails"][0]
    assert SubscriptionAlert.objects.get(entity_id=abo["anderer"].pk).sent_in_digest is not None
    # Die Markierung kann vorübergehend sein: Der Hinweis bleibt offen ...
    assert SubscriptionAlert.objects.get(entity_id=welt["vorgang"].pk).sent_in_digest is None

    # ... und geht hinaus, sobald der Abgleich sie aufgehoben hat
    OParlPaper.objects.filter(pk=welt["vorgang"].pk).update(deleted=False, deleted_at=None, deletion_reason=None)
    _digest()

    assert len(abo["mails"]) == 2 and VORGANG in abo["mails"][1]


def test_digest_verwirft_hinweise_auf_zurueckgenommene_und_fehlende_vorgaenge(
    welt: dict[str, Any], abo: dict[str, Any]
) -> None:
    _markiert(welt["vorgang"], REASON_WITHDRAWN)
    verwaist = SubscriptionAlert.objects.create(
        subscriber=abo["abonnent"],
        alert_type="keyword",
        entity_type="paper",
        entity_id=uuid.uuid4(),
        entity_title="Endgültig gelöschter Vorgang",
        entity_url="/insight/vorgaenge/gibt-es-nicht/",
    )

    _digest()

    assert len(abo["mails"]) == 1
    assert VORGANG not in abo["mails"][0] and "Endgültig gelöschter Vorgang" not in abo["mails"][0]
    assert "Radwegekonzept" in abo["mails"][0]
    assert not SubscriptionAlert.objects.filter(pk__in=[verwaist.pk, abo["hinweise"][welt["vorgang"].pk].pk]).exists()


def test_digest_ohne_mail_wenn_kein_hinweis_uebrig_bleibt(welt: dict[str, Any], abo: dict[str, Any]) -> None:
    _markiert(welt["vorgang"], REASON_WITHDRAWN)
    _markiert(abo["anderer"])

    _digest()

    assert abo["mails"] == []
    assert [h.entity_id for h in SubscriptionAlert.objects.all()] == [abo["anderer"].pk], (
        "übersprungen, nicht verworfen"
    )


@pytest.mark.parametrize("grund", [None, REASON_WITHDRAWN], ids=["uebersprungen", "verworfen"])
def test_digest_fuellt_hinter_ausgelassenen_hinweisen_auf(
    welt: dict[str, Any], abo: dict[str, Any], settings: Any, grund: str | None
) -> None:
    """Begrenzt wird die Zahl der gültigen Hinweise: Ein ausgelassener vorn hält die dahinter nicht bis zum nächsten Lauf auf."""
    settings.INSIGHT_DIGEST_MAX_ALERTS_PER_MAIL = 1
    # Neueste zuerst: Der Hinweis auf den markierten Vorgang steht vorn
    SubscriptionAlert.objects.filter(entity_id=welt["vorgang"].pk).update(
        created_at=timezone.now() - timedelta(hours=1)
    )
    _markiert(abo["anderer"], grund)

    _digest()

    assert len(abo["mails"]) == 1 and VORGANG in abo["mails"][0]
    assert "Radwegekonzept" not in abo["mails"][0]
    assert SubscriptionAlert.objects.filter(entity_id=abo["anderer"].pk).exists() is (grund is None)


def test_probelauf_des_digests_verwirft_nichts(welt: dict[str, Any], abo: dict[str, Any]) -> None:
    _markiert(welt["vorgang"], REASON_WITHDRAWN)

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


def test_ruecknahme_behaelt_schon_versandte_hinweise(welt: dict[str, Any], abo: dict[str, Any]) -> None:
    """
    Der versandte Hinweis ist die Sperre gegen eine zweite Meldung desselben Vorgangs, falls er später wieder
    veröffentlicht wird. Mit dem endgültigen Löschen geht auch er.
    """
    _digest()
    assert SubscriptionAlert.objects.filter(sent_in_digest__isnull=False).count() == 2

    welt["vorgang"].mark_deleted(reason=REASON_WITHDRAWN)

    assert SubscriptionAlert.objects.get(entity_id=welt["vorgang"].pk).sent_in_digest is not None
    call_command("purge_deleted", "--ids", str(welt["vorgang"].pk), "--yes", stdout=StringIO())
    assert not SubscriptionAlert.objects.filter(entity_id=welt["vorgang"].pk).exists()


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


def _aktion(verwaltung: Client, modell: str, *objekte: Any, bestaetigt: bool = True) -> Any:
    """Aktion aus der Liste aufrufen; ``bestaetigt``: das Formular der Zwischenseite, sonst das der Liste."""
    daten: dict[str, Any] = {"action": "retract_selected", "_selected_action": [str(objekt.pk) for objekt in objekte]}
    if bestaetigt:
        daten["post"] = "ja"
    antwort = verwaltung.post(reverse(f"admin:insight_core_{modell}_changelist"), daten)
    assert antwort.status_code == (302 if bestaetigt else 200)
    return antwort


def _meldungen(antwort: Any) -> str:
    return " | ".join(str(meldung) for meldung in get_messages(antwort.wsgi_request))


def _gemeldet() -> list[tuple[str, Any, str, str]]:
    ereignisse = Event.objects.filter(type="ris.object.depublished").order_by("aggregate_type")
    return [(e.aggregate_type, e.aggregate_id, e.operation, e.payload["reason"]) for e in ereignisse]


def test_aktion_fragt_nach_und_aendert_bis_dahin_nichts(welt: dict[str, Any], verwaltung: Client) -> None:
    vorgang = welt["vorgang"]

    antwort = _aktion(verwaltung, "oparlpaper", vorgang, bestaetigt=False)

    html = antwort.content.decode()
    assert "Dauerhaft aus der Veröffentlichung zurücknehmen" in html
    assert "stellt sie nicht wieder her" in html and "Fotos von Personen" in html
    assert str(vorgang) in html, "die Seite nennt, was gewählt ist"
    # Das Formular der Seite ruft dieselbe Aktion mit denselben Objekten erneut auf
    for feld in ('name="action" value="retract_selected"', 'name="post" value="ja"', f'value="{vorgang.pk}"'):
        assert feld in html, feld
    vorgang.refresh_from_db()
    assert not vorgang.deleted and not OParlFile.objects.filter(deleted=True).exists()
    assert _gemeldet() == []


def test_aktion_nimmt_vorgang_mit_anlagen_dauerhaft_zurueck_und_meldet_es(
    welt: dict[str, Any], verwaltung: Client
) -> None:
    vorgang, anlage = welt["vorgang"], welt["anlage"]

    antwort = _aktion(verwaltung, "oparlpaper", vorgang)

    vorgang.refresh_from_db()
    anlage.refresh_from_db()
    # Der Grund an der Zeile ist der, den der Abgleich mit der Quelle stehen lässt (Ingestor: ``keeps_withdrawal``
    # vergleicht mit genau diesem Wert); jeden anderen höbe er auf, sobald die Quelle das Objekt wieder liefert
    assert REASON_PRIVACY == "datenschutz"
    assert (vorgang.deleted, vorgang.deletion_reason) == (True, REASON_PRIVACY)
    assert (anlage.deleted, anlage.deletion_reason) == (True, REASON_PRIVACY)
    assert Client().get(_seite("paper_detail", vorgang)).status_code == 410
    assert _vorschau(anlage).status_code == 410
    # Je Objekt eine öffentliche Meldung für Abnehmer des Änderungsfeeds, als Rücknahme
    assert _gemeldet() == [
        ("File", anlage.pk, "delete", REASON_WITHDRAWN),
        ("Paper", vorgang.pk, "delete", REASON_WITHDRAWN),
    ]
    assert all(e.visibility == "oeffentlich" for e in Event.objects.filter(type="ris.object.depublished"))
    meldungen = _meldungen(antwort)
    assert "Dauerhaft aus der Veröffentlichung genommen: 1 zurückgenommen, 1 Anlagen mitgenommen" in meldungen
    assert "stellt diese Objekte nicht wieder her" in meldungen
    # Andere Objekte bleiben unberührt
    assert not OParlFile.objects.get(pk=welt["frei"].pk).deleted

    # Ein zweiter Aufruf ändert und meldet nichts mehr
    assert "schon dauerhaft zurückgenommen" in _meldungen(_aktion(verwaltung, "oparlpaper", vorgang))
    assert len(_gemeldet()) == 2


@pytest.mark.parametrize(
    ("grund", "vorher"),
    [(None, 200), (REASON_DELETED_AT_SOURCE, 200), (REASON_WITHDRAWN, 410), (REASON_NOT_PUBLIC, 410)],
    ids=["ohne_grund", "quelle_geloescht", "zurueckgenommen", "nichtoeffentlich"],
)
def test_aktion_nimmt_schon_markiertes_dauerhaft_zurueck_und_meldet_es(
    welt: dict[str, Any], verwaltung: Client, grund: str | None, vorher: int
) -> None:
    """
    Nur das Häkchen gesetzt oder in der Quelle gelöscht: Das Objekt bleibt sichtbar, bis die Aktion es zurücknimmt.
    Auch eine Rücknahme von Hand bekommt den dauerhaften Grund; sonst höbe der nächste Abgleich sie auf.
    """
    person = welt["person"]
    OParlPerson.objects.filter(pk=person.pk).update(deleted=True, deleted_at=timezone.now(), deletion_reason=grund)
    assert Client().get(_seite("person_detail", person)).status_code == vorher

    antwort = _aktion(verwaltung, "oparlperson", person)

    person.refresh_from_db()
    assert person.deletion_reason == REASON_PRIVACY
    assert Client().get(_seite("person_detail", person)).status_code == 410
    # Die Markierung bestand schon, die Rücknahme ist neu: Abnehmer erfahren sie
    assert _gemeldet() == [("Person", person.pk, "delete", REASON_WITHDRAWN)]
    assert "1 waren schon markiert und sind jetzt dauerhaft zurückgenommen" in _meldungen(antwort)


def test_aktion_nimmt_schon_markierte_anlagen_mit(welt: dict[str, Any], verwaltung: Client) -> None:
    """Eine in der Quelle gelöschte Anlage käme mit dem nächsten Abgleich zurück, wenn die Quelle sie wieder liefert."""
    anlage = welt["anlage"]
    anlage.mark_deleted(reason=REASON_DELETED_AT_SOURCE)

    _aktion(verwaltung, "oparlpaper", welt["vorgang"])

    anlage.refresh_from_db()
    assert (anlage.deleted, anlage.deletion_reason) == (True, REASON_PRIVACY)


def test_aktion_loescht_das_foto_der_person(
    foto: Any, verwaltung: Client, django_capture_on_commit_callbacks: Any
) -> None:
    person, ablage, name = foto

    with django_capture_on_commit_callbacks(execute=True):
        antwort = _aktion(verwaltung, "oparlperson", person)

    assert not ablage.exists(name)
    person.refresh_from_db()
    assert not person.photo and person.deletion_reason == REASON_PRIVACY
    assert "Die Fotos dieser Personen sind gelöscht." in _meldungen(antwort)


def test_aktion_laesst_objekte_aus_session_unveraendert(welt: dict[str, Any], verwaltung: Client) -> None:
    """
    Session nimmt selbst zurück und veröffentlicht wieder. Mit dem dauerhaften Grund bliebe ein dort wieder
    veröffentlichtes Objekt hier für immer zurückgenommen.
    """
    session = "https://mandari.example/session/beispielstadt/api/oparl"
    offen = OParlPaper.objects.create(external_id=f"{session}/paper/1", body=welt["body"], name="Haushalt")
    markiert = OParlPaper.objects.create(external_id=f"{session}/paper/2", body=welt["body"], name="Satzung")
    markiert.mark_deleted(reason=REASON_NOT_PUBLIC)

    antwort = _aktion(verwaltung, "oparlpaper", offen, markiert, welt["vorgang"])

    offen.refresh_from_db()
    markiert.refresh_from_db()
    assert not offen.deleted
    assert markiert.deletion_reason == REASON_NOT_PUBLIC
    assert [zeile[:2] for zeile in _gemeldet()] == [("File", welt["anlage"].pk), ("Paper", welt["vorgang"].pk)]
    meldungen = _meldungen(antwort)
    assert "2 stammen aus mandari Session und bleiben unverändert" in meldungen
    assert "1 zurückgenommen" in meldungen


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


def _dienst(welt: dict[str, Any]) -> Any:
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
    return dienst


def _gefunden(welt: dict[str, Any]) -> set[str]:
    ergebnis = _dienst(welt).search_all("Bauantrag", index_names=["papers", "files"])
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


def test_suche_nennt_keinen_treffer_ohne_zeile_im_bestand(welt: dict[str, Any]) -> None:
    """Endgültig gelöscht, das Dokument steht aber noch im Index: Titel und Ausschnitt gehen nicht mehr hinaus."""
    vorgang, anlage, frei = (str(welt[name].pk) for name in ("vorgang", "anlage", "frei"))

    OParlFile.objects.filter(pk=frei).delete()

    assert _gefunden(welt) == {vorgang, anlage}


def test_gruppe_aus_einem_gesperrten_dokument_entfaellt(welt: dict[str, Any]) -> None:
    """Ein einzelnes Dokument ohne Vorgang und Sitzung bildet eine eigene Gruppe; ohne das Dokument bleibt nichts."""
    frei = str(welt["frei"].pk)
    gruppen = [{"kind": "file", "key": frei, "members": {"files": [frei]}}]
    assert [gruppe["key"] for gruppe in _dienst(welt)._load_groups(gruppen, {})] == [frei]

    OParlFile.objects.filter(pk=frei).update(source_missing_since=timezone.now())

    assert _dienst(welt)._load_groups(gruppen, {}) == []
