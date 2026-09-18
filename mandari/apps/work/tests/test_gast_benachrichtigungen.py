# SPDX-License-Identifier: AGPL-3.0-or-later
"""Gäste pollen die Benachrichtigungsglocke wie alle anderen: JSON statt Umleitung auf die Gast-Übersicht."""

from __future__ import annotations

from typing import Any

import pytest


@pytest.mark.django_db
def test_gast_bekommt_json_fuer_die_glocke(org: Any, make_member: Any, client_for: Any) -> None:
    gast = make_member(org, [])
    gast.is_guest = True
    gast.save(update_fields=["is_guest"])
    client = client_for(gast.user)
    for pfad in ("notifications/count/", "notifications/latest/"):
        antwort = client.get(f"/work/{org.slug}/{pfad}")
        assert antwort.status_code == 200, pfad
        assert antwort["Content-Type"].startswith("application/json"), pfad
    assert client.get(f"/work/{org.slug}/notifications/count/").json()["count"] == 0
