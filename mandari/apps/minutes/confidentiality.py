# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Vertraulichkeitsstufen und Routing der KI-Verarbeitung.

Grundregel aus dem Leitfaden des LfDI Baden-Württemberg zur KI-Transkription
von Gemeinderatssitzungen: bevorzugt On-Premise oder abgeschottete Instanz;
externe Verarbeitung nur unter engen Bedingungen. Daraus folgt für mandari:

- Nichtöffentliche Inhalte verlassen die mandanteneigene Verarbeitung nie.
- Ausgeschlossene Inhalte (Personalangelegenheiten, Bürgerfragestunde) werden
  gar nicht erst aufgezeichnet und nie an ein Modell gegeben.
- Öffentliche Inhalte dürfen an einen zertifizierten EU-Anbieter gehen, wenn
  der Mandant das ausdrücklich erlaubt hat. Voreinstellung ist auch hier lokal.

Die Entscheidung wird bewusst hier zentral getroffen und nicht in den Views,
damit sie testbar ist und nur an einer Stelle geändert werden kann.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import models


class Confidentiality(models.TextChoices):
    """Vertraulichkeit eines Aufzeichnungsabschnitts."""

    PUBLIC = "public", "Öffentlich"
    NON_PUBLIC = "non_public", "Nichtöffentlich"
    EXCLUDED = "excluded", "Von der Aufzeichnung ausgenommen"


class ProcessingTarget(models.TextChoices):
    """Wohin ein Abschnitt zur KI-Verarbeitung gegeben werden darf."""

    EU_API = "eu_api", "Zertifizierter EU-Anbieter"
    LOCAL = "local", "Mandanteneigene Instanz"
    NONE = "none", "Keine KI-Verarbeitung"


@dataclass(frozen=True)
class ProcessingPolicy:
    """
    Mandantenweite Vorgabe.

    Attributes:
        allow_external_ai: Externe EU-Anbieter für öffentliche Inhalte erlaubt.
        allow_voice_profiles: Stimmprofile über die Sitzung hinaus speichern.
    """

    allow_external_ai: bool = False
    allow_voice_profiles: bool = False


DEFAULT_POLICY = ProcessingPolicy()


def processing_target(confidentiality: str, policy: ProcessingPolicy = DEFAULT_POLICY) -> ProcessingTarget:
    """
    Zulässiges Verarbeitungsziel für eine Vertraulichkeitsstufe bestimmen.

    Args:
        confidentiality: Wert aus :class:`Confidentiality`.
        policy: Vorgabe des Mandanten.

    Returns:
        Das höchstzulässige Verarbeitungsziel.

    Raises:
        ValueError: Bei unbekannter Vertraulichkeitsstufe. Bewusst laut, damit
            eine neue Stufe nicht stillschweigend nach außen geroutet wird.
    """
    if confidentiality == Confidentiality.EXCLUDED:
        return ProcessingTarget.NONE
    if confidentiality == Confidentiality.NON_PUBLIC:
        return ProcessingTarget.LOCAL
    if confidentiality == Confidentiality.PUBLIC:
        return ProcessingTarget.EU_API if policy.allow_external_ai else ProcessingTarget.LOCAL
    raise ValueError(f"Unbekannte Vertraulichkeitsstufe: {confidentiality!r}")


def may_record(confidentiality: str) -> bool:
    """True, wenn ein Abschnitt dieser Stufe überhaupt aufgezeichnet werden darf."""
    return confidentiality != Confidentiality.EXCLUDED


def effective_target(confidentialities: list[str], policy: ProcessingPolicy = DEFAULT_POLICY) -> ProcessingTarget:
    """
    Gemeinsames Ziel für mehrere Abschnitte bestimmen.

    Wird ein Sammelaufruf über mehrere TOPs gebildet, gilt die strengste
    beteiligte Stufe. Ohne Abschnitte gibt es nichts zu verarbeiten.
    """
    if not confidentialities:
        return ProcessingTarget.NONE
    targets = {processing_target(value, policy) for value in confidentialities}
    if ProcessingTarget.NONE in targets:
        return ProcessingTarget.NONE
    if ProcessingTarget.LOCAL in targets:
        return ProcessingTarget.LOCAL
    return ProcessingTarget.EU_API
