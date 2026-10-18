# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Reiter der Einstellungen im neuen Erscheinungsbild (Issue #951, Teilpaket 3).

Sechs Reiter statt bisher bis zu zehn: Allgemein (mit E-Mail-Versand, Registrierung und API), Mitglieder (mit
Rollen), Fraktionssitzungen, Dokumente (Typen, Themen, Vorlagen, Briefköpfe), Verwaltung (Einreichung und
Ratsfraktionen) und Sicherheit. Alle Adressen bleiben; ein Reiter mit mehreren Seiten zeigt sie als Unterpunkte.

Sichtbar ist, was die bisherige Leiste ``work/organization/_tabs.html`` zeigt: Fraktionssitzungen, API und
Einreichung nur mit ``faction.manage``, Rollen nur mit ``organization.manage_roles``, alles Übrige für alle, die die
Einstellungen öffnen dürfen. Jede Seite prüft ihr Recht weiter selbst; an den Rechten ändert sich nichts. Knöpfe und
Reiter, die ohne Recht auf 403 führen (E-Mail, Registrierung, Ratsfraktionen ohne ``organization.edit``), bleiben
wie bisher sichtbar, bis Sven über das Ausblenden entscheidet (Plan #951, Frage 3).

Die Unterseite „Sicherheit“ ist die Anmeldesicherheit der Seite ``organization/`` (Zwei-Faktor-Pflicht); sie öffnet
sich unter derselben Adresse mit ``?bereich=sicherheit``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.urls import reverse

#: Abfrage der Unterseite „Sicherheit“ auf ``work:organization``
BEREICH_SICHERHEIT = "sicherheit"


@dataclass(frozen=True)
class Seite:
    """Eine Seite der Einstellungen: Schlüssel, Beschriftung, Adressname, nötiges Recht für den Eintrag, Abfrage."""

    key: str
    label: str
    url_name: str
    recht: str = ""
    abfrage: str = ""


@dataclass(frozen=True)
class Reiter:
    key: str
    label: str
    seiten: tuple[Seite, ...]


REITER: tuple[Reiter, ...] = (
    Reiter(
        "allgemein",
        "Allgemein",
        (
            Seite("organisation", "Organisation", "work:organization"),
            Seite("email", "E-Mail-Versand", "work:organization_email_settings"),
            Seite("registrierung", "Registrierung", "work:organization_registration"),
            Seite("api", "API", "work:organization_api_settings", recht="faction.manage"),
        ),
    ),
    Reiter(
        "mitglieder",
        "Mitglieder",
        (
            Seite("mitglieder", "Mitglieder", "work:members"),
            Seite("rollen", "Rollen", "work:roles", recht="organization.manage_roles"),
        ),
    ),
    Reiter(
        "fraktion",
        "Fraktionssitzungen",
        (Seite("fraktion", "Fraktionssitzungen", "work:organization_faction_settings", recht="faction.manage"),),
    ),
    Reiter(
        "dokumente",
        "Dokumente",
        (
            Seite("uebersicht", "Übersicht", "work:organization_documents"),
            Seite("typen", "Dokumenttypen", "work:document_type_list"),
            Seite("themen", "Themen", "work:document_topic_list"),
            Seite("vorlagen", "Vorlagen", "work:document_template_list"),
            Seite("briefkoepfe", "Briefköpfe", "work:document_letterhead_list"),
        ),
    ),
    Reiter(
        "verwaltung",
        "Verwaltung",
        (
            Seite("einreichung", "Einreichung", "work:organization_ris_settings", recht="faction.manage"),
            Seite("ratsfraktionen", "Ratsfraktionen", "work:council_parties"),
        ),
    ),
    Reiter(
        "sicherheit",
        "Sicherheit",
        (Seite("sicherheit", "Anmeldesicherheit", "work:organization", abfrage=f"bereich={BEREICH_SICHERHEIT}"),),
    ),
)

_SEITEN: dict[str, tuple[Reiter, Seite]] = {s.key: (r, s) for r in REITER for s in r.seiten}


def _sichtbar(seite: Seite, membership: Any) -> bool:
    return not seite.recht or bool(membership is not None and membership.has_permission(seite.recht))


def _url(seite: Seite, org_slug: str) -> str:
    url = reverse(seite.url_name, kwargs={"org_slug": org_slug})
    return f"{url}?{seite.abfrage}" if seite.abfrage else url


def navigation(organization: Any, membership: Any, seite_key: str, aktuell: str = "page") -> dict[str, Any]:
    """
    Reiter und Unterpunkte für die geöffnete Seite ``seite_key``. ``aktuell`` ist ``page``, wenn die Seite selbst
    geöffnet ist, und ``true`` für Detail- und Formularseiten darunter (Mitglied, Rolle, Dokumenttyp …): dann trägt
    der Unterpunkt aria-current="true". Der aktive Reiter trägt ``page`` nur auf seiner ersten Seite.
    """
    reiter_aktiv, seite_aktiv = _SEITEN[seite_key]
    slug = organization.slug
    reiter: list[dict[str, Any]] = []
    unterpunkte: list[dict[str, Any]] = []
    for r in REITER:
        sichtbar = [s for s in r.seiten if _sichtbar(s, membership)]
        if not sichtbar:
            continue
        aktiv = r is reiter_aktiv
        ziel = sichtbar[0]
        reiter.append(
            {
                "key": r.key,
                "label": r.label,
                "url": _url(ziel, slug),
                "aktiv": aktiv,
                "current": aktuell if aktiv and ziel is seite_aktiv else "true",
            }
        )
        if aktiv and len(sichtbar) > 1:
            unterpunkte = [
                {
                    "key": s.key,
                    "label": s.label,
                    "url": _url(s, slug),
                    "aktiv": s is seite_aktiv,
                    "current": aktuell if s is seite_aktiv else "",
                }
                for s in sichtbar
            ]
    return {
        "reiter": reiter,
        "unterpunkte": unterpunkte,
        "reiter_label": reiter_aktiv.label,
        "seite_label": seite_aktiv.label,
    }


def ziel_nach_speichern(request: Any, organization: Any) -> str:
    """
    Adresse nach dem Speichern auf ``work:organization``: Kam das Formular vom Reiter „Sicherheit“
    (``?bereich=sicherheit``), bleibt dieser Reiter offen; sonst die Seite selbst wie bisher.
    """
    url = reverse("work:organization", kwargs={"org_slug": organization.slug})
    return f"{url}?bereich={BEREICH_SICHERHEIT}" if request.GET.get("bereich") == BEREICH_SICHERHEIT else url
