# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sprecherzuordnung als Kaskade.

Ein Transkriptabschnitt kann aus mehreren Quellen einer Person zugeordnet
werden. Die Quellen unterscheiden sich in Verlässlichkeit und in ihrer
datenschutzrechtlichen Einordnung:

- ``MIC_CHANNEL``   Kanal der Konferenzanlage. Kein Personenbezug über die
                    Stimme, sondern über die Sitzplatzbelegung. Unkritisch.
- ``SPEAKER_LIST``  Rednerliste/Wortmeldung aus dem Sitzungsdienst, über den
                    Zeitstempel abgeglichen. Ebenfalls unkritisch.
- ``VOICE_PROFILE`` Abgleich gegen ein gespeichertes Stimmprofil. Biometrische
                    Verarbeitung nach Art. 9 DSGVO, daher nur mit
                    ausdrücklicher Einwilligung der betroffenen Person.
- ``DIARIZATION``   Reine Sprechertrennung innerhalb der Sitzung. Liefert kein
                    Personenergebnis, sondern nur ein Cluster-Label.
- ``MANUAL``        Zuordnung durch die Protokollführung. Schlägt alles.

Die Auflösung ist eine reine Funktion ohne Datenbankzugriff, damit sie
vollständig testbar bleibt.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import models


class AttributionSource(models.TextChoices):
    """Herkunft einer Sprecherzuordnung."""

    MANUAL = "manual", "Protokollführung"
    MIC_CHANNEL = "mic_channel", "Mikrofonkanal"
    SPEAKER_LIST = "speaker_list", "Rednerliste"
    VOICE_PROFILE = "voice_profile", "Stimmprofil"
    DIARIZATION = "diarization", "Sprechertrennung"


# Höherer Wert gewinnt. Die Reihenfolge ist die inhaltliche Aussage dieses
# Moduls: nicht-biometrische Signale schlagen das Stimmprofil.
SOURCE_PRIORITY: dict[str, int] = {
    AttributionSource.MANUAL: 100,
    AttributionSource.MIC_CHANNEL: 80,
    AttributionSource.SPEAKER_LIST: 60,
    AttributionSource.VOICE_PROFILE: 40,
    AttributionSource.DIARIZATION: 20,
}

# Unterhalb dieser Konfidenz gilt eine Zuordnung als Vorschlag, der von der
# Protokollführung bestätigt werden muss.
CONFIRMATION_THRESHOLD: dict[str, float] = {
    AttributionSource.MANUAL: 0.0,
    AttributionSource.MIC_CHANNEL: 0.0,
    AttributionSource.SPEAKER_LIST: 0.75,
    AttributionSource.VOICE_PROFILE: 0.85,
    AttributionSource.DIARIZATION: 1.01,  # nie ohne Bestätigung
}


@dataclass(frozen=True)
class Candidate:
    """Ein Zuordnungsvorschlag für einen Transkriptabschnitt."""

    source: str
    confidence: float
    person_id: str | None = None
    speaker_label: str | None = None


@dataclass(frozen=True)
class Resolution:
    """Ergebnis der Kaskade."""

    source: str | None
    person_id: str | None
    speaker_label: str | None
    confidence: float
    needs_confirmation: bool


UNRESOLVED = Resolution(
    source=None,
    person_id=None,
    speaker_label=None,
    confidence=0.0,
    needs_confirmation=True,
)


def resolve(candidates: list[Candidate], *, voice_profiles_allowed: bool = False) -> Resolution:
    """
    Beste Zuordnung aus den vorliegenden Vorschlägen bestimmen.

    Args:
        candidates: Vorschläge aus allen verfügbaren Quellen.
        voice_profiles_allowed: Nur wenn für die betroffene Person eine
            gültige Einwilligung vorliegt, darf ein Stimmprofil-Treffer
            überhaupt gewertet werden.

    Returns:
        Die gewählte Zuordnung. Ohne verwertbaren Vorschlag ``UNRESOLVED``.
    """
    usable = [
        candidate
        for candidate in candidates
        if voice_profiles_allowed or candidate.source != AttributionSource.VOICE_PROFILE
    ]
    named = [candidate for candidate in usable if candidate.person_id is not None]
    pool = named or usable
    if not pool:
        return UNRESOLVED

    best = max(pool, key=lambda candidate: (SOURCE_PRIORITY.get(candidate.source, 0), candidate.confidence))
    threshold = CONFIRMATION_THRESHOLD.get(best.source, 1.01)
    return Resolution(
        source=best.source,
        person_id=best.person_id,
        speaker_label=best.speaker_label,
        confidence=best.confidence,
        needs_confirmation=best.confidence < threshold or best.person_id is None,
    )
