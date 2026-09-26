# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Erstes Erzeugen des Mandantenschlüssels: Zwei gleichzeitige erste Verschlüsselungen dürfen
nicht zwei Schlüssel erzeugen. Wer später kommt, übernimmt den bereits gespeicherten
Schlüssel, statt ihn zu überschreiben – sonst wären die Daten des ersten unlesbar.
"""

from __future__ import annotations

from typing import Any, cast
from unittest import mock

import pytest

from apps.common.encryption import TenantEncryption as _TenantEncryption
from apps.common.encryption import decrypt_key
from apps.session.models import SessionTenant
from apps.tenants.models import Organization

pytestmark = pytest.mark.django_db

TenantEncryption = cast(Any, _TenantEncryption)  # Modul ist noch untypisiert


def _gespeichert(model: Any, pk: Any) -> bytes:
    return bytes(cast(Any, model.objects.get(pk=pk)).encryption_key)


def _ohne_schluessel(model: Any, **felder: str) -> tuple[Any, Any]:
    objekt = model.objects.create(**felder)
    model.objects.filter(pk=objekt.pk).update(encryption_key=None)
    return model.objects.get(pk=objekt.pk), model.objects.get(pk=objekt.pk)


@pytest.mark.parametrize(
    ("model", "felder"),
    [
        (Organization, {"name": "Fraktion Race", "slug": "fraktion-race"}),
        (SessionTenant, {"name": "Stadt Race", "slug": "stadt-race"}),
    ],
)
def test_gleichzeitige_erste_erzeugung_ergibt_einen_schluessel(model: Any, felder: dict[str, str]) -> None:
    erste, zweite = _ohne_schluessel(model, **felder)
    verschluesselung_zwei = TenantEncryption(zweite)

    schluessel_eins = TenantEncryption(erste).key
    # Die zweite Anfrage hat den leeren Stand gelesen, bevor die erste speicherte
    with mock.patch.object(zweite, "refresh_from_db"):
        schluessel_zwei = verschluesselung_zwei.key

    assert decrypt_key(_gespeichert(model, erste.pk)) == schluessel_eins
    assert schluessel_zwei == schluessel_eins
    # Mit dem Schlüssel der zweiten Anfrage Verschlüsseltes bleibt mit dem gespeicherten lesbar
    geheim = verschluesselung_zwei.encrypt("Inhalt")
    assert TenantEncryption(model.objects.get(pk=erste.pk)).decrypt(geheim) == "Inhalt"


def test_vorhandener_schluessel_bleibt(org: Organization) -> None:
    vorher = _gespeichert(Organization, org.pk)
    TenantEncryption(Organization.objects.get(pk=org.pk)).encrypt("x")
    assert _gespeichert(Organization, org.pk) == vorher
