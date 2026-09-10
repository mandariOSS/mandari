# SPDX-License-Identifier: AGPL-3.0-or-later
"""Kaskade der Sprecherzuordnung."""

from apps.minutes.attribution import AttributionSource, Candidate, resolve


def test_ohne_vorschlaege_bleibt_offen() -> None:
    result = resolve([])
    assert result.person_id is None
    assert result.needs_confirmation is True


def test_mikrofonkanal_schlaegt_stimmprofil() -> None:
    result = resolve(
        [
            Candidate(source=AttributionSource.VOICE_PROFILE, confidence=0.99, person_id="a"),
            Candidate(source=AttributionSource.MIC_CHANNEL, confidence=0.90, person_id="b"),
        ],
        voice_profiles_allowed=True,
    )
    assert result.source == AttributionSource.MIC_CHANNEL
    assert result.person_id == "b"
    assert result.needs_confirmation is False


def test_rednerliste_schlaegt_stimmprofil() -> None:
    result = resolve(
        [
            Candidate(source=AttributionSource.VOICE_PROFILE, confidence=0.99, person_id="a"),
            Candidate(source=AttributionSource.SPEAKER_LIST, confidence=0.80, person_id="b"),
        ],
        voice_profiles_allowed=True,
    )
    assert result.source == AttributionSource.SPEAKER_LIST
    assert result.needs_confirmation is False


def test_stimmprofil_ohne_einwilligung_wird_verworfen() -> None:
    result = resolve(
        [
            Candidate(source=AttributionSource.VOICE_PROFILE, confidence=0.99, person_id="a"),
            Candidate(source=AttributionSource.DIARIZATION, confidence=0.7, speaker_label="SPEAKER_01"),
        ],
        voice_profiles_allowed=False,
    )
    assert result.source == AttributionSource.DIARIZATION
    assert result.person_id is None
    assert result.needs_confirmation is True


def test_sprechertrennung_allein_braucht_immer_bestaetigung() -> None:
    result = resolve([Candidate(source=AttributionSource.DIARIZATION, confidence=1.0, speaker_label="SPEAKER_02")])
    assert result.speaker_label == "SPEAKER_02"
    assert result.needs_confirmation is True


def test_schwache_stimmprofil_treffer_brauchen_bestaetigung() -> None:
    result = resolve(
        [Candidate(source=AttributionSource.VOICE_PROFILE, confidence=0.60, person_id="a")],
        voice_profiles_allowed=True,
    )
    assert result.source == AttributionSource.VOICE_PROFILE
    assert result.needs_confirmation is True


def test_manuelle_zuordnung_schlaegt_alles() -> None:
    result = resolve(
        [
            Candidate(source=AttributionSource.MIC_CHANNEL, confidence=1.0, person_id="a"),
            Candidate(source=AttributionSource.MANUAL, confidence=0.0, person_id="b"),
        ]
    )
    assert result.source == AttributionSource.MANUAL
    assert result.person_id == "b"
    assert result.needs_confirmation is False


def test_benannte_vorschlaege_gehen_vor_unbenannten() -> None:
    result = resolve(
        [
            Candidate(source=AttributionSource.DIARIZATION, confidence=0.9, speaker_label="SPEAKER_03"),
            Candidate(source=AttributionSource.SPEAKER_LIST, confidence=0.9, person_id="a"),
        ]
    )
    assert result.person_id == "a"
