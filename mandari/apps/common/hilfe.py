# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Verweise auf die Anwenderdokumentation (Issue #589).

Die Hilfe für Nutzerinnen und Nutzer steht an einer Stelle: in der Dokumentation unter
``DOCS_BASE_URL`` (Standard https://docs.mandari.de). Die Anwendung verlinkt nur dorthin, über feste
Schlüssel statt verstreuter URLs. Ändert sich ein Pfad in der Dokumentation, wird er hier angepasst.

- :data:`SEITEN` bildet Schlüssel auf Pfade (mit festen Ankern) ab.
- :func:`docs_url` liefert die vollständige Adresse; in Templates ``{% hilfe_url "konto" %}``
  (``apps.common.templatetags.hilfe_tags``) oder die Komponente ``<c-ui.help-link topic="konto" />``.
- :data:`HILFETHEMEN` ist die Themenübersicht im Support-Bereich.
- :func:`alte_wissensdatenbank_url` leitet Adressen der früheren Wissensdatenbank auf die passende Seite.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings

DOCS_BASE_URL_DEFAULT = "https://docs.mandari.de"

#: Schlüssel → Pfad unterhalb von ``DOCS_BASE_URL`` (Anker wie in der Dokumentation festgelegt)
SEITEN: dict[str, str] = {
    "start": "",
    "work": "work/",
    "erste_schritte": "work/erste-schritte/",
    "dashboard": "work/erste-schritte/#dashboard",
    "profil": "work/erste-schritte/#profil",
    "mobil": "work/erste-schritte/#mobil",
    "sitzungen": "work/sitzungen-vorbereiten/",
    "sitzung_fehlt": "work/sitzungen-vorbereiten/#sitzung-fehlt",
    "dokumente": "work/dokumente/",
    "ki_assistent": "work/dokumente/#ki-assistent",
    "einreichen": "work/antraege-einreichen/",
    "konto": "work/konto-und-sicherheit/",
    "passwort": "work/konto-und-sicherheit/#passwort",
    "zwei_faktor": "work/konto-und-sicherheit/#zwei-faktor",
    "sicherheitsschluessel": "work/konto-und-sicherheit/#sicherheitsschluessel",
    "daten": "work/konto-und-sicherheit/#daten",
    "faq": "work/haeufige-fragen/",
    "fraktions_api": "work/fraktions-api/",
    "termine_einbinden": "work/termine-einbinden/",
    "beschluesse": "work/beschluesse/",
}


def docs_base_url() -> str:
    """Basisadresse der Dokumentation ohne abschließenden Schrägstrich."""
    return str(getattr(settings, "DOCS_BASE_URL", "") or DOCS_BASE_URL_DEFAULT).rstrip("/")


def docs_url(schluessel: str) -> str:
    """Vollständige Adresse einer Dokumentationsseite; unbekannte Schlüssel sind ein Programmierfehler."""
    return f"{docs_base_url()}/{SEITEN[schluessel]}"


@dataclass(frozen=True)
class HilfeThema:
    """Eintrag der Themenübersicht im Support-Bereich."""

    schluessel: str
    titel: str
    beschreibung: str
    icon: str

    @property
    def url(self) -> str:
        return docs_url(self.schluessel)


HILFETHEMEN: tuple[HilfeThema, ...] = (
    HilfeThema("erste_schritte", "Erste Schritte", "Zugang, Profil, Dashboard und Navigation", "rocket"),
    HilfeThema("sitzungen", "Sitzungen vorbereiten", "Positionen, Notizen und Redebeiträge je TOP", "calendar"),
    HilfeThema("dokumente", "Dokumente und Anträge", "Gemeinsam schreiben, freigeben, exportieren", "file-text"),
    HilfeThema("einreichen", "Anträge einreichen", "Digital bei der Verwaltung einreichen", "send"),
    HilfeThema("konto", "Konto und Sicherheit", "Passwort, Zwei-Faktor-Anmeldung, Passkeys", "shield"),
    HilfeThema("termine_einbinden", "Termine auf der Webseite", "Fraktionssitzungen öffentlich einbinden", "globe"),
    HilfeThema("faq", "Häufige Fragen", "Schnelle Antworten auf typische Fragen", "help-circle"),
    HilfeThema("work", "Work im Überblick", "Module, Rollen und Berechtigungen", "book-open"),
)


# Frühere Wissensdatenbank (/work/<org>/support/kb/…): Kategorie- und Artikel-Slugs → Dokumentationsseite
_ALTE_KATEGORIEN: dict[str, str] = {
    "erste-schritte": "erste_schritte",
    "sitzungen": "sitzungen",
    "antraege": "dokumente",
    "fraktion": "work",
    "aufgaben": "work",
    "konto-sicherheit": "konto",
    "faq": "faq",
}
_ALTE_ARTIKEL: dict[str, str] = {
    "willkommen": "erste_schritte",
    "dashboard": "dashboard",
    "sitzungen-vorbereiten": "sitzungen",
    "antraege-erstellen": "dokumente",
    "2fa-einrichten": "zwei_faktor",
    "passwort-aendern": "passwort",
    "profil-bearbeiten": "profil",
    "sitzungen-nicht-sichtbar": "sitzung_fehlt",
    "mobile-nutzung": "mobil",
}


def alte_wissensdatenbank_url(kategorie: str | None = None, artikel: str | None = None) -> str:
    """Nachfolgeseite einer Adresse der früheren Wissensdatenbank; Unbekanntes führt zur Übersicht."""
    schluessel = _ALTE_ARTIKEL.get(artikel or "") or _ALTE_KATEGORIEN.get(kategorie or "") or "work"
    return docs_url(schluessel)
