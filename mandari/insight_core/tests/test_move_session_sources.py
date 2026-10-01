# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Umzug der Session-Quellen nach einem Domainwechsel (Issue #733): Adressen neu, Kennungen gleich.

``manage.py move_session_sources`` schreibt URL, Adressen, Verweise, Links und Rohdaten unter der alten
Adresse der Schnittstelle auf die neue um und hält die bisherige Basis der Kennungen in der Quelle fest.
"""

from __future__ import annotations

from io import StringIO
from typing import Any

import pytest
from django.core.management import CommandError, call_command
from django.test import override_settings

from insight_core.models import (
    OParlBody,
    OParlConsultation,
    OParlFile,
    OParlMeeting,
    OParlPaper,
    OParlSource,
)

pytestmark = pytest.mark.django_db

ALT = "https://mandari.example/session/nord/api/oparl/"
NEU_SITE = "https://neu.example"
NEU = f"{NEU_SITE}/session/nord/api/oparl/"
FREMD = "https://ris.beispiel.example/oparl/v1/"
MODELLE = (OParlBody, OParlMeeting, OParlPaper, OParlFile, OParlConsultation)


@pytest.fixture
def quelle(db: Any) -> OParlSource:
    """Session-Quelle unter der alten Adresse mit Verweisen, Links und Rohdaten; daneben ein Fremd-RIS."""
    source = OParlSource.objects.create(
        name="Sitzungsdienst Nord", url=ALT, sync_config={"source_type": "oparl", "session_tenant": "nord"}
    )
    body = OParlBody.objects.create(
        external_id=f"{ALT}body/",
        source=source,
        name="Nord",
        slug="nord",
        meeting_list_url=f"{ALT}meetings/",
        website="https://www.nord.example/",
        raw_json={"id": f"{ALT}body/", "meeting": f"{ALT}meetings/", "website": "https://www.nord.example/"},
    )
    sitzung = OParlMeeting.objects.create(
        external_id=f"{ALT}meeting/1/",
        body=body,
        name="Rat",
        raw_json={
            "id": f"{ALT}meeting/1/",
            "organization": [f"{ALT}organization/3/"],
            "auxiliaryFile": [{"id": f"{ALT}file/9/", "accessUrl": f"{ALT}file/9/download/"}],
        },
    )
    vorlage = OParlPaper.objects.create(external_id=f"{ALT}paper/2/", body=body, name="Radweg")
    OParlFile.objects.create(
        external_id=f"{ALT}file/9/",
        body=body,
        meeting=sitzung,
        access_url=f"{ALT}file/9/download/",
        download_url=f"{ALT}file/9/download/?download=1",
    )
    OParlConsultation.objects.create(
        external_id=f"{ALT}consultation/4/",
        body=body,
        paper=vorlage,
        paper_external_id=f"{ALT}paper/2/",
        meeting_external_id=f"{ALT}meeting/1/",
    )
    fremd = OParlSource.objects.create(name="Beispiel-RIS", url=f"{FREMD}system")
    OParlPaper.objects.create(
        external_id=f"{FREMD}paper/1",
        body=OParlBody.objects.create(external_id=f"{FREMD}body/1", source=fremd, name="Bsp", slug="bsp"),
    )
    return source


def _stand() -> dict[str, list[tuple[Any, str]]]:
    return {model.__name__: sorted(model.objects.values_list("id", "external_id")) for model in MODELLE}


def _kennungen() -> dict[str, set[Any]]:
    return {model.__name__: set(model.objects.values_list("id", flat=True)) for model in MODELLE}


def _umziehen(*argumente: str) -> str:
    out = StringIO()
    call_command("move_session_sources", *argumente, stdout=out)
    return out.getvalue()


@override_settings(SITE_URL=NEU_SITE)
def test_vorschau_aendert_nichts(quelle: OParlSource) -> None:
    vorher = _stand()

    ausgabe = _umziehen()

    assert "Vorschau" in ausgabe
    assert f"nord: {ALT} -> {NEU} (würde umziehen: 5 Objekte)" in ausgabe
    assert f"Basis der Kennungen bleibt: {ALT}" in ausgabe
    assert _stand() == vorher
    quelle.refresh_from_db()
    assert quelle.url == ALT


@override_settings(SITE_URL=NEU_SITE)
def test_umzug_schreibt_adressen_um_und_behaelt_die_kennungen(quelle: OParlSource) -> None:
    kennungen = _kennungen()

    ausgabe = _umziehen("--yes")

    assert f"nord: {ALT} -> {NEU} (umgezogen: 5 Objekte)" in ausgabe
    assert _kennungen() == kennungen
    quelle.refresh_from_db()
    assert quelle.url == NEU
    assert quelle.sync_config == {"source_type": "oparl", "session_tenant": "nord", "id_base": ALT}

    body = OParlBody.objects.get(source=quelle)
    assert (body.external_id, body.meeting_list_url, body.website) == (
        f"{NEU}body/",
        f"{NEU}meetings/",
        "https://www.nord.example/",
    )
    assert body.raw_json == {"id": f"{NEU}body/", "meeting": f"{NEU}meetings/", "website": "https://www.nord.example/"}
    sitzung = OParlMeeting.objects.get()
    assert sitzung.raw_json == {
        "id": f"{NEU}meeting/1/",
        "organization": [f"{NEU}organization/3/"],
        "auxiliaryFile": [{"id": f"{NEU}file/9/", "accessUrl": f"{NEU}file/9/download/"}],
    }
    datei = OParlFile.objects.get()
    assert (datei.external_id, datei.access_url, datei.download_url) == (
        f"{NEU}file/9/",
        f"{NEU}file/9/download/",
        f"{NEU}file/9/download/?download=1",
    )
    beratung = OParlConsultation.objects.get()
    assert (beratung.paper_external_id, beratung.meeting_external_id) == (f"{NEU}paper/2/", f"{NEU}meeting/1/")
    # Das Fremd-RIS bleibt, wie es war
    assert OParlPaper.objects.filter(external_id=f"{FREMD}paper/1").exists()

    # Wiederholt: nichts mehr zu tun
    assert f"nord: bereits unter {NEU}" in _umziehen("--yes")


@override_settings(SITE_URL=NEU_SITE)
def test_eine_schon_festgehaltene_basis_bleibt(quelle: OParlSource) -> None:
    """Zog die Quelle schon einmal um, bleibt die ursprüngliche Basis der Kennungen."""
    erste = "https://erste.example/session/nord/api/oparl/"
    quelle.sync_config = {**quelle.sync_config, "id_base": erste}
    quelle.save(update_fields=["sync_config"])

    _umziehen("--yes")

    quelle.refresh_from_db()
    assert (quelle.url, quelle.sync_config["id_base"]) == (NEU, erste)


@override_settings(SITE_URL=NEU_SITE)
def test_leere_quelle_unter_der_neuen_adresse_geht_auf(quelle: OParlSource) -> None:
    """Nach dem Domainwechsel kann die Registrierung eine leere Quelle unter der neuen Adresse anlegen."""
    leer = OParlSource.objects.create(name="Sitzungsdienst Nord", url=NEU, sync_config={"session_tenant": "nord"})

    ausgabe = _umziehen("--yes")

    assert f"Leere Quelle unter der neuen Adresse geht auf: {leer.pk}" in ausgabe
    assert not OParlSource.objects.filter(pk=leer.pk).exists()
    quelle.refresh_from_db()
    assert quelle.url == NEU


@override_settings(SITE_URL=NEU_SITE)
def test_quelle_mit_kommunen_unter_der_neuen_adresse_bricht_ab(quelle: OParlSource) -> None:
    andere = OParlSource.objects.create(name="Doppelt", url=NEU)
    OParlBody.objects.create(external_id="https://x.example/body/", source=andere, name="X", slug="x")
    vorher = _stand()
    out = StringIO()

    with pytest.raises(CommandError, match="ließ sich nicht umziehen"):
        call_command("move_session_sources", "--yes", stdout=out)

    assert "schon eine Quelle mit Kommunen registriert" in out.getvalue()
    assert _stand() == vorher
    quelle.refresh_from_db()
    assert quelle.url == ALT


@override_settings(SITE_URL=NEU_SITE)
def test_objekte_unter_der_neuen_adresse_brechen_ab(quelle: OParlSource) -> None:
    """Hat ein Abgleich schon Objekte unter der neuen Adresse angelegt, erst prüfen."""
    OParlPaper.objects.create(external_id=f"{NEU}paper/7/", body=OParlBody.objects.get(source=quelle))
    out = StringIO()

    with pytest.raises(CommandError):
        call_command("move_session_sources", "--yes", stdout=out)

    assert f"Unter {NEU} stehen schon Objekte (paper)" in out.getvalue()
    quelle.refresh_from_db()
    assert quelle.url == ALT


@override_settings(SITE_URL=NEU_SITE)
def test_nur_ein_mandant(quelle: OParlSource) -> None:
    with pytest.raises(CommandError, match="Keine Session-Quelle"):
        _umziehen("--tenant", "sued", "--yes")
    assert "umgezogen: 5 Objekte" in _umziehen("--tenant", "nord", "--yes")


def test_ohne_session_quelle(db: Any) -> None:
    with pytest.raises(CommandError, match="Keine Session-Quelle"):
        _umziehen()
