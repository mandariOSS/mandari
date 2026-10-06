# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungsvorbereitung im neuen Design (#856) hinter dem Schalter je Organisation.

- Schalter aus: die bisherige Seite unverändert (Vorlage, Alpine-Komponente, kein Link zur neuen Ansicht).
- Schalter an: neue Seite mit derselben Konfiguration (alle Funktionen der bisherigen Seite bleiben erreichbar),
  „Bisherige Ansicht“ über ``?ansicht=bisher`` mit Link zurück.
- Ansehen ändert keine gespeicherten Positionen, Notizen oder Redebeiträge (verschlüsselt, Bestand vorher = nachher).
- Org-Grenze: fremde Sitzungen bleiben unerreichbar.
"""

import json
import re
from types import SimpleNamespace
from typing import Any, cast

import pytest

from apps.common.tests.factories import OrganizationFactory
from apps.work.meetings.models import AgendaItemPosition, AgendaPrivateNote, AgendaSpeechNote
from apps.work.meetings.serializers import set_encrypted
from apps.work.meetings.views import prepare as prepare_view
from apps.work.neues_design import SCHALTER_FELD, neues_design_aktiv
from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlFile,
    OParlMeeting,
    OParlPaper,
    OParlSource,
)

pytestmark = pytest.mark.django_db

RIS = "https://ris.example.org"
CONFIG_RE = re.compile(r'<script[^>]*id="prepare-config"[^>]*>(.*?)</script>', re.S)
NEU = "work/meetings/vorbereitung/seite.html"
BISHER = "work/meetings/prepare.html"


@pytest.fixture
def schalter_an(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neues Design für alle Organisationen an (Übergang bis zum Schalter aus #852)."""
    monkeypatch.setattr(prepare_view, "neues_design_aktiv", lambda organization: True)


@pytest.fixture
def sitzung(org: Any) -> OParlMeeting:
    source = OParlSource.objects.create(name="Test-RIS", url=f"{RIS}/system")
    body = OParlBody.objects.create(external_id=f"{RIS}/body/1", source=source, name="Stadt Test")
    org.body = body
    org.save(update_fields=["body"])
    meeting = OParlMeeting.objects.create(external_id=f"{RIS}/meeting/1", body=body, name="Rat")
    OParlAgendaItem.objects.create(
        external_id=f"{RIS}/agenda/1", meeting=meeting, number="1", name="Eröffnung", order=1
    )
    top = OParlAgendaItem.objects.create(
        external_id=f"{RIS}/agenda/2", meeting=meeting, number="2", name="Radweg Nord", order=2
    )
    OParlAgendaItem.objects.create(
        external_id=f"{RIS}/agenda/3", meeting=meeting, number="NÖ 1", name="Grundstück", order=3, public=False
    )
    vorlage = OParlPaper.objects.create(external_id=f"{RIS}/paper/1", body=body, name="Radweg", reference="V/1")
    OParlFile.objects.create(
        external_id=f"{RIS}/file/1",
        body=body,
        paper=vorlage,
        name="Vorlage V/1",
        mime_type="application/pdf",
        access_url=f"{RIS}/file/1.pdf",
    )
    OParlConsultation.objects.create(
        external_id=f"{RIS}/consultation/1", body=body, paper=vorlage, agenda_item_external_id=top.external_id
    )
    return meeting


def _seite(client: Any, org: Any, meeting: OParlMeeting, query: str = "") -> Any:
    return client.get(f"/work/{org.slug}/meetings/{meeting.id}/prepare/{query}")


def _vorlagen(response: Any) -> set[str]:
    return {t.name for t in response.templates if t.name}


def _config(response: Any) -> dict[str, Any]:
    match = CONFIG_RE.search(response.content.decode())
    assert match, "json_script #prepare-config fehlt"
    return dict(json.loads(match.group(1)))


def test_hilfsfunktion_liest_schalter_der_organisation() -> None:
    assert neues_design_aktiv(None) is False
    assert neues_design_aktiv(SimpleNamespace()) is False
    assert neues_design_aktiv(SimpleNamespace(**{SCHALTER_FELD: False})) is False
    assert neues_design_aktiv(SimpleNamespace(**{SCHALTER_FELD: True})) is True


def test_schalter_aus_zeigt_die_bisherige_seite_unveraendert(
    org: Any, sitzung: OParlMeeting, make_member: Any, client_for: Any
) -> None:
    member = make_member(org, ["meetings.prepare"], email="bisher@example.org")
    response = _seite(client_for(member.user), org, sitzung)
    html = response.content.decode()

    assert response.status_code == 200
    assert BISHER in _vorlagen(response)
    assert NEU not in _vorlagen(response)
    assert 'x-data="preparationApp"' in html
    assert 'x-data="vorbereitung"' not in html
    assert "Zur neuen Ansicht" not in html


def test_schalter_an_zeigt_die_neue_vorbereitung(
    org: Any, sitzung: OParlMeeting, make_member: Any, client_for: Any, schalter_an: None
) -> None:
    member = make_member(org, ["meetings.prepare"], email="neu@example.org")
    response = _seite(client_for(member.user), org, sitzung)
    html = response.content.decode()

    assert response.status_code == 200
    assert NEU in _vorlagen(response)
    assert BISHER not in _vorlagen(response)
    assert 'x-data="vorbereitung"' in html
    # Dieselbe Konfiguration wie die bisherige Seite, dazu die Gliederung öffentlich/nichtöffentlich
    config = _config(response)
    assert [i["name"] for i in config["items"]] == ["Eröffnung", "Radweg Nord", "Grundstück"]
    assert [i["isPublic"] for i in config["items"]] == [True, True, False]
    radweg = config["items"][1]
    assert radweg["paper"]["reference"] == "V/1"
    assert [f["name"] for f in radweg["files"]] == ["Vorlage V/1"]
    assert radweg["files"][0]["isPdf"] is True
    # Leiste: vier gleichrangige Positionen in fester Reihenfolge, die übrigen unter „Andere …“
    leiste = re.findall(r'name="position-leiste" value="(\w+)"', html)
    assert leiste == ["for", "against", "abstain", "open"]
    andere = re.search(r'<option value="">Andere …</option>(.*?)</select>', html, re.S)
    assert andere
    assert re.findall(r'<option value="(\w+)"', andere.group(1)) == ["defer", "refer", "amended", "info"]
    # Funktionen der bisherigen Seite bleiben erreichbar (Auszug; vollständig im E2E-Test)
    for merkmal in (
        'id="reasoning-input"',
        'id="outcome-select"',
        'id="thread-visibility"',
        'id="teleprompter-link"',
        "Sitzungsnotizen",
        "Zusammenfassung",
        "Legende",
        "Anlage hinzufügen",
        "An der Vorlage speichern",
        "Dokument als Redebeitrag verknüpfen",
        "Mit Organisation teilen",
        "Position der Fraktion",
        "Im Beratungsverlauf",
        "Bisherige Ansicht",
    ):
        assert merkmal in html, merkmal
    # Seiten-Templates ohne Inline-Skript und -Stil
    assert "<style" not in html.split("</head>", 1)[1]


def test_bisherige_ansicht_bei_eingeschaltetem_schalter(
    org: Any, sitzung: OParlMeeting, make_member: Any, client_for: Any, schalter_an: None
) -> None:
    member = make_member(org, ["meetings.prepare"], email="zurueck@example.org")
    response = _seite(client_for(member.user), org, sitzung, "?ansicht=bisher")
    html = response.content.decode()

    assert BISHER in _vorlagen(response)
    assert 'x-data="preparationApp"' in html
    assert "Zur neuen Ansicht" in html


def test_ansehen_aendert_keine_gespeicherten_inhalte(
    org: Any, sitzung: OParlMeeting, make_member: Any, client_for: Any, schalter_an: None
) -> None:
    member = make_member(org, ["meetings.prepare"], email="bestand@example.org")
    top = sitzung.agenda_items.get(number="2")
    position = AgendaItemPosition(organization=org, agenda_item=top, position="for", is_final=True, set_by=member)
    set_encrypted(position, "reasoning", "Weil der Radweg fehlt.")
    position.save()
    notiz = AgendaPrivateNote(organization=org, author=member, agenda_item=top)
    set_encrypted(notiz, "content", "Nur für mich")
    notiz.save()
    rede = AgendaSpeechNote(organization=org, author=member, agenda_item=top, title="Rede", is_shared=True)
    set_encrypted(rede, "content", "<p>Sehr geehrte Damen und Herren</p>")
    rede.save()

    def bestand() -> list[Any]:
        return [
            list(
                AgendaItemPosition.objects.values_list("id", "position", "is_final", "reasoning_encrypted", "outcome")
            ),
            list(AgendaPrivateNote.objects.values_list("id", "content_encrypted")),
            list(AgendaSpeechNote.objects.values_list("id", "title", "content_encrypted", "is_shared")),
        ]

    vorher = bestand()
    response = _seite(client_for(member.user), org, sitzung)
    assert response.status_code == 200
    assert bestand() == vorher

    # Die gespeicherten Inhalte erscheinen unverändert in der neuen Ansicht
    radweg = _config(response)["items"][1]
    assert radweg["position"] == "for"
    assert radweg["isFinal"] is True
    assert radweg["reasoning"] == "Weil der Radweg fehlt."
    assert radweg["privateNote"] == "Nur für mich"
    assert radweg["speechTitle"] == "Rede"
    assert radweg["speechShared"] is True


def test_fremde_sitzung_bleibt_unerreichbar(
    org: Any, sitzung: OParlMeeting, make_member: Any, client_for: Any, schalter_an: None
) -> None:
    fremde: Any = cast(Any, OrganizationFactory)(name="Andere Fraktion", slug="andere-fraktion")
    quelle = OParlSource.objects.create(name="Anderes RIS", url="https://anderes.example.org/system")
    fremde.body = OParlBody.objects.create(
        external_id="https://anderes.example.org/body/1", source=quelle, name="Andere Stadt"
    )
    fremde.save(update_fields=["body"])
    member = make_member(fremde, ["meetings.prepare"], email="fremd@example.org")

    response = client_for(member.user).get(f"/work/{fremde.slug}/meetings/{sitzung.id}/prepare/")

    assert response.status_code == 404


def test_ohne_recht_keine_vorbereitung(
    org: Any, sitzung: OParlMeeting, make_member: Any, client_for: Any, schalter_an: None
) -> None:
    member = make_member(org, ["dashboard.view"], email="ohne@example.org")
    response = _seite(client_for(member.user), org, sitzung)
    assert response.status_code in (302, 403)


def test_leerer_redebeitrag_gilt_nicht_als_vorhanden(
    org: Any, sitzung: OParlMeeting, make_member: Any, client_for: Any
) -> None:
    """#887: Beim Öffnen angelegte leere Einträge bleiben gespeichert, zählen aber nicht als Redebeitrag."""
    member = make_member(org, ["meetings.prepare"], email="rede@example.org")
    leer_top, voll_top = sitzung.agenda_items.get(number="1"), sitzung.agenda_items.get(number="2")
    leer = AgendaSpeechNote(organization=org, author=member, agenda_item=leer_top)
    set_encrypted(leer, "content", "<p></p>")
    leer.save()
    voll = AgendaSpeechNote(organization=org, author=member, agenda_item=voll_top)
    set_encrypted(voll, "content", "<p>Sehr geehrte Damen und Herren</p>")
    voll.save()
    vorher = list(AgendaSpeechNote.objects.order_by("id").values_list("id", "content_encrypted"))

    items = _config(_seite(client_for(member.user), org, sitzung))["items"]

    assert [i["hasSpeechNote"] for i in items[:2]] == [False, True]
    assert list(AgendaSpeechNote.objects.order_by("id").values_list("id", "content_encrypted")) == vorher
