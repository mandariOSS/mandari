# SPDX-License-Identifier: AGPL-3.0-or-later
"""
E-Mail-Adressen der Konten ohne Beachtung der Groß-/Kleinschreibung (Issue #1397).

Geprüft wird: Konten speichern die Adresse klein geschrieben (Anlage, ``save``, Formular des Admins); die
Anmeldung findet ein Konto in jeder Schreibweise; zwei Konten aus der Zeit vor dieser Regel, die sich nur in
der Schreibweise unterscheiden, bleiben je mit ihrer eigenen erreichbar und speicherbar; die Migration stellt
den Bestand um und lässt solche Paare unverändert.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from typing import Any, cast

import pytest
from django.apps import apps as django_apps
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse

from apps.accounts.admin import GDPRUserCreationForm
from apps.accounts.models import User
from apps.accounts.tests.test_login_2fa import PASSWORD, is_logged_in, make_user

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _cache_leeren() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


def _altbestand(email: str) -> User:
    """Konto mit gemischter Schreibweise, wie es vor dieser Regel gespeichert wurde (an ``save`` vorbei)."""
    konto = make_user(f"platzhalter-{User.objects.count()}@example.org")
    User.objects.filter(pk=konto.pk).update(email=email)
    konto.refresh_from_db()
    return konto


def _anmelden(email: str, password: str = PASSWORD) -> Client:
    client = Client()
    client.post(reverse("accounts:login"), {"email": email, "password": password})
    return client


def test_anlage_und_speichern_schreiben_die_adresse_klein() -> None:
    angelegt = make_user("  Otto.Gross@Example.ORG ")
    assert angelegt.email == "otto.gross@example.org"

    direkt = User(email="Erika.Muster@Example.org")
    direkt.set_password(PASSWORD)
    direkt.save()
    direkt.refresh_from_db()
    assert direkt.email == "erika.muster@example.org"

    # Ein Speichern einzelner Felder fasst die Adresse nicht an
    direkt.first_name = "Erika"
    direkt.save(update_fields=["first_name"])
    assert User.objects.get(pk=direkt.pk).email == "erika.muster@example.org"


@pytest.mark.parametrize("eingabe", ["otto.gross@example.org", "Otto.Gross@Example.org", "OTTO.GROSS@EXAMPLE.ORG"])
def test_anmeldung_in_jeder_schreibweise(eingabe: str) -> None:
    make_user("Otto.Gross@example.org")
    assert is_logged_in(_anmelden(eingabe))


def test_anmeldung_findet_konten_mit_alter_schreibweise() -> None:
    konto = _altbestand("Otto.Gross@example.org")
    client = _anmelden("otto.gross@example.org")
    assert client.session["_auth_user_id"] == str(konto.pk)
    assert not is_logged_in(_anmelden("otto.gross@example.org", "Falsches-Geheimwort-987!"))


def test_zwei_schreibweisen_aus_dem_bestand_bleiben_getrennt_erreichbar() -> None:
    klein = make_user("otto.gross@example.org")
    gemischt = _altbestand("Otto.Gross@example.org")

    assert User.objects.get_by_natural_key("otto.gross@example.org") == klein
    assert User.objects.get_by_natural_key("Otto.Gross@example.org") == gemischt
    # Eine dritte Schreibweise fällt auf die gespeicherte Form (klein) zurück
    assert User.objects.get_by_natural_key("OTTO.GROSS@example.org") == klein

    # Das Konto mit der alten Schreibweise lässt sich weiter speichern und behält sie
    gemischt.first_name = "Otto"
    gemischt.save()
    gemischt.full_clean()
    assert User.objects.get(pk=gemischt.pk).email == "Otto.Gross@example.org"


def test_mehrdeutige_schreibweise_meldet_niemanden_an() -> None:
    erstes = _altbestand("Otto.Gross@example.org")
    _altbestand("OTTO.gross@example.org")
    with pytest.raises(User.DoesNotExist):
        User.objects.get_by_natural_key("otto.gross@example.org")
    assert User.objects.find_by_email("otto.gross@example.org") is None
    assert not is_logged_in(_anmelden("otto.gross@example.org"))

    # Auch ohne klein geschriebenes Gegenstück behält ein Konto des Paars beim Speichern seine Schreibweise;
    # sonst würde aus der mehrdeutigen Anmeldung still ein Treffer.
    erstes.first_name = "Otto"
    erstes.save()
    erstes.full_clean()
    assert User.objects.get(pk=erstes.pk).email == "Otto.Gross@example.org"
    assert User.objects.find_by_email("otto.gross@example.org") is None

    # Ein drittes Konto in klein geschriebener Form entsteht nicht
    with pytest.raises(IntegrityError), transaction.atomic():
        make_user("otto.gross@example.org")


def test_geaenderte_adresse_darf_kein_neues_paar_bilden() -> None:
    alice = make_user("alice@example.org")
    make_user("bob@example.org")
    _altbestand("Carla@example.org")
    _altbestand("CARLA@example.org")

    for adresse in ("Bob@example.org", "carla@example.org", "Carla@Example.org"):
        alice.email = adresse
        with pytest.raises(ValidationError) as fehler:
            alice.full_clean()
        assert "email" in fehler.value.message_dict
    alice.email = "Bob@example.org"
    with pytest.raises(IntegrityError), transaction.atomic():
        alice.save()
    assert User.objects.get(pk=alice.pk).email == "alice@example.org"

    # Eine freie Adresse geht weiter und wird klein gespeichert
    alice.email = "Alice.Neu@Example.org"
    alice.full_clean()
    alice.save()
    assert User.objects.get(pk=alice.pk).email == "alice.neu@example.org"


def test_admin_legt_kein_zweites_konto_in_anderer_schreibweise_an() -> None:
    make_user("otto.gross@example.org")
    angaben = {"email": "Otto.Gross@Example.org", "password1": PASSWORD, "password2": PASSWORD}
    formular = GDPRUserCreationForm(data={**angaben, "usable_password": "true"})
    assert not formular.is_valid()
    assert "email" in formular.errors

    neu = GDPRUserCreationForm(data={**angaben, "email": "Neu.Hier@Example.org", "usable_password": "true"})
    assert neu.is_valid(), neu.errors
    assert cast(Any, neu.save()).email == "neu.hier@example.org"


def test_migration_stellt_den_bestand_um_und_laesst_paare_stehen(capsys: pytest.CaptureFixture[str]) -> None:
    einzeln = _altbestand("Einzeln.Konto@example.org")
    klein = make_user("paar@example.org")
    gemischt = _altbestand("Paar@example.org")
    doppelt_a = _altbestand("Doppelt@example.org")
    doppelt_b = _altbestand("DOPPELT@example.org")

    migration = importlib.import_module("apps.accounts.migrations.0012_email_kleinschreibung")
    migration.lowercase_emails(django_apps, None)

    adressen = dict(User.objects.values_list("pk", "email"))
    assert adressen[einzeln.pk] == "einzeln.konto@example.org"
    assert adressen[klein.pk] == "paar@example.org"
    assert adressen[gemischt.pk] == "Paar@example.org"
    assert {adressen[doppelt_a.pk], adressen[doppelt_b.pk]} == {"Doppelt@example.org", "DOPPELT@example.org"}
    ausgabe = capsys.readouterr().out
    assert "3 Konten behalten ihre Schreibweise" in ausgabe
    assert "example.org" not in ausgabe
