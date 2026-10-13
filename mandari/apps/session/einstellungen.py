# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Reiter der Einstellungen des Sitzungsdienstes im neuen Erscheinungsbild (Issue #944, Teil 2b).

Bisher führte die Übersicht ``session/settings/index.html`` über elf Kacheln zu den Einstellungsseiten, jede Seite mit
eigenen Brotkrumen zurück. Im neuen Rahmen stehen die Seiten unter fünf Reitern (wie Work, Issue #951: höchstens
sechs); ein Reiter mit mehreren Seiten zeigt sie als Unterpunkte:

- **Allgemein**: Übersicht mit Mandant und Kontakt, Anmeldesicherheit, Erinnerungen, Bürgerportal und OParl-Schnittstelle
- **Benutzer und Rechte**: Benutzer (mit Einladen), Rollen und Rechte, Vertretungen, Vier-Augen-Prinzip
- **Sitzungen**: Sitzungsformate und Landesprofil (mit Ortsrecht je Körperschaft), Körperschaften, Wahlperioden,
  Textbausteine und Standard-TOPs
- **Vorlagen**: Nummernkreise, Mitzeichnung und Ämter
- **Datenschutz und Zugänge**: Datenschutz, Einreichungs-Zugänge für Fraktionen

Alle Adressen bleiben. Sichtbar ist ein Eintrag nur mit dem Recht, das seine Seite verlangt (``permission_required``
der View); jede Seite prüft ihr Recht weiter selbst. Körperschaften zeigt der Reiter wie die bisherige Kachel erst ab
der zweiten Körperschaft (``body_service.has_several``) – oder wenn eine ihrer Seiten geöffnet ist.

Detail- und Formularseiten (Einladen, Körperschaft anlegen/bearbeiten, Ortsrecht, Veröffentlichung beenden) stehen
unter ihrem Unterpunkt; dieser trägt dann aria-current="true" statt "page".
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

from django.urls import NoReverseMatch, reverse

SETTINGS = ("manage_settings",)
USERS = ("manage_users",)


@dataclass(frozen=True)
class Seite:
    """Eine Seite: Schlüssel, Beschriftung, Adressname (ohne ``session:``), Rechte (eines genügt)."""

    key: str
    label: str
    url_name: str
    rechte: tuple[str, ...]


@dataclass(frozen=True)
class Reiter:
    key: str
    label: str
    seiten: tuple[Seite, ...]


REITER: tuple[Reiter, ...] = (
    Reiter("allgemein", "Allgemein", (Seite("uebersicht", "Übersicht", "settings", SETTINGS + USERS),)),
    Reiter(
        "benutzer",
        "Benutzer und Rechte",
        (
            Seite("benutzer", "Benutzer", "users", USERS),
            Seite("rollen", "Rollen und Rechte", "settings_roles", USERS),
            Seite("vertretungen", "Vertretungen", "settings_delegations", USERS),
            Seite("vier_augen", "Vier-Augen-Prinzip", "settings_four_eyes", SETTINGS),
        ),
    ),
    Reiter(
        "sitzungen",
        "Sitzungen",
        (
            Seite("formate", "Sitzungsformate", "settings_meeting_formats", SETTINGS),
            Seite("koerperschaften", "Körperschaften", "settings_bodies", SETTINGS),
            Seite("wahlperioden", "Wahlperioden", "terms", SETTINGS),
            Seite("textbausteine", "Textbausteine und Standard-TOPs", "settings_textblocks", SETTINGS),
        ),
    ),
    Reiter(
        "vorlagen",
        "Vorlagen",
        (
            Seite("nummernkreise", "Nummernkreise", "settings_numbering", SETTINGS),
            Seite("mitzeichnung", "Mitzeichnung und Ämter", "settings_cosign", SETTINGS),
        ),
    ),
    Reiter(
        "datenschutz",
        "Datenschutz und Zugänge",
        (
            Seite("datenschutz", "Datenschutz", "privacy_settings", SETTINGS),
            Seite("zugaenge", "Einreichungs-Zugänge", "settings_api_tokens", SETTINGS),
        ),
    ),
)

#: Detail- und Formularseiten unter einem Unterpunkt: Adressname → Schlüssel des Unterpunkts
UNTERSEITEN: dict[str, str] = {
    "user_invite": "benutzer",
    "settings_body_create": "koerperschaften",
    "settings_body_edit": "koerperschaften",
    "settings_local_rules": "formate",
    "portal_publication_end": "uebersicht",
}

_SEITEN: dict[str, tuple[Reiter, Seite]] = {s.key: (r, s) for r in REITER for s in r.seiten}
_NACH_ADRESSE: dict[str, str] = {s.url_name: s.key for r in REITER for s in r.seiten}


def seite_zu(url_name: str) -> tuple[str, str] | None:
    """(Schlüssel des Unterpunkts, aria-current) zur geöffneten Seite; ``None``, wenn sie nicht zu den Einstellungen gehört."""
    if url_name in _NACH_ADRESSE:
        return _NACH_ADRESSE[url_name], "page"
    if url_name in UNTERSEITEN:
        return UNTERSEITEN[url_name], "true"
    return None


def _erlaubt(seite: Seite, rechte: Collection[str]) -> bool:
    return any(recht in rechte for recht in seite.rechte)


def _url(seite: Seite, slug: str) -> str:
    try:
        return reverse(f"session:{seite.url_name}", kwargs={"tenant_slug": slug})
    except NoReverseMatch:
        return ""


def navigation(tenant: Any, rechte: Collection[str], url_name: str) -> dict[str, Any] | None:
    """
    Reiter und Unterpunkte der geöffneten Einstellungsseite, nur mit Einträgen, deren Seite das Recht erlaubt. Der
    aktive Reiter trägt aria-current="page" nur auf seiner ersten Seite, sonst "true". ``None`` außerhalb der
    Einstellungen.
    """
    treffer = seite_zu(url_name)
    if treffer is None:
        return None
    seite_key, aktuell = treffer
    reiter_aktiv, seite_aktiv = _SEITEN[seite_key]
    mehrere: bool | None = None

    def sichtbar(seite: Seite) -> bool:
        nonlocal mehrere
        if not _erlaubt(seite, rechte):
            return False
        if seite.key == "koerperschaften" and seite is not seite_aktiv:
            if mehrere is None:
                from .services import body_service

                mehrere = body_service.has_several(tenant)
            return mehrere
        return True

    reiter: list[dict[str, Any]] = []
    unterpunkte: list[dict[str, Any]] = []
    for r in REITER:
        seiten = [s for s in r.seiten if sichtbar(s)]
        if not seiten:
            continue
        aktiv = r is reiter_aktiv
        ziel = seiten[0]
        reiter.append(
            {
                "key": r.key,
                "label": r.label,
                "url": _url(ziel, tenant.slug),
                "aktiv": aktiv,
                "current": aktuell if aktiv and ziel is seite_aktiv else "true",
            }
        )
        if aktiv and len(seiten) > 1:
            unterpunkte = [
                {
                    "key": s.key,
                    "label": s.label,
                    "url": _url(s, tenant.slug),
                    "aktiv": s is seite_aktiv,
                    "current": aktuell if s is seite_aktiv else "",
                }
                for s in seiten
            ]
    return {
        "reiter": reiter,
        "unterpunkte": unterpunkte,
        "reiter_label": reiter_aktiv.label,
        "seite_label": seite_aktiv.label,
    }
