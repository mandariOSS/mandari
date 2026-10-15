# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Personenfotos ruhen wie Dokument-Cache und Vorschau (``docs/ABRUFREGELN.md``, Issue #1027): kein Abruf, wenn die
Quelle abgeschaltet ist, keine Dateien liefern soll (``sync_config["file_downloads"] = false``) oder geschont wird
(vorher liefen sie montags trotzdem). Keine Abrufe fremder Server: Antworten kommen aus httpx.MockTransport.
"""

from __future__ import annotations

import uuid
from io import StringIO
from typing import Any

import httpx
import pytest
from django.core.management import call_command

from insight_core.models import OParlBody, OParlPerson, OParlSource
from insight_core.services import host_pacing, person_photos

pytestmark = pytest.mark.django_db


def _kommune(**quelle: Any) -> OParlBody:
    source = OParlSource.objects.create(
        name=f"Quelle {uuid.uuid4().hex[:6]}",
        url=f"https://rat.example.de/oparl/{uuid.uuid4().hex[:6]}/system",
        **quelle,
    )
    return OParlBody.objects.create(
        source=source,
        external_id=f"https://rat.example.de/oparl/bodies/{uuid.uuid4()}",
        name="Beispiel",
        person_photo_url_template="https://rat.example.de/fotos/{id}.jpg",
        person_photo_id_pattern=r"/persons/(\d+)$",
    )


def _person(body: OParlBody) -> OParlPerson:
    return OParlPerson.objects.create(
        body=body, external_id=f"https://rat.example.de/oparl/persons/{uuid.uuid4().int % 10**6}", name="Beispiel"
    )


class TestPersonenfotos:
    @pytest.mark.parametrize(
        ("quelle", "erwartet"),
        [
            ({"sync_config": {"file_downloads": False}}, "disabled"),
            ({"is_active": False}, "disabled"),
            ({"consecutive_failures": 3}, "paused"),
        ],
    )
    def test_ruhen_wie_der_dokument_cache(
        self, quelle: dict[str, Any], erwartet: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def kein_abruf(*_a: Any, **_kw: Any) -> Any:
            raise AssertionError("Fotos dieser Quelle dürfen nicht abgerufen werden")

        monkeypatch.setattr(httpx, "Client", kein_abruf)
        monkeypatch.setattr(host_pacing, "wait", kein_abruf)
        body = _kommune(**quelle)
        person = _person(body)
        assert person.photo_url and person.photo_url.startswith("https://rat.example.de/fotos/")

        assert person_photos.fetch_photos_for_body(body, sleep=0) == {erwartet: 1}
        assert person_photos.fetch_person_photo(person) == erwartet
        person.refresh_from_db()
        assert person.photo_fetched_at is None and not person.photo_error, "nichts vermerkt, später neu versucht"

    def test_befehl_fasst_ruhende_quellen_nicht_an(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def kein_abruf(*_a: Any, **_kw: Any) -> Any:
            raise AssertionError("Fotos dieser Quelle dürfen nicht abgerufen werden")

        monkeypatch.setattr(httpx, "Client", kein_abruf)
        _person(_kommune(sync_config={"file_downloads": False}))
        out = StringIO()
        call_command("fetch_person_photos", "--trotz-zeitplan", stdout=out)
        assert "disabled=1" in out.getvalue()

    def test_aktive_quelle_laedt_weiter(self) -> None:
        person = _person(_kommune())
        gesehen: list[str] = []

        def foto(request: httpx.Request) -> httpx.Response:
            gesehen.append(str(request.url))
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(foto))
        assert person_photos.fetch_person_photo(person, client=client) == "missing"
        assert gesehen == [person.photo_url]
