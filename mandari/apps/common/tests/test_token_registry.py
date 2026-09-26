# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Verzeichnis der Token-Felder und Hilfsfunktionen aus ``apps/common/tokens.py``.

Jedes Text-Feld eines Modells, dessen Name „token“ enthält, steht entweder in ``HASHED_TOKEN_FIELDS``
oder mit Begründung in ``PLAINTEXT_TOKEN_FIELDS``. Ein neues Token-Feld fällt hier auf, bevor es im
Klartext in Produktion geht.
"""

from __future__ import annotations

import hmac
from typing import Any

import pytest
from django.apps import apps
from django.db import models

from apps.common import tokens


def _feld(label: str) -> Any:
    app_label, model_name, field_name = label.split(".")
    return apps.get_model(app_label, model_name)._meta.get_field(field_name)


def test_jedes_token_feld_ist_eingeordnet() -> None:
    gefunden = {
        f"{model._meta.app_label}.{model.__name__}.{field.name}"
        for model in apps.get_models()
        for field in model._meta.get_fields()
        if "token" in field.name and isinstance(field, models.CharField | models.UUIDField | models.TextField)
    }
    eingeordnet = tokens.HASHED_TOKEN_FIELDS | set(tokens.PLAINTEXT_TOKEN_FIELDS)
    assert gefunden - eingeordnet == set(), "Token-Feld ohne Entscheidung: hashen oder begründen"


def test_verzeichnis_ohne_doppelte_und_verwaiste_eintraege() -> None:
    assert tokens.HASHED_TOKEN_FIELDS.isdisjoint(tokens.PLAINTEXT_TOKEN_FIELDS)
    for label in tokens.HASHED_TOKEN_FIELDS | set(tokens.PLAINTEXT_TOKEN_FIELDS):
        _feld(label)  # wirft, wenn das Feld nicht (mehr) existiert
    assert all(len(grund) > 40 for grund in tokens.PLAINTEXT_TOKEN_FIELDS.values())


@pytest.mark.parametrize("label", sorted(tokens.HASHED_TOKEN_FIELDS))
def test_gehashte_felder_speichern_nie_klartext_per_default(label: str) -> None:
    feld = _feld(label)
    assert isinstance(feld, models.CharField) and feld.max_length == tokens.HASH_LENGTH
    # Ein Default darf höchstens einen Hash erzeugen, nie ein nutzbares Token
    assert feld.default in (models.NOT_PROVIDED, tokens.unusable_token_hash)


def test_hash_ist_sha256_hex() -> None:
    assert tokens.hash_token("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert len(tokens.unusable_token_hash()) == tokens.HASH_LENGTH
    assert tokens.unusable_token_hash() != tokens.unusable_token_hash()


def test_neues_token_hat_256_bit() -> None:
    token = tokens.new_token()
    assert len(token) == 43 and token != tokens.new_token()


@pytest.mark.parametrize("falsch", [None, "", "x" * (tokens.MAX_TOKEN_LENGTH + 1), 42, "anderes-token"])
def test_token_matches_lehnt_falsches_ab(falsch: object) -> None:
    gespeichert = tokens.hash_token("richtiges-token")
    assert tokens.token_matches("richtiges-token", gespeichert)
    assert not tokens.token_matches(falsch, gespeichert)
    assert not tokens.token_matches("richtiges-token", "")


def test_vergleich_in_konstanter_zeit(monkeypatch: pytest.MonkeyPatch) -> None:
    aufrufe: list[tuple[str, str]] = []
    original = hmac.compare_digest

    def mitschneiden(a: str, b: str) -> bool:
        aufrufe.append((a, b))
        return bool(original(a, b))

    monkeypatch.setattr(hmac, "compare_digest", mitschneiden)
    assert tokens.token_matches("t", tokens.hash_token("t"))
    assert aufrufe == [(tokens.hash_token("t"), tokens.hash_token("t"))]
