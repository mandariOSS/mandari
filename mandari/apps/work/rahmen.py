# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rahmen von Work: Schalter für das neue Erscheinungsbild und die Navigation des neuen Rahmens (Issue #852).

Schalter: ``neues_design(organization)`` sagt, ob eine Organisation den neuen Rahmen nutzt (Feld
``Organization.work_new_design``, Standard aus, die Demo an). Seiten mit eigener neuer Ansicht wählen damit ihre
Vorlage. Der Kontextprozessor ``rahmen_kontext`` gibt den Wert als ``work_neues_design`` an alle Vorlagen;
``work/base_work.html`` erweitert damit entweder den neuen (``work/base_work_neu.html``) oder den bisherigen Rahmen
(``work/base_work_alt.html``). Der bisherige Rahmen bleibt unverändert, bis Sven den neuen freigibt.

Navigation des neuen Rahmens: sechs Bereiche (Start, Sitzungen, Dokumente, Aufgaben, Team, Recherche), darunter
Einstellungen, Hilfe und die Person. Welcher Bereich aktiv ist, folgt aus dem Namen der aufgerufenen Adresse; die
rund 70 Seiten brauchen dafür keine eigene Angabe. Sitzungen und Recherche haben Reiter über die vorhandenen Listen:
Jede bisherige Seite bleibt unter ihrer Adresse erreichbar, das Ratsinformationssystem bleibt vollständig in Work
(Entscheidung Sven vom 06.10.2026: keine RIS-Seite fällt weg).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from django.http import HttpRequest
from django.urls import NoReverseMatch, reverse

#: Kennfarbe von Work, wenn die Organisation keine gültige Farbe hat (Indigo 600)
STANDARD_FARBE = "#4f46e5"
_HEX_FARBE = re.compile(r"^#[0-9a-fA-F]{6}$")
_WORT = re.compile(r"[^\W\d_]+")


def neues_design(organization: Any) -> bool:
    """True, wenn die Organisation das neue Erscheinungsbild von Work eingeschaltet hat (Issue #852)."""
    return bool(organization is not None and getattr(organization, "work_new_design", False))


@dataclass(frozen=True)
class Ziel:
    """Eintrag der Navigation: Schlüssel, Beschriftung, Adressname (``work:…``) und Symbol (Lucide)."""

    key: str
    label: str
    url_name: str
    icon: str = ""


BEREICHE: tuple[Ziel, ...] = (
    Ziel("start", "Start", "work:dashboard", "house"),
    Ziel("sitzungen", "Sitzungen", "work:meetings", "calendar-days"),
    Ziel("dokumente", "Dokumente", "work:documents", "file-text"),
    Ziel("aufgaben", "Aufgaben", "work:tasks", "square-check"),
    Ziel("team", "Team", "work:team", "users"),
    Ziel("recherche", "Recherche", "work:ris_overview", "library"),
)
#: Gäste sehen nur, was ihnen freigegeben ist (wie im bisherigen Rahmen)
GAST_BEREICHE: tuple[Ziel, ...] = (Ziel("dokumente", "Freigegebene Dokumente", "work:guest_documents", "file-lock"),)
EINSTELLUNGEN = Ziel("einstellungen", "Einstellungen", "work:organization", "settings")
HILFE = Ziel("hilfe", "Hilfe und Support", "work:support", "circle-help")
PERSON = Ziel("person", "Profil", "work:profile", "user")
BENACHRICHTIGUNGEN = Ziel("benachrichtigungen", "Benachrichtigungen", "work:notifications", "bell")

#: Reiter über den vorhandenen Listen; der erste ist die Startseite des Bereichs
REITER: dict[str, tuple[Ziel, ...]] = {
    "sitzungen": (
        Ziel("fuer_mich", "Für mich", "work:meetings"),
        Ziel("fraktion", "Fraktion", "work:faction"),
        Ziel("alle_gremien", "Alle Gremien", "work:ris_meetings"),
    ),
    "recherche": (
        Ziel("uebersicht", "Übersicht", "work:ris_overview"),
        Ziel("suche", "Suche", "work:ris_search"),
        Ziel("vorgaenge", "Vorgänge", "work:ris_papers"),
        Ziel("beschluesse", "Beschlüsse", "work:ris_decisions"),
        Ziel("gremien", "Gremien", "work:ris_organizations"),
        Ziel("personen", "Personen", "work:ris_persons"),
        Ziel("dokumente", "Dokumente", "work:ris_files"),
        Ziel("karte", "Karte", "work:ris_map"),
    ),
}

#: Seiten mit fester Einordnung: Adressname → (Bereich, Reiter, eigene Brotkrume)
SEITEN: dict[str, tuple[str, str | None, str | None]] = {
    "dashboard": ("start", None, None),
    "dashboard_explicit": ("start", None, None),
    "meetings": ("sitzungen", "fuer_mich", None),
    "meetings_calendar": ("sitzungen", None, "Kalender"),
    "session_invitations": ("sitzungen", None, "Ladungen der Verwaltung"),
    "ris_meetings": ("sitzungen", "alle_gremien", None),
    "ris_meeting_detail": ("sitzungen", "alle_gremien", None),
    "guest_documents": ("dokumente", None, None),
    "ris_overview": ("recherche", "uebersicht", None),
    "ris_search": ("recherche", "suche", None),
    "ris_papers": ("recherche", "vorgaenge", None),
    "ris_paper_detail": ("recherche", "vorgaenge", None),
    "ris_decisions": ("recherche", "beschluesse", None),
    "ris_organizations": ("recherche", "gremien", None),
    "ris_organization_detail": ("recherche", "gremien", None),
    "ris_persons": ("recherche", "personen", None),
    "ris_person_detail": ("recherche", "personen", None),
    "ris_files": ("recherche", "dokumente", None),
    "ris_map": ("recherche", "karte", None),
    "paper_comments_api": ("recherche", None, None),
}

#: Übrige Seiten nach dem Anfang ihres Adressnamens (Reihenfolge zählt: längere Anfänge zuerst)
PRAEFIXE: tuple[tuple[str, str, str | None], ...] = (
    ("meeting", "sitzungen", "fuer_mich"),
    ("faction", "sitzungen", "fraktion"),
    ("session_invitation", "sitzungen", None),
    ("document", "dokumente", None),
    ("motion", "dokumente", None),
    ("task", "aufgaben", None),
    ("team", "team", None),
    ("ris_", "recherche", None),
    ("organization", "einstellungen", None),
    ("member", "einstellungen", None),
    ("guest_", "einstellungen", None),
    ("invitation_", "einstellungen", None),
    ("role", "einstellungen", None),
    ("council_parties", "einstellungen", None),
    ("support", "hilfe", None),
    ("knowledge_base", "hilfe", None),
    ("kb_", "hilfe", None),
    ("profile", "person", None),
    ("security", "person", None),
    ("export_", "person", None),
    ("notification", "benachrichtigungen", None),
)


def einordnen(url_name: str) -> tuple[str | None, str | None, str | None]:
    """(Bereich, Reiter, eigene Brotkrume) einer Work-Seite nach ihrem Adressnamen; unbekannt → (None, None, None)."""
    if url_name in SEITEN:
        return SEITEN[url_name]
    for praefix, bereich, reiter in PRAEFIXE:
        if url_name.startswith(praefix):
            return bereich, reiter, None
    return None, None, None


def raum_kuerzel(name: str) -> str:
    """Zwei Buchstaben für das Raumzeichen: Anfänge der ersten beiden Wörter, sonst die ersten zwei Buchstaben."""
    woerter: list[str] = _WORT.findall(name or "")
    if len(woerter) >= 2:
        return (woerter[0][0] + woerter[1][0]).upper()
    if woerter:
        return woerter[0][:2].upper()
    return "?"


def raum_farbe(organization: Any) -> str:
    """Farbe der Organisation für das Raumzeichen; nur gültige #rrggbb-Werte, sonst Indigo."""
    farbe = str(getattr(organization, "effective_primary_color", "") or "")
    return farbe if _HEX_FARBE.match(farbe) else STANDARD_FARBE


def _url(ziel: Ziel, org_slug: str) -> str:
    try:
        return reverse(ziel.url_name, kwargs={"org_slug": org_slug})
    except NoReverseMatch:
        return ""


def _eintrag(ziel: Ziel, org_slug: str, aktiv: bool) -> dict[str, Any]:
    return {"key": ziel.key, "label": ziel.label, "icon": ziel.icon, "url": _url(ziel, org_slug), "aktiv": aktiv}


def raum_art(organization: Any) -> str:
    """Zweite Zeile am Raum-Knopf: die Kommune der Organisation, sonst ihre Partei."""
    body = getattr(organization, "body", None)
    if body is not None:
        return str(body.get_display_name())
    partei = getattr(organization, "party_group", None)
    return str(partei.name) if partei is not None else ""


def raeume(user: Any, organization: Any) -> list[dict[str, Any]]:
    """Alle Organisationen des Kontos für den Raum-Dialog (eine Abfrage), die geöffnete markiert."""
    from apps.tenants.models import Membership

    if user is None or not getattr(user, "is_authenticated", False):
        return []
    mitgliedschaften = (
        Membership.objects.filter(user=user, is_active=True, organization__is_active=True)
        .select_related("organization")
        .order_by("organization__name")
    )
    return [
        {
            "name": m.organization.name,
            "url": reverse("work:dashboard", kwargs={"org_slug": m.organization.slug}),
            "kuerzel": raum_kuerzel(m.organization.name),
            "gast": m.is_guest,
            "aktuell": m.organization_id == organization.id,
        }
        for m in mitgliedschaften
    ]


def navigation(organization: Any, membership: Any, url_name: str) -> dict[str, Any]:
    """
    Alles, was der neue Rahmen zum Zeichnen braucht: Bereiche der Seitenleiste und der Leiste unten, Reiter,
    Brotkrumen, Raumzeichen. Rechte wie im bisherigen Rahmen: Gäste sehen nur ihre Freigaben, Einstellungen nur
    mit ``organization.view``.
    """
    slug = organization.slug
    ist_gast = bool(getattr(membership, "is_guest", False))
    bereich, reiter_key, seite = einordnen(url_name)

    hauptbereiche = GAST_BEREICHE if ist_gast else BEREICHE
    bereiche = [_eintrag(z, slug, z.key == bereich) for z in hauptbereiche]

    unten: list[dict[str, Any]] = []
    if not ist_gast:
        if membership is not None and membership.has_permission("organization.view"):
            unten.append(_eintrag(EINSTELLUNGEN, slug, bereich == "einstellungen"))
        unten.append(_eintrag(HILFE, slug, bereich == "hilfe"))

    # Leiste unten am Handy: Start, Sitzungen, Dokumente, Recherche und „Mehr“ (Konzept W8)
    if ist_gast:
        leiste_unten = bereiche
        mehr = [_eintrag(BENACHRICHTIGUNGEN, slug, bereich == "benachrichtigungen")]
    else:
        leiste_unten = [b for b in bereiche if b["key"] in ("start", "sitzungen", "dokumente", "recherche")]
        mehr = [b for b in bereiche if b["key"] in ("aufgaben", "team")]
        mehr.append(_eintrag(BENACHRICHTIGUNGEN, slug, bereich == "benachrichtigungen"))
        mehr.extend(unten)
    mehr_aktiv = any(m["aktiv"] for m in mehr) or bereich == "person"

    # Reiter nur auf den Listen selbst; Detailseiten tragen den Weg in den Brotkrumen
    reiter_ziele = () if ist_gast else REITER.get(bereich or "", ())
    reiter_aktuell = next((z for z in reiter_ziele if z.url_name == f"work:{url_name}"), None)
    reiter = [_eintrag(z, slug, z is reiter_aktuell) for z in reiter_ziele] if reiter_aktuell else []

    return {
        "bereich": bereich,
        "bereiche": bereiche,
        "unten": unten,
        "leiste_unten": leiste_unten,
        "mehr": mehr,
        "mehr_aktiv": mehr_aktiv,
        "reiter": reiter,
        "brotkrumen": brotkrumen(organization, hauptbereiche, bereich, reiter_key, seite, url_name),
        "ist_gast": ist_gast,
        "start_url": bereiche[0]["url"],
        "suche_url": "" if ist_gast else _url(Ziel("suche", "Suche", "work:ris_search"), slug),
        "raum_kuerzel": raum_kuerzel(organization.name),
        "raum_farbe": raum_farbe(organization),
        "raum_art": raum_art(organization),
    }


def brotkrumen(
    organization: Any,
    hauptbereiche: tuple[Ziel, ...],
    bereich: str | None,
    reiter_key: str | None,
    seite: str | None,
    url_name: str,
) -> list[dict[str, Any]]:
    """Raum › Bereich › Reiter › Seite; der letzte Eintrag ist die aktuelle Seite, sofern der Rahmen sie kennt."""
    slug = organization.slug
    alle = {z.key: z for z in (*hauptbereiche, EINSTELLUNGEN, HILFE, PERSON, BENACHRICHTIGUNGEN)}
    krumen: list[dict[str, Any]] = [{"label": organization.name, "url": _url(hauptbereiche[0], slug), "aktuell": False}]
    ziel = alle.get(bereich or "")
    if ziel is None:
        return krumen
    reiter = next((z for z in REITER.get(ziel.key, ()) if z.key == reiter_key), None)
    erster_reiter = REITER.get(ziel.key, (None,))[0]
    # Der erste Reiter ist die Startseite des Bereichs und bekommt keine eigene Krume
    if reiter is not None and reiter is erster_reiter:
        reiter = None
    auf_bereich = url_name == ziel.url_name.split(":", 1)[1] and seite is None
    krumen.append({"label": ziel.label, "url": _url(ziel, slug), "aktuell": auf_bereich})
    if reiter is not None:
        auf_reiter = url_name == reiter.url_name.split(":", 1)[1] and seite is None
        krumen.append({"label": reiter.label, "url": _url(reiter, slug), "aktuell": auf_reiter})
    if seite:
        krumen.append({"label": seite, "url": "", "aktuell": True})
    return krumen


def rahmen_kontext(request: HttpRequest) -> dict[str, Any]:
    """
    Kontextprozessor: ``work_neues_design`` für alle Work-Seiten und, wenn eingeschaltet, ``work_rahmen`` mit der
    Navigation. Außerhalb von Work (keine Organisation an der Anfrage) liefert er nichts.
    """
    organization = getattr(request, "organization", None)
    if organization is None:
        return {}
    aktiv = neues_design(organization)
    kontext: dict[str, Any] = {"work_neues_design": aktiv}
    if aktiv:
        match = getattr(request, "resolver_match", None)
        url_name = match.url_name if match is not None and match.namespace == "work" else ""
        rahmen = navigation(organization, getattr(request, "membership", None), url_name or "")
        rahmen["raeume"] = raeume(getattr(request, "user", None), organization)
        kontext["work_rahmen"] = rahmen
    return kontext
