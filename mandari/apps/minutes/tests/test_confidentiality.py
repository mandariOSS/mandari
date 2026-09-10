# SPDX-License-Identifier: AGPL-3.0-or-later
"""Routing der KI-Verarbeitung nach Vertraulichkeit."""

import pytest

from apps.minutes.confidentiality import (
    Confidentiality,
    ProcessingPolicy,
    ProcessingTarget,
    effective_target,
    may_record,
    processing_target,
)


def test_ausgeschlossene_abschnitte_werden_nie_verarbeitet() -> None:
    for policy in (ProcessingPolicy(), ProcessingPolicy(allow_external_ai=True)):
        assert processing_target(Confidentiality.EXCLUDED, policy) == ProcessingTarget.NONE


def test_ausgeschlossene_abschnitte_duerfen_nicht_aufgezeichnet_werden() -> None:
    assert may_record(Confidentiality.EXCLUDED) is False
    assert may_record(Confidentiality.PUBLIC) is True
    assert may_record(Confidentiality.NON_PUBLIC) is True


def test_nichtoeffentliches_bleibt_lokal_auch_bei_erlaubter_externer_ki() -> None:
    policy = ProcessingPolicy(allow_external_ai=True)
    assert processing_target(Confidentiality.NON_PUBLIC, policy) == ProcessingTarget.LOCAL


def test_oeffentliches_bleibt_ohne_freigabe_lokal() -> None:
    assert processing_target(Confidentiality.PUBLIC, ProcessingPolicy()) == ProcessingTarget.LOCAL


def test_oeffentliches_darf_mit_freigabe_nach_aussen() -> None:
    policy = ProcessingPolicy(allow_external_ai=True)
    assert processing_target(Confidentiality.PUBLIC, policy) == ProcessingTarget.EU_API


def test_unbekannte_stufe_schlaegt_fehl_statt_nach_aussen_zu_routen() -> None:
    with pytest.raises(ValueError):
        processing_target("neu_erfundene_stufe", ProcessingPolicy(allow_external_ai=True))


def test_sammelaufruf_nimmt_die_strengste_stufe() -> None:
    policy = ProcessingPolicy(allow_external_ai=True)
    assert effective_target([Confidentiality.PUBLIC, Confidentiality.PUBLIC], policy) == ProcessingTarget.EU_API
    assert effective_target([Confidentiality.PUBLIC, Confidentiality.NON_PUBLIC], policy) == ProcessingTarget.LOCAL
    assert effective_target([Confidentiality.PUBLIC, Confidentiality.EXCLUDED], policy) == ProcessingTarget.NONE


def test_leerer_sammelaufruf_verarbeitet_nichts() -> None:
    assert effective_target([]) == ProcessingTarget.NONE
