# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Befehle aus der eigenen Oberfläche (Issue #1588, ``hub.commands.oberflaeche``): ``ausfuehren`` liefert Quittung oder
Problem, ``Ergebnis`` übersetzt die Fehler für Meldungen und Formulare.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, cast

import pytest
from django import forms
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory

from apps.accounts.models import User
from apps.common.api.idempotency import FORM_FIELD
from apps.common.templatetags.befehle import idempotenzschluessel
from apps.common.tests.factories import UserFactory
from hub.commands import Command, CommandError, Dispatcher, Ergebnis, HandlerResult, Problem, ausfuehren
from hub.commands import dispatcher as dispatcher_modul
from hub.commands.channel import INTERN, OBERFLAECHE, Channel, current_channel
from hub.commands.oberflaeche import CHECK_INPUT, RESENT, TRY_LATER
from hub.commands.problems import CONTRACT_VIOLATED, validation_problem
from hub.commands.tests.hilfen import DOCUMENT, TENANT, register_mit_testvertraegen

AUFRUFE: list[tuple[Command, Channel]] = []


def einreichen(command: Command) -> HandlerResult:
    AUFRUFE.append((command, current_channel()))
    if command.body["title"].strip() == "":
        raise CommandError.invalid("Pflichtangaben dürfen nicht nur aus Leerzeichen bestehen.", {"title": "ist leer"})
    return HandlerResult(reference=f"A-{len(AUFRUFE)}")


@pytest.fixture
def dispatcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dispatcher:
    AUFRUFE.clear()
    dispatcher = Dispatcher(register_mit_testvertraegen(tmp_path))
    dispatcher.register("submission.submit", 1, einreichen)
    monkeypatch.setattr(dispatcher_modul, "_DEFAULT", dispatcher)
    return dispatcher


def _anfrage(daten: dict[str, str] | None = None, user: Any = None) -> Any:
    anfrage = RequestFactory().post("/session/musterstadt/x/", daten or {})
    anfrage.user = user or AnonymousUser()
    return anfrage


# =============================================================================
# ausfuehren
# =============================================================================


@pytest.mark.django_db
class TestAusfuehren:
    def test_quittung_im_weg_der_oberflaeche(self, dispatcher: Dispatcher) -> None:
        person = cast(User, UserFactory(email="erika@example.org"))  # type: ignore[no-untyped-call]
        ergebnis = ausfuehren(
            _anfrage(user=person), "submission.submit", {"document": DOCUMENT, "title": "Bänke"}, mandant=TENANT
        )

        assert ergebnis.ok and ergebnis.problem is None and ergebnis.meldung == ""
        assert ergebnis.quittung is not None and ergebnis.quittung.reference == "A-1"
        command, weg = AUFRUFE[0]
        assert command.actor_ref == f"user:{person.pk}"
        assert command.tenant_ref == TENANT
        assert weg == Channel(OBERFLAECHE)
        # Außerhalb eines Befehls: ein anderes Modul
        assert current_channel() == Channel(INTERN)

    def test_ohne_anmeldung_ohne_ausloeser(self, dispatcher: Dispatcher) -> None:
        ausfuehren(_anfrage(), "submission.submit", {"document": DOCUMENT, "title": "Bänke"}, mandant=TENANT)
        assert AUFRUFE[0][0].actor_ref is None

    def test_doppelt_abgeschicktes_formular_wirkt_einmal(self, dispatcher: Dispatcher) -> None:
        schluessel = str(uuid.uuid4())
        inhalt = {"document": DOCUMENT, "title": "Bänke"}
        erste = ausfuehren(_anfrage({FORM_FIELD: schluessel}), "submission.submit", inhalt, mandant=TENANT)
        zweite = ausfuehren(_anfrage({FORM_FIELD: schluessel}), "submission.submit", inhalt, mandant=TENANT)

        assert erste.quittung == zweite.quittung
        assert len(AUFRUFE) == 1
        assert AUFRUFE[0][0].idempotency_key == schluessel
        # Mit anderem Inhalt: Das Formular wurde schon gesendet
        anders = ausfuehren(
            _anfrage({FORM_FIELD: schluessel}), "submission.submit", {**inhalt, "title": "Anders"}, mandant=TENANT
        )
        assert not anders.ok and anders.meldung == RESENT

    def test_ohne_formularfeld_je_aufruf_ein_neuer_schluessel(self, dispatcher: Dispatcher) -> None:
        inhalt = {"document": DOCUMENT, "title": "Bänke"}
        for daten in ({}, {FORM_FIELD: "mit leerzeichen"}):
            ausfuehren(_anfrage(daten), "submission.submit", inhalt, mandant=TENANT)
        assert len(AUFRUFE) == 2
        assert AUFRUFE[0][0].idempotency_key != AUFRUFE[1][0].idempotency_key

    def test_fehler_des_handlers_kommen_als_ergebnis(self, dispatcher: Dispatcher) -> None:
        ergebnis = ausfuehren(_anfrage(), "submission.submit", {"document": DOCUMENT, "title": "  "}, mandant=TENANT)

        assert not ergebnis.ok and ergebnis.problem is not None
        assert ergebnis.problem.status == 422
        assert ergebnis.feldfehler == {"title": ["ist leer"]}


def test_template_tag_setzt_ein_neues_feld() -> None:
    erstes, zweites = str(idempotenzschluessel()), str(idempotenzschluessel())
    assert erstes.startswith(f'<input type="hidden" name="{FORM_FIELD}" value="')
    assert erstes != zweites


# =============================================================================
# Ergebnis: Meldungen und Formulare
# =============================================================================


def _vertragsfehler(*verstoesse: str, befehl: str = "session.textblock.create") -> Ergebnis:
    return Ergebnis(befehl=befehl, version=1, problem=validation_problem(verstoesse))


class TestErgebnis:
    def test_regeln_des_vertrags_werden_saetze_am_feld(self) -> None:
        ergebnis = _vertragsfehler(
            "$: Pflichtfeld fehlt (content, title)",
            "$.order: verletzt „maximum“",
            "$.category: verletzt „enum“",
            "$.is_active: verletzt „type“",
        )
        assert ergebnis.feldfehler == {
            "content": ["fehlt"],
            "title": ["fehlt"],
            "order": ["ist zu groß"],
            "category": ["hat einen nicht vorgesehenen Wert"],
            "is_active": ["ist ungültig"],
        }

    def test_meldung_mit_den_namen_aus_dem_vertrag(self) -> None:
        ergebnis = _vertragsfehler("$.title: verletzt „maxLength“", "$: Pflichtfeld fehlt (content)")
        assert ergebnis.meldung == f"{CHECK_INPUT}: Titel ist zu lang; Text fehlt."

    def test_ohne_namen_im_vertrag_der_feldname(self) -> None:
        ergebnis = _vertragsfehler("$.title: verletzt „minLength“", befehl="submission.submit")
        assert ergebnis.meldung == f"{CHECK_INPUT}: title ist leer oder zu kurz."

    def test_nicht_vorgesehene_felder_ohne_feld(self) -> None:
        ergebnis = _vertragsfehler("$: nicht vorgesehene Felder (extra)")
        assert ergebnis.feldfehler == {"": ["Das Formular enthält nicht vorgesehene Angaben."]}

    def test_fehler_des_handlers_mit_eigener_meldung(self) -> None:
        problem = CommandError.invalid(
            "Pflichtangaben dürfen nicht nur aus Leerzeichen bestehen.", {"title": "ist leer"}
        )
        ergebnis = Ergebnis(befehl="session.textblock.create", version=1, problem=problem.problem)
        assert ergebnis.meldung == "Pflichtangaben dürfen nicht nur aus Leerzeichen bestehen: Titel ist leer."
        assert problem.problem.errors == ({"pointer": "/title", "detail": "ist leer"},)

    @pytest.mark.parametrize(
        ("problem", "meldung"),
        [
            (
                Problem(status=404, kind="nicht-gefunden", detail="Der Textbaustein ist nicht bekannt."),
                "Der Textbaustein ist nicht bekannt.",
            ),
            (Problem(status=422, kind="idempotenzschluessel-wiederverwendet", detail="…"), RESENT),
            (Problem(status=500, kind="interner-fehler", detail="… Idempotenzschlüssel …"), TRY_LATER),
            (Problem(status=503, kind="eigentuemer-nicht-erreichbar", detail="…"), TRY_LATER),
            (Problem(status=422, kind="validierung", detail=CONTRACT_VIOLATED), CONTRACT_VIOLATED),
        ],
    )
    def test_meldungen_fuer_die_oberflaeche(self, problem: Problem, meldung: str) -> None:
        assert Ergebnis(befehl="submission.submit", version=1, problem=problem).meldung == meldung

    def test_in_formular(self) -> None:
        class Formular(forms.Form):
            title = forms.CharField(required=False)

        formular = Formular(data={})
        assert formular.is_valid()
        _vertragsfehler("$.title: verletzt „maxLength“", "$.order: verletzt „minimum“").in_formular(formular)
        assert formular.errors == {"title": ["Titel ist zu lang."], "__all__": ["Reihenfolge ist zu klein."]}

        formular = Formular(data={})
        assert formular.is_valid()
        Ergebnis(
            befehl="submission.submit", version=1, problem=Problem(403, "keine-berechtigung", "Fehlt.")
        ).in_formular(formular)
        assert formular.errors == {"__all__": ["Fehlt."]}
