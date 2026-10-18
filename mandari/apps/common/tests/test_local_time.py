# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Ortszeit an den Tagen der Zeitumstellung (Issue #974), Europe/Berlin.

- 25.10.2026 (Ende der Sommerzeit): 02:30 Uhr gibt es zweimal, es gilt die erste (00:30 UTC) mit Hinweis.
- 28.03.2027 (Beginn der Sommerzeit): 02:30 Uhr gibt es nicht, die Meldung nennt das und schlägt 03:30 Uhr vor.
- Steht ein Wert schon auf der zweiten 02:30 Uhr, verschiebt ihn das Formular nicht und meldet keine Änderung.

Zeitpunkte in der doppelten Stunde vergleichen wir in UTC: Python wertet ``==`` zwischen verschiedenen Zeitzonen dort
stets als ungleich (PEP 495).
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from django import forms

from apps.common import local_time

BERLIN = ZoneInfo("Europe/Berlin")
DOPPELT = datetime(2026, 10, 25, 2, 30)
FEHLT = datetime(2027, 3, 28, 2, 30)
ERSTE = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
ZWEITE = datetime(2026, 10, 25, 1, 30, tzinfo=UTC)


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


class _Formular(forms.Form):
    start = local_time.LocalDateTimeField(label="Beginn")


class TestErkennung:
    @pytest.mark.parametrize(
        ("wert", "doppelt", "fehlt"),
        [
            (DOPPELT, True, False),
            (datetime(2026, 10, 25, 2, 0), True, False),
            (datetime(2026, 10, 25, 3, 0), False, False),
            (datetime(2026, 10, 25, 1, 59), False, False),
            (FEHLT, False, True),
            (datetime(2027, 3, 28, 2, 0), False, True),
            (datetime(2027, 3, 28, 3, 0), False, False),
            (datetime(2026, 10, 25, 12, 0), False, False),
        ],
    )
    def test_doppelte_und_fehlende_stunde(self, wert: datetime, doppelt: bool, fehlt: bool) -> None:
        assert local_time.is_ambiguous(wert, BERLIN) is doppelt
        assert local_time.is_nonexistent(wert, BERLIN) is fehlt


class TestAware:
    def test_doppelte_stunde_ergibt_die_erste_uhrzeit(self) -> None:
        assert _utc(local_time.aware(DOPPELT)) == ERSTE

    def test_fehlende_stunde_mit_verstaendlicher_meldung(self) -> None:
        with pytest.raises(local_time.NonexistentLocalTimeError) as fehler:
            local_time.aware(FEHLT)
        assert str(fehler.value) == (
            "02:30 Uhr gibt es am 28.03.2027 nicht, weil die Uhr in dieser Nacht vorgestellt wird (Zeitumstellung). "
            "Bitte eine andere Uhrzeit angeben, etwa 03:30 Uhr."
        )

    def test_gewoehnliche_und_schon_bestimmte_zeitpunkte(self) -> None:
        assert local_time.aware(datetime(2026, 10, 25, 12, 0)) == datetime(2026, 10, 25, 11, 0, tzinfo=UTC)
        assert local_time.aware(ZWEITE) is ZWEITE


class TestHinweis:
    def test_nennt_die_gespeicherte_der_beiden_uhrzeiten(self) -> None:
        assert local_time.ambiguous_notice(ERSTE, label="Beginn") == (
            "Beginn: 02:30 Uhr gibt es am 25.10.2026 zweimal, weil die Uhr in dieser Nacht zurückgestellt wird "
            "(Zeitumstellung). Gespeichert ist die erste (noch Sommerzeit)."
        )
        assert local_time.ambiguous_notice(ZWEITE).endswith("Gespeichert ist die zweite (schon Normalzeit).")

    def test_kein_hinweis_bei_eindeutiger_uhrzeit(self) -> None:
        assert local_time.ambiguous_notice(datetime(2026, 10, 25, 2, 30, tzinfo=UTC)) == ""  # 03:30 Uhr Ortszeit
        assert local_time.ambiguous_notice(None) == ""


class TestFormularfeld:
    def test_doppelte_stunde_wird_gespeichert_mit_hinweis(self) -> None:
        formular = _Formular(data={"start": "2026-10-25T02:30"})
        assert formular.is_valid(), formular.errors
        assert _utc(formular.cleaned_data["start"]) == ERSTE
        assert local_time.ambiguous_notices(formular) == [local_time.ambiguous_notice(ERSTE, label="Beginn")]

    def test_fehlende_stunde_wird_verstaendlich_abgelehnt(self) -> None:
        formular = _Formular(data={"start": "2027-03-28T02:30"})
        assert not formular.is_valid()
        assert formular.errors["start"] == [local_time.nonexistent_message(FEHLT)]

    def test_eingabeformate_wie_im_django_feld(self) -> None:
        for eingabe in ("2026-10-25 12:00", "25.10.2026 12:00", "2026-10-25T12:00+01:00"):
            formular = _Formular(data={"start": eingabe})
            assert formular.is_valid(), (eingabe, formular.errors)
            assert formular.cleaned_data["start"] == datetime(2026, 10, 25, 11, 0, tzinfo=UTC)
        assert local_time.ambiguous_notices(formular) == []

    def test_ungueltige_eingaben(self) -> None:
        for eingabe in ("morgen", "2026-13-01T10:00", "0001-01-01T00:00"):
            formular = _Formular(data={"start": eingabe})
            assert not formular.is_valid(), eingabe
            assert formular.errors["start"][0] == "Bitte ein gültiges Datum und Uhrzeit eingeben."

    def test_gespeicherte_zweite_uhrzeit_bleibt_beim_speichern(self) -> None:
        formular = _Formular(data={"start": "2026-10-25T02:30"}, initial={"start": ZWEITE})
        assert formular.is_valid(), formular.errors
        assert formular.cleaned_data["start"] == ZWEITE
        assert formular.changed_data == []
        assert local_time.ambiguous_notices(formular) == []

    def test_geaenderte_uhrzeit_ersetzt_den_gespeicherten_wert(self) -> None:
        formular = _Formular(data={"start": "2026-10-25T02:45"}, initial={"start": ZWEITE})
        assert formular.is_valid(), formular.errors
        assert _utc(formular.cleaned_data["start"]) == datetime(2026, 10, 25, 0, 45, tzinfo=UTC)
        assert formular.changed_data == ["start"]
