# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Das Verzeichnis der verschlüsselten Felder ist vollständig und stimmt.

Der Schlüsselwechsel erfasst nur, was in ``apps/common/crypto_registry.py`` steht. Ein
vergessenes Feld wäre nach dem Wechsel unlesbar – genau das ist früher mit den
Zugangsdaten der Organisationen, den plattformweiten Geheimnissen und dem zweiten
Faktor passiert. Diese Tests schlagen fehl, sobald ein neues verschlüsseltes Feld
nicht eingetragen ist oder ein Eintrag nicht mehr zum Modell passt.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, cast

import pytest
from django.apps import apps
from django.db import models

from apps.common.crypto_registry import (
    ENCRYPTED_FIELDS,
    SELF,
    TENANT_KEY_FIELDS,
    TENANT_MODELS,
    UNENCRYPTED_BINARY_FIELDS,
    EncryptedField,
    KeyKind,
)
from apps.common.encryption import EncryptedTextField
from apps.common.key_rotation import tenant_target

REGISTERED = {entry.label: entry for entry in ENCRYPTED_FIELDS}


def _all_fields() -> list[tuple[str, models.Field[Any, Any]]]:
    return [
        (f"{model._meta.label}.{field.name}", field)
        for model in apps.get_models()
        for field in model._meta.concrete_fields
    ]


def test_jedes_binaerfeld_ist_eingetragen_oder_begruendet() -> None:
    fehlend = [
        label
        for label, field in _all_fields()
        if isinstance(field, models.BinaryField) and label not in REGISTERED and label not in UNENCRYPTED_BINARY_FIELDS
    ]
    assert not fehlend, (
        "Binärfelder ohne Eintrag in apps/common/crypto_registry.py – verschlüsselt (ENCRYPTED_FIELDS "
        f"mit Schlüsselart) oder begründet unverschlüsselt (UNENCRYPTED_BINARY_FIELDS)? {fehlend}"
    )


def test_felder_mit_verschluesselungsnamen_sind_eingetragen() -> None:
    fehlend = [
        label
        for label, field in _all_fields()
        if (field.name.endswith("_encrypted") or field.name in TENANT_KEY_FIELDS) and label not in REGISTERED
    ]
    assert not fehlend, f"Verschlüsselte Felder ohne Eintrag in apps/common/crypto_registry.py: {fehlend}"


def test_encrypted_text_fields_nutzen_den_mandantenschluessel() -> None:
    for label, field in _all_fields():
        if isinstance(field, EncryptedTextField):
            assert label in REGISTERED, f"{label} fehlt im Verzeichnis"
            assert REGISTERED[label].kind is KeyKind.TENANT, f"{label}: EncryptedTextField nutzt den Mandantenschlüssel"


def test_eintraege_passen_zu_den_modellen() -> None:
    doppelt = [label for label, anzahl in Counter(entry.label for entry in ENCRYPTED_FIELDS).items() if anzahl > 1]
    assert not doppelt, f"Mehrfach eingetragen: {doppelt}"
    for entry in ENCRYPTED_FIELDS:
        feld = apps.get_model(entry.model)._meta.get_field(entry.field)
        assert isinstance(feld, models.BinaryField), f"{entry.label} ist kein Binärfeld"
        assert bool(entry.tenant_paths) == (entry.kind is KeyKind.TENANT), f"{entry.label}: Pfad nur bei Mandantendaten"
    for label in UNENCRYPTED_BINARY_FIELDS:
        model_label, field_name = label.rsplit(".", 1)
        assert isinstance(apps.get_model(model_label)._meta.get_field(field_name), models.BinaryField), label
        assert label not in REGISTERED, f"{label} steht in beiden Listen"


def test_mandantenmodelle_tragen_beide_schluesselspalten() -> None:
    for model_label in TENANT_MODELS:
        for field_name in TENANT_KEY_FIELDS:
            assert REGISTERED[f"{model_label}.{field_name}"].kind is KeyKind.TENANT_KEY


def _kette(model: type[models.Model], path: str, mandant: models.Model) -> models.Model:
    """Ungespeicherte Instanzen entlang des Pfads; am Ende steht der Mandant."""
    if path == SELF:
        return mandant
    start = model()
    aktuell: models.Model = start
    teile = path.split("__")
    for nummer, teil in enumerate(teile):
        ziel_modell = cast(type[models.Model], aktuell._meta.get_field(teil).related_model)
        ziel = mandant if nummer == len(teile) - 1 else ziel_modell()
        setattr(aktuell, teil, ziel)
        aktuell = ziel
    return start


def _mandantendaten() -> list[tuple[EncryptedField, str]]:
    return [(entry, path) for entry in ENCRYPTED_FIELDS if entry.kind is KeyKind.TENANT for path in entry.tenant_paths]


@pytest.mark.parametrize(("entry", "path"), _mandantendaten(), ids=lambda wert: getattr(wert, "label", wert))
def test_pfad_zum_mandanten_entspricht_get_encryption_organization(entry: EncryptedField, path: str) -> None:
    """Der Wechsel findet den Mandanten über den Pfad, die Anwendung über get_encryption_organization()."""
    model = apps.get_model(entry.model)
    ziel = tenant_target(model, path)
    assert ziel in TENANT_MODELS, f"{entry.label}: '{path}' endet bei {ziel}, nicht bei einem Mandanten"
    mandant = model() if path == SELF else apps.get_model(ziel)()
    objekt = _kette(model, path, mandant)
    assert cast(Any, objekt).get_encryption_organization() is mandant, (
        f"{entry.label}: Pfad '{path}' führt zu einem anderen Mandanten als get_encryption_organization()"
    )
