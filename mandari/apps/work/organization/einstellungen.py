# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Reiter der Einstellungen im neuen Erscheinungsbild (Issue #951, neu geordnet in Issue #1230).

Fünf Bereiche, freigegeben von Sven am 05.11.2026: Organisation (Profil, E-Mail-Versand, Themen und Labels),
Mitglieder und Rechte (Mitglieder, Rollen, Registrierung, Anmeldesicherheit), Sitzungen (Fraktionssitzungen,
Veröffentlichung), Dokumente (Übersicht, Dokumenttypen, Vorlagen, Briefköpfe) sowie Rat und Verwaltung
(Ratsfraktionen, Verwaltungskontakte, Einreichung). Ein Reiter mit mehreren Seiten zeigt sie als Unterpunkte.

Reiter und Unterpunkte erscheinen nur, wenn die Person die Seite auch öffnen darf: ``recht`` jeder Seite ist das
Recht ihrer View (bei mehreren genügt eines). Ein Reiter ohne sichtbare Seite entfällt, ein Reiter führt zu seiner
ersten sichtbaren Seite. Jede Seite prüft ihr Recht weiter selbst; an den Rechten ändert sich nichts.

Die Unterseite „Anmeldesicherheit“ liegt auf der Seite ``organization/`` (Zwei-Faktor-Pflicht); sie öffnet sich unter
derselben Adresse mit ``?bereich=sicherheit``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.urls import reverse

#: Abfrage der Unterseite „Anmeldesicherheit“ auf ``work:organization``
BEREICH_SICHERHEIT = "sicherheit"


@dataclass(frozen=True)
class Seite:
    """
    Eine Seite der Einstellungen: Schlüssel, Beschriftung, Adressname, nötiges Recht für den Eintrag (bei mehreren
    genügt eines), Abfrage.
    """

    key: str
    label: str
    url_name: str
    recht: str | tuple[str, ...] = ""
    abfrage: str = ""


@dataclass(frozen=True)
class Reiter:
    key: str
    label: str
    seiten: tuple[Seite, ...]


REITER: tuple[Reiter, ...] = (
    Reiter(
        "organisation",
        "Organisation",
        (
            Seite("organisation", "Profil", "work:organization", recht="organization.view"),
            Seite("email", "E-Mail-Versand", "work:organization_email_settings", recht="organization.edit"),
            # Themen (organization.edit) sind auch die Fachgebiete der Mitglieder; die Aufgaben-Labels pflegt, wer
            # tasks.manage hat
            Seite(
                "themen", "Themen und Labels", "work:document_topic_list", recht=("organization.edit", "tasks.manage")
            ),
        ),
    ),
    Reiter(
        "mitglieder",
        "Mitglieder und Rechte",
        (
            Seite("mitglieder", "Mitglieder", "work:members", recht="members.view"),
            Seite("rollen", "Rollen", "work:roles", recht="organization.manage_roles"),
            Seite("registrierung", "Registrierung", "work:organization_registration", recht="organization.edit"),
            Seite(
                "sicherheit",
                "Anmeldesicherheit",
                "work:organization",
                recht="organization.view",
                abfrage=f"bereich={BEREICH_SICHERHEIT}",
            ),
        ),
    ),
    Reiter(
        "sitzungen",
        "Sitzungen",
        (
            # Mitglieder mit faction.view_public sehen die Seite zur Information (Nur-Lesen, Issue #1071)
            Seite(
                "fraktion",
                "Fraktionssitzungen",
                "work:organization_faction_settings",
                recht=("faction.manage", "faction.view_public"),
            ),
            Seite("api", "Veröffentlichung", "work:organization_api_settings", recht="faction.manage"),
        ),
    ),
    Reiter(
        "dokumente",
        "Dokumente",
        (
            Seite("uebersicht", "Übersicht", "work:organization_documents", recht="organization.view"),
            Seite("typen", "Dokumenttypen", "work:document_type_list", recht="organization.edit"),
            Seite("vorlagen", "Vorlagen", "work:document_template_list", recht="organization.edit"),
            Seite("briefkoepfe", "Briefköpfe", "work:document_letterhead_list", recht="organization.edit"),
        ),
    ),
    Reiter(
        "verwaltung",
        "Rat und Verwaltung",
        (
            Seite("ratsfraktionen", "Ratsfraktionen", "work:council_parties", recht="organization.edit"),
            Seite("kontakte", "Verwaltungskontakte", "work:organization_admin_contacts", recht="organization.edit"),
            Seite("einreichung", "Einreichung", "work:organization_ris_settings", recht="faction.manage"),
        ),
    ),
)

_SEITEN: dict[str, tuple[Reiter, Seite]] = {s.key: (r, s) for r in REITER for s in r.seiten}


def _sichtbar(seite: Seite, membership: Any) -> bool:
    if not seite.recht:
        return True
    rechte = (seite.recht,) if isinstance(seite.recht, str) else seite.recht
    return membership is not None and any(membership.has_permission(recht) for recht in rechte)


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


def seite_sichtbar(seite_key: str, membership: Any) -> bool:
    """True, wenn die Person die Seite ``seite_key`` öffnen darf und sie deshalb in der Navigation steht."""
    return _sichtbar(_SEITEN[seite_key][1], membership)


def ist_sicherheit(request: Any) -> bool:
    """True, wenn ``work:organization`` als Unterseite „Anmeldesicherheit“ (``?bereich=sicherheit``) geöffnet ist."""
    return bool(request.GET.get("bereich") == BEREICH_SICHERHEIT)


def ziel_nach_speichern(request: Any, organization: Any) -> str:
    """
    Adresse nach dem Speichern auf ``work:organization``: Kam das Formular von der Anmeldesicherheit
    (``?bereich=sicherheit``), bleibt diese Seite offen; sonst die Seite selbst wie bisher.
    """
    url = reverse("work:organization", kwargs={"org_slug": organization.slug})
    return f"{url}?bereich={BEREICH_SICHERHEIT}" if ist_sicherheit(request) else url
