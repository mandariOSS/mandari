# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sitzungsort im Spiegel: gleiche Abbildung wie im Ingestor (ingestor/tests/test_session_ort.py)."""

from __future__ import annotations

from typing import Any, cast

import pytest

from insight_core.models import OParlBody, OParlMeeting, OParlSource
from insight_sync.session_mirror import SessionMirror

BASIS = "https://mandari.example/session/nord/api/oparl/"

pytestmark = pytest.mark.django_db


def _spiegeln(**felder: Any) -> OParlMeeting:
    source = OParlSource.objects.create(name="Nord (Session)", url=BASIS)
    body = OParlBody.objects.create(external_id=BASIS + "body/", source=source, name="Nord", slug="nord")
    mirror = cast(Any, SessionMirror)(source, fetch=lambda url: {})
    mirror._upsert_meeting(body, {"id": f"{BASIS}meeting/1/", "name": "Rat", **felder})
    return OParlMeeting.objects.get(external_id=f"{BASIS}meeting/1/")


def test_ort_und_anschrift() -> None:
    meeting = _spiegeln(
        **{
            "mandari:locationName": "Rathaus",
            "mandari:locationRoom": "Ratssaal",
            "mandari:locationAddress": "Markt 1, 12345 Musterstadt",
        }
    )
    assert meeting.location_name == "Rathaus"
    assert meeting.location_address == "Markt 1, 12345 Musterstadt"


def test_nur_raum_ergibt_ortsnamen() -> None:
    meeting = _spiegeln(**{"mandari:locationRoom": "Sitzungszimmer 2"})
    assert meeting.location_name == "Sitzungszimmer 2"
    assert meeting.location_address is None
