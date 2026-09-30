# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Die eine Serialisierung der offenen Schnittstelle (``hub.api.serialization``): Zeitfilter, Blättern,
Listen-Hülle, Gelöschtes neben Bestehendem, Antwort einer Liste.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import connection
from django.test import RequestFactory, override_settings
from django.test.utils import CaptureQueriesContext

from hub.api.http import BadRequestError
from hub.api.serialization import (
    TIME_FILTERS,
    Gone,
    MergedEntries,
    TimeFilters,
    list_envelope,
    list_response,
    page_number,
    single_page,
)
from insight_core.models import OParlBody, OParlMeeting, OParlPaper, OParlSource

BASIS = "https://mandari.example/oparl/v1/body/1/papers"
T0 = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)


def _anfrage(query: str = "") -> Any:
    return RequestFactory().get(f"/liste?{query}" if query else "/liste")


@pytest.fixture(autouse=True)
def _leerer_cache() -> Any:
    cache.clear()
    yield
    cache.clear()


# =============================================================================
# Zeitfilter
# =============================================================================


def test_zeitfilter_liest_die_vier_parameter_mit_zeitzone() -> None:
    filter_ = TimeFilters.from_request(
        _anfrage("modified_since=2026-09-01T10:00:00%2B02:00&created_until=2026-09-02T00:00:00Z&page=2&x=1")
    )

    assert filter_.sent == {"created_until": "2026-09-02T00:00:00Z", "modified_since": "2026-09-01T10:00:00+02:00"}
    assert filter_.parsed["modified_since"] == T0
    assert filter_.parsed["created_until"] == datetime(2026, 9, 2, tzinfo=UTC)
    assert filter_.incremental
    assert bool(filter_)


def test_ohne_zeitfilter_ist_die_liste_nicht_inkrementell() -> None:
    filter_ = TimeFilters.from_request(_anfrage("page=3"))

    assert not filter_ and not filter_.incremental and filter_.sent == {}
    assert not TimeFilters.requested(_anfrage("page=3"))
    assert TimeFilters.requested(_anfrage("created_since=quatsch"))
    # Nur modified_since macht die Liste inkrementell
    assert not TimeFilters.from_request(_anfrage("modified_until=2026-09-02T00:00:00Z")).incremental


@pytest.mark.parametrize("name", TIME_FILTERS)
def test_zeitfilter_ohne_zeitzone_wird_abgelehnt(name: str) -> None:
    with pytest.raises(BadRequestError) as fehler:
        TimeFilters.from_request(_anfrage(f"{name}=2026-09-01T10:00:00"))
    assert name in fehler.value.message and "Zeitzone" in fehler.value.message


def test_zeitfilter_ohne_gueltigen_zeitpunkt_nennt_den_ersten_fehlerhaften_parameter() -> None:
    with pytest.raises(BadRequestError) as fehler:
        TimeFilters.from_request(_anfrage("modified_since=gestern&created_since=vorgestern"))
    # Geprüft wird in fester Reihenfolge (created_* vor modified_*), nicht in der der Anfrage
    assert "'created_since': 'vorgestern'" in fehler.value.message


@pytest.mark.django_db
def test_zeitfilter_wirken_mit_den_vergleichen_der_ausgabe() -> None:
    body = _body()
    alt = OParlPaper.objects.create(external_id="p/alt", body=body, name="alt", oparl_modified=T0)
    neu = OParlPaper.objects.create(external_id="p/neu", body=body, name="neu", oparl_modified=T0 + timedelta(days=2))
    filter_ = TimeFilters.from_request(_anfrage("modified_since=2026-09-02T00:00:00Z"))

    gefiltert = filter_.apply(OParlPaper.objects.all(), {"modified_since": "oparl_modified__gte"})

    assert list(gefiltert) == [neu]
    assert alt not in gefiltert


# =============================================================================
# Blättern und Hülle
# =============================================================================


@pytest.mark.parametrize(("query", "nummer"), [("", 1), ("page=1", 1), ("page=7", 7)])
def test_seitennummer(query: str, nummer: int) -> None:
    assert page_number(_anfrage(query)) == nummer


@pytest.mark.parametrize("wert", ["0", "-1", "abc", "1.5", ""])
def test_ungueltige_seitennummer_wird_abgelehnt(wert: str) -> None:
    with pytest.raises(BadRequestError) as fehler:
        page_number(_anfrage(f"page={wert}"))
    assert "page" in fehler.value.message


def test_huelle_mit_blaetter_links_und_link_header() -> None:
    paginator: Paginator[int] = Paginator(list(range(5)), 2)
    huelle, kopf = list_envelope(
        BASIS, {"modified_since": "2026-09-01T10:00:00+02:00"}, paginator, paginator.page(2), []
    )

    filter_ = "modified_since=2026-09-01T10%3A00%3A00%2B02%3A00"
    assert huelle["pagination"] == {"totalElements": 5, "elementsPerPage": 2, "currentPage": 2, "totalPages": 3}
    # Seite 1 ohne page-Parameter: eine Schreibweise je Adresse; der Filter bleibt in jedem Link
    assert huelle["links"] == {
        "first": f"{BASIS}?{filter_}",
        "self": f"{BASIS}?{filter_}&page=2",
        "prev": f"{BASIS}?{filter_}",
        "next": f"{BASIS}?{filter_}&page=3",
        "last": f"{BASIS}?{filter_}&page=3",
    }
    assert list(huelle) == ["data", "pagination", "links"]
    assert kopf == {
        "Link": (
            f'<{BASIS}?{filter_}>; rel="first", <{BASIS}?{filter_}>; rel="prev", '
            f'<{BASIS}?{filter_}&page=3>; rel="next", <{BASIS}?{filter_}&page=3>; rel="last"'
        )
    }


def test_huelle_ohne_filter_auf_einer_seite() -> None:
    paginator: Paginator[int] = Paginator([1], 100)
    huelle, kopf = list_envelope(BASIS, {}, paginator, paginator.page(1), [{"id": "x"}])

    assert huelle["links"] == {"first": BASIS, "self": BASIS, "last": BASIS}
    assert kopf == {"Link": f'<{BASIS}>; rel="first", <{BASIS}>; rel="last"'}


@override_settings(OPARL_API_PAGE_SIZE=25)
def test_liste_auf_einer_seite_hat_dieselbe_form_wie_jede_externe_liste() -> None:
    huelle = single_page(BASIS, [{"id": "x"}])

    assert huelle == {
        "data": [{"id": "x"}],
        "pagination": {"totalElements": 1, "elementsPerPage": 25, "currentPage": 1, "totalPages": 1},
        "links": {"first": BASIS, "self": BASIS, "last": BASIS},
    }


# =============================================================================
# Gelöschtes neben Bestehendem
# =============================================================================


def _body() -> OParlBody:
    source = OParlSource.objects.create(name="Musterstadt", url="https://ris.example/oparl/system")
    return OParlBody.objects.create(external_id="https://ris.example/oparl/body/1", source=source, name="Musterstadt")


def _zusammengefuehrt() -> tuple[MergedEntries, dict[str, Any]]:
    """Vorlagen als Bestehendes, gelöschte Sitzungen als Gelöschtes – abwechselnd in der Zeit."""
    body = _body()
    eintraege: dict[str, Any] = {}
    for nummer in range(4):
        vorlage = OParlPaper.objects.create(external_id=f"p/{nummer}", body=body, name=f"Vorlage {nummer}")
        OParlPaper.objects.filter(pk=vorlage.pk).update(updated_at=T0 + timedelta(hours=2 * nummer))
        eintraege[f"vorlage{nummer}"] = vorlage
        sitzung = OParlMeeting.objects.create(
            external_id=f"m/{nummer}", body=body, deleted=True, deleted_at=T0 + timedelta(hours=2 * nummer + 1)
        )
        eintraege[f"geloescht{nummer}"] = sitzung
    return MergedEntries(OParlPaper.objects.all(), OParlMeeting.objects.filter(deleted=True)), eintraege


@pytest.mark.django_db
def test_zusammengefuehrte_folge_ist_nach_zeit_sortiert() -> None:
    folge, eintraege = _zusammengefuehrt()

    assert len(folge) == folge.count() == 8
    seite = folge[0:8]
    erwartet: list[Any] = []
    for nummer in range(4):
        erwartet += [eintraege[f"vorlage{nummer}"], Gone(eintraege[f"geloescht{nummer}"])]
    assert seite == erwartet


@pytest.mark.django_db
def test_seiten_der_zusammengefuehrten_folge_schliessen_aneinander_an() -> None:
    folge, _ = _zusammengefuehrt()
    paginator: Paginator[Any] = Paginator(cast(Any, folge), 3)

    seiten = [list(paginator.page(nummer).object_list) for nummer in paginator.page_range]

    assert [len(seite) for seite in seiten] == [3, 3, 2]
    assert [eintrag for seite in seiten for eintrag in seite] == folge[0:8]


@pytest.mark.django_db
def test_bei_gleichem_zeitpunkt_steht_bestehendes_vor_geloeschtem() -> None:
    body = _body()
    vorlage = OParlPaper.objects.create(external_id="p/1", body=body)
    OParlPaper.objects.filter(pk=vorlage.pk).update(updated_at=T0)
    sitzung = OParlMeeting.objects.create(external_id="m/1", body=body, deleted=True, deleted_at=T0)

    folge = MergedEntries(OParlPaper.objects.all(), OParlMeeting.objects.filter(deleted=True))

    assert folge[0:2] == [vorlage, Gone(sitzung)]


@pytest.mark.django_db
def test_zusammenfuehren_laedt_nur_die_eintraege_der_seite() -> None:
    folge, _ = _zusammengefuehrt()

    with CaptureQueriesContext(connection) as erfasst:
        seite = folge[2:4]

    # je Quelle Zeitstempel und Kennung, dann die Einträge der Seite
    assert len(erfasst) == 4
    assert len(seite) == 2
    with pytest.raises(TypeError):
        folge[0]  # type: ignore[index]


# =============================================================================
# Antwort einer Liste
# =============================================================================


def _abbilden(aufrufe: list[list[int]]) -> Any:
    def render(eintraege: list[int]) -> list[dict[str, Any]]:
        aufrufe.append(eintraege)
        return [{"id": f"{BASIS}/{eintrag}"} for eintrag in eintraege]

    return render


@override_settings(OPARL_API_PAGE_SIZE=2)
def test_antwort_bildet_genau_die_eintraege_der_seite_ab() -> None:
    aufrufe: list[list[int]] = []
    anfrage = _anfrage("page=2")

    antwort = list_response(anfrage, BASIS, [1, 2, 3, 4, 5], TimeFilters.from_request(anfrage), _abbilden(aufrufe))

    assert antwort.status_code == 200
    daten = json.loads(antwort.content)
    assert [eintrag["id"] for eintrag in daten["data"]] == [f"{BASIS}/3", f"{BASIS}/4"]
    assert daten["links"]["next"] == f"{BASIS}?page=3"
    assert 'rel="next"' in antwort["Link"] and antwort["ETag"]
    # ein Aufruf je Seite, damit Verweise gesammelt aufgelöst werden können
    assert aufrufe == [[3, 4]]


@override_settings(OPARL_API_PAGE_SIZE=2)
def test_seite_hinter_der_letzten_ergibt_404_ohne_abbildung() -> None:
    aufrufe: list[list[int]] = []
    anfrage = _anfrage("page=9")

    antwort = list_response(anfrage, BASIS, [1, 2, 3], TimeFilters.from_request(anfrage), _abbilden(aufrufe))

    assert antwort.status_code == 404
    assert json.loads(antwort.content) == {"error": "Seite 9 existiert nicht (letzte Seite: 2).", "status": 404}
    assert aufrufe == []


def test_leere_liste_hat_eine_leere_erste_seite() -> None:
    anfrage = _anfrage()

    antwort = list_response(anfrage, BASIS, [], TimeFilters.from_request(anfrage), _abbilden([]))

    assert antwort.status_code == 200
    assert json.loads(antwort.content)["pagination"]["totalElements"] == 0


def test_ungefilterte_seite_wird_kurz_zwischengespeichert_gefilterte_nie() -> None:
    aufrufe: list[list[int]] = []
    anfrage = _anfrage()
    gefiltert = _anfrage("modified_since=2026-09-01T00:00:00Z")

    erste = list_response(anfrage, BASIS, [1], TimeFilters.from_request(anfrage), _abbilden(aufrufe), cache_seconds=60)
    zweite = list_response(anfrage, BASIS, [2], TimeFilters.from_request(anfrage), _abbilden(aufrufe), cache_seconds=60)

    assert zweite.content == erste.content and zweite["Link"] == erste["Link"] and zweite["ETag"] == erste["ETag"]
    assert aufrufe == [[1]]
    for _ in range(2):
        list_response(gefiltert, BASIS, [3], TimeFilters.from_request(gefiltert), _abbilden(aufrufe), cache_seconds=60)
    assert aufrufe == [[1], [3], [3]]
    # ohne cache_seconds nie
    list_response(anfrage, BASIS, [4], TimeFilters.from_request(anfrage), _abbilden(aufrufe))
    assert aufrufe[-1] == [4]
