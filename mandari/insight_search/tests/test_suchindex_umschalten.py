# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Umschalten des Suchindex auf das Abonnement ``suchindex`` (Issue #527).

Mit ``SEARCH_INDEX_SUBSCRIPTION=aktiv`` schreibt nur noch das Abonnement den Live-Index: Die Signale schreiben nichts
und bauen keine Dokumente, ``reindex_elasticsearch`` baut über das Abonnement mit der Folgenummer als Version, und der
Ingestor schreibt nicht (``ingestor/tests/test_suchindex_umschalten.py``). Steht das Abonnement in der Datenbank noch
auf ``schatten``, hält die Zustellung an, statt die Ereignisse in den Schattenindex zu verbrauchen.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from io import StringIO
from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from apps.events import TargetUnavailableError
from apps.events.dispatch import deliver_batch, ensure_subscription
from apps.events.models import ParkedEvent, Subscription, SubscriptionState
from apps.events.registry import Subscriber, get
from apps.events.tests.hilfen import nummeriert
from insight_core import signals
from insight_core.models import OParlBody, OParlFile, OParlMeeting, OParlOrganization, OParlPaper, OParlPerson
from insight_search import abonnement, subscribers
from insight_search.indices import shadow_name
from insight_search.tests.fake_es import FakeElasticsearch

Kommune = Callable[[str], OParlBody]

#: Schreibwege des Suchindex (Kennzahl K2a, ``docs/DATENDREHSCHEIBE.md``), die ``SEARCH_INDEX_SUBSCRIPTION=aktiv``
#: abschaltet, mit dem Test, der es belegt. ``scripts/architektur_kennzahlen.py`` liest die Liste: Was in K2a steht und
#: hier fehlt, schreibt nach dem Umschalten weiter (Einzelheit ``bei_aktiv``, Ziel: nur das Abonnement).
ABGESCHALTET_BEI_AKTIV = {
    "mandari/insight_core/signals.py": (
        "insight_search/tests/test_suchindex_umschalten.py::test_signale_schreiben_mit_aktiv_nichts"
    ),
    "mandari/insight_search/management/commands/reindex_elasticsearch.py": (
        "insight_search/tests/test_suchindex_umschalten.py::test_neuaufbau_mit_aktiv_ueber_das_abonnement"
    ),
    "ingestor/src/indexing/elasticsearch.py": (
        "ingestor/tests/test_suchindex_umschalten.py::test_mit_aktiv_schreibt_der_ingestor_nicht"
    ),
}


def _vorgang(body: OParlBody, name: str = "Vorlage") -> OParlPaper:
    return OParlPaper.objects.create(external_id=f"https://ris.example/paper/{uuid.uuid4()}", body=body, name=name)


def _sitzung(body: OParlBody) -> OParlMeeting:
    return OParlMeeting.objects.create(
        external_id=f"https://ris.example/meeting/{uuid.uuid4()}", body=body, name="Rat", start=timezone.now()
    )


# --- Signale -------------------------------------------------------------------------------------


class _Client:
    """Elasticsearch-Client der Signale: merkt sich, was geschrieben würde."""

    def __init__(self) -> None:
        self.aufrufe: list[str] = []

    def index(self, *, index: str, id: str, document: dict[str, Any]) -> None:  # noqa: A002 – wie der Client
        self.aufrufe.append(f"index {index}/{id}")

    def delete(self, *, index: str, id: str, ignore: Any = None) -> None:  # noqa: A002 – wie der Client
        self.aufrufe.append(f"delete {index}/{id}")


def _bestand_aendern(body: OParlBody) -> None:
    """Was die Signale bisher in den Index trugen: Anlegen, Ändern, Sperren und Löschen."""
    vorgang = _vorgang(body)
    _sitzung(body)
    OParlOrganization.objects.create(external_id=f"https://ris.example/org/{uuid.uuid4()}", body=body, name="Rat")
    person = OParlPerson.objects.create(external_id=f"https://ris.example/person/{uuid.uuid4()}", body=body, name="X")
    datei = OParlFile.objects.create(
        external_id=f"https://ris.example/file/{uuid.uuid4()}",
        body=body,
        paper=vorgang,
        name="Anlage",
        text_content="Spielplatz an der Ringstraße",
        text_extraction_status="completed",
    )
    vorgang.name = "Vorlage, geändert"
    vorgang.save()
    signals.remove_file_from_index(datei)  # Löschabgleich (insight_core.services.file_reconcile)
    datei.delete()
    person.delete()


@pytest.mark.django_db
@pytest.mark.parametrize(("modus", "schreibt"), [("aktiv", False), ("schatten", True), ("aus", True)])
def test_signale_schreiben_mit_aktiv_nichts(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    kommune: Kommune,
    django_capture_on_commit_callbacks: Any,
    modus: str,
    schreibt: bool,
) -> None:
    settings.ELASTICSEARCH_AUTO_INDEX = True
    settings.SEARCH_INDEX_SUBSCRIPTION = modus
    client = _Client()
    monkeypatch.setattr(signals, "_get_elasticsearch_client", lambda: client)
    gebaut: list[str] = []
    for name in ("_paper_to_doc", "_meeting_to_doc", "_organization_to_doc", "_person_to_doc", "_file_to_doc"):
        echt = getattr(signals, name)
        monkeypatch.setattr(signals, name, lambda objekt, _echt=echt, _name=name: gebaut.append(_name) or _echt(objekt))

    with django_capture_on_commit_callbacks(execute=True):
        _bestand_aendern(kommune("Beispielstadt"))

    assert bool(client.aufrufe) is schreibt
    # Mit aktiv bauen die Signale auch keine Dokumente mehr (Abfragen ohne Nutzen)
    assert bool(gebaut) is schreibt
    assert signals.is_auto_indexing_enabled() is schreibt


# --- reindex_elasticsearch -----------------------------------------------------------------------


def _reindex(*argumente: str) -> str:
    ausgabe = StringIO()
    call_command("reindex_elasticsearch", *argumente, stdout=ausgabe)
    return ausgabe.getvalue()


@pytest.mark.django_db
def test_neuaufbau_mit_aktiv_ueber_das_abonnement(
    settings: Any, monkeypatch: pytest.MonkeyPatch, es: FakeElasticsearch, kommune: Kommune
) -> None:
    import elasticsearch

    def _eigener_client(*_: Any, **__: Any) -> None:
        raise AssertionError("reindex_elasticsearch schreibt mit aktiv nicht selbst")

    monkeypatch.setattr(elasticsearch, "Elasticsearch", _eigener_client)
    settings.SEARCH_INDEX_SUBSCRIPTION = "aktiv"
    body, andere = kommune("Gewählt"), kommune("Andere")
    vorgang, neuer, fremd = _vorgang(body), _vorgang(body), _vorgang(andere)
    letzte = nummeriert()
    # Ein Ereignis nach dem Lesen des Neuaufbaus hat eine höhere Folgenummer und gewinnt
    es.ablegen("papers", {"id": str(neuer.pk), "name": "neuerer Stand"}, version=letzte.seq + 5)

    ausgabe = _reindex("--index", "papers", "--body", str(body.pk))

    assert f"Version (Folgenummer) {letzte.seq}" in ausgabe
    assert "papers: indexed 1, stale 1" in ausgabe
    docs = es.indizes["papers"].docs
    assert set(docs) == {str(vorgang.pk), str(neuer.pk)}  # nur die gewählte Kommune
    assert docs[str(vorgang.pk)].version == letzte.seq
    assert docs[str(neuer.pk)].source["name"] == "neuerer Stand"
    assert str(fremd.pk) not in docs
    assert shadow_name("papers") not in es.indizes

    with pytest.raises(CommandError, match="--clear"):
        _reindex("--clear")
    with pytest.raises(CommandError, match="Body not found"):
        _reindex("--body", "keine-kennung")


# --- Zustellung: Schalter aktiv, Zustand schatten -----------------------------------------------


def _abonnement_im_schatten(settings: Any) -> Subscriber:
    """Registriert wie im Schattenbetrieb; die Zeile in der Datenbank steht auf ``schatten``."""
    settings.SEARCH_INDEX_SUBSCRIPTION = "schatten"
    assert subscribers.register()
    spec = get(abonnement.NAME)
    ensure_subscription(spec)
    return spec


@pytest.mark.django_db
def test_aktiv_im_zustand_schatten_haelt_an_und_holt_dann_nach(
    settings: Any, leeres_register: dict[str, Subscriber], es: FakeElasticsearch, kommune: Kommune
) -> None:
    spec = _abonnement_im_schatten(settings)
    zeile = Subscription.objects.get(name=abonnement.NAME)
    assert zeile.state == SubscriptionState.SCHATTEN
    body = kommune("Beispielstadt")
    sitzung = _sitzung(body)
    ereignis = nummeriert(
        type="ris.meeting.changed",
        aggregate_type="Meeting",
        aggregate_id=sitzung.pk,
        tenant_ref=f"source:{uuid.uuid4()}",
        body_id=body.pk,
        visibility="oeffentlich",
        payload={},
    )
    settings.SEARCH_INDEX_SUBSCRIPTION = "aktiv"

    # Die alten Wege schweigen: Ein Schreiben in den Schatten verbrauchte das Ereignis für den Live-Index
    with pytest.raises(TargetUnavailableError):
        deliver_batch(spec)
    assert not es.indizes
    assert Subscription.objects.get(name=abonnement.NAME).cursor_seq == zeile.cursor_seq
    assert not ParkedEvent.objects.exists()

    # Steht das Abonnement auf aktiv (Admin: Pausieren, Fortsetzen (aktiv)), geht derselbe Batch in den Live-Index
    Subscription.objects.filter(name=abonnement.NAME).update(state=SubscriptionState.AKTIV)
    assert deliver_batch(spec).delivered == 1
    gespeichert = es.doc("meetings", sitzung.pk)
    assert gespeichert is not None and gespeichert.version == ereignis.seq
    assert shadow_name("meetings") not in es.indizes


@pytest.mark.django_db
def test_status_warnt_wenn_die_zustellung_anhaelt(settings: Any, es: FakeElasticsearch) -> None:
    Subscription.objects.create(name=abonnement.NAME, cursor_seq=0, state=SubscriptionState.SCHATTEN)
    settings.SEARCH_INDEX_SUBSCRIPTION = "aktiv"

    ausgabe = StringIO()
    call_command("suchindex_schatten", "status", stdout=ausgabe)

    assert "Die Zustellung hält an" in ausgabe.getvalue()

    Subscription.objects.filter(name=abonnement.NAME).update(state=SubscriptionState.AKTIV)
    ausgabe = StringIO()
    call_command("suchindex_schatten", "status", stdout=ausgabe)
    assert "hält an" not in ausgabe.getvalue()
