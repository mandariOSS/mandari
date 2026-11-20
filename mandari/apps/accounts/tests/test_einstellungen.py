# SPDX-License-Identifier: AGPL-3.0-or-later
"""``User.settings`` unter Sperre ändern (Issue #1472): Ein veralteter Stand im Objekt überschreibt nichts."""

from __future__ import annotations

from typing import Any, cast

import pytest

from apps.accounts.einstellungen import einstellung_setzen, einstellungen_aendern
from apps.accounts.models import User
from apps.work.dashboard import hinweise
from apps.work.organization import services

pytestmark = pytest.mark.django_db


@pytest.fixture
def person() -> User:
    return cast(
        User,
        cast(Any, User.objects).create_user(
            email="einstellungen@example.org", password="x" * 32, settings={"vorher": 1}
        ),
    )


def _veraltet(person: User) -> User:
    """Dieselbe Person, wie eine zweite Anfrage sie vor der Änderung der ersten geladen hat."""
    return cast(User, User.objects.get(pk=person.pk))


def test_setzen_behaelt_was_eine_andere_anfrage_dazwischen_schrieb(person: User) -> None:
    erste, zweite = _veraltet(person), _veraltet(person)

    einstellung_setzen(erste, "a", 1)
    einstellung_setzen(zweite, "b", 2)

    person.refresh_from_db()
    assert person.settings == {"vorher": 1, "a": 1, "b": 2}
    assert zweite.settings == person.settings, "das Objekt trägt danach den gespeicherten Stand"


def test_aendern_arbeitet_auf_dem_stand_der_datenbank(person: User) -> None:
    veraltet = _veraltet(person)
    einstellung_setzen(person, "neu", True)

    gesehen: list[dict[str, Any]] = []
    gespeichert = einstellungen_aendern(veraltet, lambda einstellungen: gesehen.append(dict(einstellungen)))

    assert gesehen == [{"vorher": 1, "neu": True}]
    assert gespeichert == {"vorher": 1, "neu": True}


def test_kein_objekt_statt_einstellungen_wird_zu_leerem_stand(person: User) -> None:
    User.objects.filter(pk=person.pk).update(settings=["kein", "objekt"])

    einstellung_setzen(person, "a", 1)

    person.refresh_from_db()
    assert person.settings == {"a": 1}


def test_zurueckstellen_und_profil_ueberschreiben_sich_nicht(person: User) -> None:
    """Schreiber aus Work: Hinweis zurückstellen, dann Profil speichern mit einem Objekt von davor."""
    profil_anfrage = _veraltet(person)
    hinweise.zwei_faktor_spaeter(_veraltet(person))

    services.save_profile_visibility(
        profil_anfrage,
        bio="Ratsmitglied",
        show_email=True,
        show_phone=False,
        preferred_contact="email",
        contact_signal="",
    )

    person.refresh_from_db()
    assert hinweise.ZWEI_FAKTOR_SPAETER_SCHLUESSEL in person.settings
    assert person.settings["profile"]["bio"] == "Ratsmitglied"
    assert person.settings["vorher"] == 1
