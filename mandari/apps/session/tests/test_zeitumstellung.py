# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sitzungsformular und Jahresplanung an den Tagen der Zeitumstellung (Issue #974), Europe/Berlin.

- 25.10.2026, 02:30 Uhr (doppelte Stunde): Die Sitzung wird gespeichert, und zwar zur ersten 02:30 Uhr (Sommerzeit);
  ein Hinweis sagt, welche der beiden gilt. Bisher lehnte das Formular die Eingabe ab.
- 28.03.2027, 02:30 Uhr (fehlende Stunde): verständliche Meldung am Feld, nichts wird gespeichert.
- Eine Sitzung, die schon auf der zweiten 02:30 Uhr steht, bleibt dort, wenn jemand anderes an ihr ändert.
- Jahresplanung: Serientermine in der doppelten Stunde mit Hinweis, in der fehlenden Stunde abgelehnt.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from django.contrib.messages import get_messages
from django.test import Client

from apps.common import local_time
from apps.common.tests.factories import UserFactory
from apps.session.models import SessionMeeting, SessionOrganization, SessionRole, SessionTenant, SessionUser

pytestmark = pytest.mark.django_db

#: Erste 02:30 Uhr am 25.10.2026 (Sommerzeit) und zweite (Normalzeit)
ERSTE = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
ZWEITE = datetime(2026, 10, 25, 1, 30, tzinfo=UTC)


@dataclass
class Welt:
    tenant: SessionTenant
    rat: SessionOrganization
    client: Client

    def url(self, pfad: str) -> str:
        return f"/session/{self.tenant.slug}{pfad}"


@pytest.fixture
def welt() -> Welt:
    tenant = SessionTenant.objects.create(name="Stadt Süd", slug="sued")
    rat = SessionOrganization.objects.create(tenant=tenant, name="Rat", short_name="Rat")
    rolle = SessionRole.objects.create(
        tenant=tenant,
        name="Sitzungsdienst",
        can_view_meetings=True,
        can_create_meetings=True,
        can_edit_meetings=True,
        can_view_non_public_meetings=True,
    )
    user = SessionUser.objects.create(user=cast(Any, UserFactory)(), tenant=tenant)
    user.roles.add(rolle)
    client = Client()
    client.force_login(user.user)
    return Welt(tenant=tenant, rat=rat, client=client)


def _meldungen(antwort: Any) -> list[str]:
    return [str(m) for m in get_messages(antwort.wsgi_request)]


def _anlegen(welt: Welt, start: str, **werte: str) -> Any:
    daten = {"name": "Ratssitzung", "organization": str(welt.rat.pk), "start": start, "is_public": "on", **werte}
    return welt.client.post(welt.url("/meetings/create/"), daten)


class TestSitzungsformular:
    def test_doppelte_stunde_wird_mit_hinweis_gespeichert(self, welt: Welt) -> None:
        antwort = _anlegen(welt, "2026-10-25T02:30")

        assert antwort.status_code == 302
        sitzung = SessionMeeting.objects.get(tenant=welt.tenant)
        assert sitzung.start == ERSTE
        assert local_time.ambiguous_notice(ERSTE, label="Beginn") in _meldungen(antwort)

    def test_fehlende_stunde_mit_verstaendlicher_meldung(self, welt: Welt) -> None:
        antwort = _anlegen(welt, "2027-03-28T02:30")

        assert antwort.status_code == 200
        assert not SessionMeeting.objects.filter(tenant=welt.tenant).exists()
        assert antwort.context["form"].errors["start"] == [local_time.nonexistent_message(datetime(2027, 3, 28, 2, 30))]
        assert "etwa 03:30 Uhr" in antwort.content.decode()

    def test_ende_in_der_doppelten_stunde(self, welt: Welt) -> None:
        antwort = _anlegen(welt, "2026-10-24T19:00", end="2026-10-25T02:15")

        assert antwort.status_code == 302
        sitzung = SessionMeeting.objects.get(tenant=welt.tenant)
        assert sitzung.end == datetime(2026, 10, 25, 0, 15, tzinfo=UTC)
        assert any(
            meldung.startswith("Ende: 02:15 Uhr gibt es am 25.10.2026 zweimal") for meldung in _meldungen(antwort)
        )

    @pytest.mark.parametrize("neu", [False, True], ids=["bisher", "neues-design"])
    def test_gespeicherte_zweite_uhrzeit_bleibt_beim_bearbeiten(self, welt: Welt, neu: bool) -> None:
        SessionTenant.objects.filter(pk=welt.tenant.pk).update(session_new_design=neu)
        sitzung = SessionMeeting.objects.create(
            tenant=welt.tenant, name="Nachtsitzung", organization=welt.rat, start=ZWEITE, is_public=True
        )
        # Das Formular zeigt die gespeicherte Ortszeit; abgeschickt wird, was dort steht
        seite = welt.client.get(welt.url(f"/meetings/{sitzung.pk}/edit/")).content.decode()
        assert 'value="2026-10-25T02:30"' in seite
        daten = {
            "name": sitzung.name,
            "organization": str(welt.rat.pk),
            "start": "2026-10-25T02:30",
            "is_public": "on",
            "location": "Feuerwache",
            "format": sitzung.format,
            "meeting_state": sitzung.meeting_state,
        }

        antwort = welt.client.post(welt.url(f"/meetings/{sitzung.pk}/edit/"), daten)

        assert antwort.status_code == 302, antwort.content.decode()[:2000]
        sitzung.refresh_from_db()
        assert (sitzung.location, sitzung.start) == ("Feuerwache", ZWEITE)
        assert not any("zweimal" in meldung for meldung in _meldungen(antwort))


def _planen(welt: Welt, datum: str, uhrzeit: str, action: str = "create") -> Any:
    return welt.client.post(
        welt.url("/meetings/plan/"),
        {
            "organization": str(welt.rat.pk),
            "rhythm": "weekly",
            "weekday": "6",  # Sonntag: die Zeitumstellung ist am letzten Sonntag im März bzw. Oktober
            "time": uhrzeit,
            "date_from": datum,
            "date_to": datum,
            "action": action,
        },
    )


class TestJahresplanung:
    def test_doppelte_stunde_mit_hinweis(self, welt: Welt) -> None:
        antwort = _planen(welt, "2026-10-25", "02:30")

        assert antwort.status_code == 302
        assert list(SessionMeeting.objects.filter(tenant=welt.tenant).values_list("start", flat=True)) == [ERSTE]
        assert local_time.ambiguous_notice(ERSTE) in _meldungen(antwort)

    def test_fehlende_stunde_wird_abgelehnt(self, welt: Welt) -> None:
        antwort = _planen(welt, "2027-03-28", "02:30")

        assert antwort.status_code == 302
        assert not SessionMeeting.objects.filter(tenant=welt.tenant).exists()
        assert _meldungen(antwort) == [local_time.nonexistent_message(datetime(2027, 3, 28, 2, 30))]

    def test_vorschau_nennt_die_gewaehlte_uhrzeit(self, welt: Welt) -> None:
        antwort = _planen(welt, "2026-10-25", "02:30", action="preview")

        assert antwort.status_code == 200
        assert not SessionMeeting.objects.filter(tenant=welt.tenant).exists()
        assert local_time.ambiguous_notice(ERSTE) in _meldungen(antwort)
