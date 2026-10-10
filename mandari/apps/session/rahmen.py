# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rahmen des Sitzungsdienstes: Schalter für das neue Erscheinungsbild und die Navigation des neuen Rahmens (Issue #944).

Schalter: ``neues_design(tenant)`` sagt, ob ein Mandant den neuen Rahmen nutzt (Feld
``SessionTenant.session_new_design``, Standard aus, auch in der Demo). ``SessionMixin.get_context_data`` gibt den Wert
als ``session_neues_design`` an alle Seiten des Mandanten; ``session/base_session.html`` erweitert damit entweder den
neuen (``session/base_session_neu.html``) oder den bisherigen Rahmen (``session/base_session_alt.html``). Seiten mit
eigener neuer Ansicht wählen damit ihre Vorlage. Der bisherige Rahmen bleibt unverändert, bis Sven den neuen freigibt.
Gesetzt wird der Schalter im Betrieb nur über :func:`setzen` (Verwaltungsbefehl ``session_neues_design``, Admin).

Navigation: derselbe Rahmen wie Work (Seitenleiste auf grauer Fläche, Kopfzeile mit Brotkrumen und Suche, Leiste
unten am Handy) mit den Bereichen des Sitzungsdienstes: Start, Sitzungen, Vorlagen, Anträge, Beschlüsse, Gremien und
Personen, darunter Leitstellen, Verwaltung (aufklappbar: Sitzungsgelder, Endgeräte, Berichte, Audit-Log) und
Einstellungen. Jeder Eintrag erscheint nur mit dem Recht, das auch die Seite verlangt (wie die bisherige Seitenleiste
``session/partials/nav_items.html``); alle bisherigen Ziele bleiben erreichbar, einige als Reiter über ihrer Liste
(Sitzungen: Liste, Kalender, Jahresplanung, Archiv; Vorlagen: Alle, Zu prüfen, Meine Mitzeichnungen; Beschlüsse:
Beschlussregister, Umlaufbeschlüsse). Welcher Bereich aktiv ist, folgt aus dem Namen der aufgerufenen Adresse.

Brotkrumen: Mandant › Bereich › Reiter › Seite. Kennt der Rahmen die Seite nicht selbst (Detail- und Formularseiten),
ergänzt ``session/base_session_neu.html`` ihren Titel als letzte Krume.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from typing import Any

from django.db import transaction
from django.urls import NoReverseMatch, reverse

logger = logging.getLogger(__name__)

#: Feld des Schalters am Mandanten (Issue #944)
FELD = "session_new_design"
#: Farbe des Raumzeichens, wenn der Mandant keine gültige Farbe hat (Indigo 600 wie Work)
STANDARD_FARBE = "#4f46e5"
_HEX_FARBE = re.compile(r"^#[0-9a-fA-F]{6}$")
_WORT = re.compile(r"[^\W\d_]+")


def neues_design(tenant: Any) -> bool:
    """True, wenn der Mandant das neue Erscheinungsbild des Sitzungsdienstes eingeschaltet hat (Issue #944)."""
    return bool(tenant is not None and getattr(tenant, FELD, False))


def setzen(tenant: Any, an: bool) -> bool:
    """
    Schalter setzen; liefert, ob sich etwas geändert hat.

    Liest den Stand unter Sperre neu und schreibt nur die eine Spalte (ohne ``save()``: keine Signale, kein neues
    ``updated_at``, keine veralteten Werte anderer Felder aus der übergebenen Instanz).
    """
    from apps.session.models import SessionTenant

    with transaction.atomic():
        bisher = SessionTenant.objects.select_for_update().filter(pk=tenant.pk).values_list(FELD, flat=True).get()
        setattr(tenant, FELD, an)
        if bool(bisher) == an:
            return False
        SessionTenant.objects.filter(pk=tenant.pk).update(**{FELD: an})
    logger.info("Session-Design umgeschaltet (mandant=%s, neues_design=%s)", tenant.pk, an)
    return True


@dataclass(frozen=True)
class Ziel:
    """Eintrag der Navigation: Schlüssel, Beschriftung, Adressname (``session:…``), Symbol, Rechte (eines genügt)."""

    key: str
    label: str
    url_name: str
    icon: str = ""
    #: Ohne Angabe für alle, sonst genügt eines dieser Rechte (wie die Seite selbst)
    rechte: tuple[str, ...] = ()
    #: Beschriftung in der Leiste unten am Handy, wenn die volle zu lang ist
    kurz: str = ""


MEETINGS = ("view_meetings",)

BEREICHE: tuple[Ziel, ...] = (
    Ziel("start", "Start", "session:dashboard", "house"),
    Ziel("sitzungen", "Sitzungen", "session:meetings", "calendar-days", MEETINGS),
    # Ohne „Vorlagen ansehen“, aber mit dem Prüfrecht führt der Bereich auf „Zu prüfen“ (wie bisher sichtbar)
    Ziel("vorlagen", "Vorlagen", "session:papers", "file-text", ("view_papers",)),
    Ziel("antraege", "Anträge", "session:applications", "inbox", ("view_applications",)),
    Ziel("beschluesse", "Beschlüsse", "session:resolutions", "gavel", MEETINGS),
    Ziel("gremien", "Gremien", "session:organizations", "landmark", MEETINGS),
    Ziel("personen", "Personen", "session:persons", "contact", MEETINGS),
)
VORLAGEN_PRUEFEN = Ziel("vorlagen", "Vorlagen", "session:papers_review", "file-text", ("approve_papers",))
#: Leiste unten am Handy (danach „Mehr“): die Kernabläufe Sitzung, Vorlage und Beschluss
LEISTE_UNTEN: tuple[str, ...] = ("start", "sitzungen", "vorlagen", "beschluesse")

VERWALTUNG = Ziel("verwaltung", "Verwaltung", "", "briefcase")
VERWALTUNG_SEITEN: tuple[Ziel, ...] = (
    Ziel("sitzungsgelder", "Sitzungsgelder", "session:allowances", "banknote", ("manage_allowances",)),
    Ziel("endgeraete", "Endgeräte", "session:devices", "tablet", ("manage_devices",)),
    Ziel("berichte", "Berichte", "session:reports", "chart-column", MEETINGS),
    Ziel("audit", "Audit-Log", "session:audit_log", "shield-check", ("view_audit_log",)),
)
EINSTELLUNGEN = Ziel(
    "einstellungen", "Einstellungen", "session:settings", "settings", ("manage_settings", "manage_users")
)
SUCHE = Ziel("suche", "Suche", "session:search", "search")

#: Reiter über den vorhandenen Listen; der erste ist die Startseite des Bereichs
REITER: dict[str, tuple[Ziel, ...]] = {
    "sitzungen": (
        Ziel("liste", "Alle Sitzungen", "session:meetings", rechte=MEETINGS),
        Ziel("kalender", "Kalender", "session:meeting_calendar", rechte=MEETINGS),
        Ziel("jahresplanung", "Jahresplanung", "session:meeting_plan", rechte=MEETINGS),
        Ziel("archiv", "Archiv", "session:archive", rechte=MEETINGS),
    ),
    "vorlagen": (
        Ziel("alle", "Alle Vorlagen", "session:papers", rechte=("view_papers",)),
        Ziel("pruefen", "Zu prüfen", "session:papers_review", rechte=("approve_papers",)),
        Ziel("mitzeichnungen", "Meine Mitzeichnungen", "session:my_cosignatures", rechte=("view_papers",)),
    ),
    "beschluesse": (
        Ziel("register", "Beschlussregister", "session:resolutions", rechte=MEETINGS),
        Ziel("umlauf", "Umlaufbeschlüsse", "session:circulars", rechte=MEETINGS),
    ),
}
#: Zahl am Reiter bzw. am Bereich: Name der Kontextvariable aus ``SessionMixin.get_context_data``
ZAEHLER: dict[str, str] = {"pruefen": "papers_review_count", "mitzeichnungen": "cosign_count"}

#: Seiten mit fester Einordnung: Adressname → (Bereich, Reiter, eigene Brotkrume)
SEITEN: dict[str, tuple[str, str | None, str | None]] = {
    "dashboard": ("start", None, None),
    "dashboard_explicit": ("start", None, None),
    "search": ("suche", None, None),
    "meetings": ("sitzungen", "liste", None),
    "meeting_calendar": ("sitzungen", "kalender", None),
    "meeting_plan": ("sitzungen", "jahresplanung", None),
    "archive": ("sitzungen", "archiv", None),
    "papers": ("vorlagen", "alle", None),
    "papers_review": ("vorlagen", "pruefen", None),
    "my_cosignatures": ("vorlagen", "mitzeichnungen", None),
    "resolutions": ("beschluesse", "register", None),
    "circulars": ("beschluesse", "umlauf", None),
    "voting_capture": ("sitzungen", None, None),
    "allowances": ("sitzungsgelder", None, None),
    "allowances_monthly": ("sitzungsgelder", None, None),
    "allowance_year": ("sitzungsgelder", None, None),
    "devices": ("endgeraete", None, None),
    "reports": ("berichte", None, None),
    "audit_log": ("audit", None, None),
    "users": ("einstellungen", None, None),
    "terms": ("einstellungen", None, None),
    "privacy_notice": ("einstellungen", None, None),
}

#: Übrige Seiten nach dem Anfang ihres Adressnamens (Reihenfolge zählt: längere Anfänge zuerst)
PRAEFIXE: tuple[tuple[str, str], ...] = (
    ("meeting", "sitzungen"),
    ("agenda", "sitzungen"),
    ("attendance", "sitzungen"),
    ("circular", "beschluesse"),
    ("resolution", "beschluesse"),
    ("paper", "vorlagen"),
    ("consultation", "vorlagen"),
    ("cosign", "vorlagen"),
    ("application", "antraege"),
    ("organization", "gremien"),
    ("membership", "gremien"),
    ("person", "personen"),
    ("allowance", "sitzungsgelder"),
    ("monthly", "sitzungsgelder"),
    ("device", "endgeraete"),
    ("settings", "einstellungen"),
    ("user", "einstellungen"),
    ("role", "einstellungen"),
    ("term", "einstellungen"),
    ("invitation", "einstellungen"),
    ("privacy", "einstellungen"),
    ("file", ""),
)


def einordnen(url_name: str) -> tuple[str | None, str | None, str | None]:
    """(Bereich, Reiter, eigene Brotkrume) einer Seite nach ihrem Adressnamen; unbekannt → (None, None, None)."""
    if url_name in SEITEN:
        return SEITEN[url_name]
    for praefix, bereich in PRAEFIXE:
        if url_name.startswith(praefix):
            return bereich or None, None, None
    return None, None, None


def raum_kuerzel(name: str) -> str:
    """Zwei Buchstaben für das Raumzeichen: Anfänge der ersten beiden Wörter, sonst die ersten zwei Buchstaben."""
    woerter: list[str] = _WORT.findall(name or "")
    if len(woerter) >= 2:
        return (woerter[0][0] + woerter[1][0]).upper()
    if woerter:
        return woerter[0][:2].upper()
    return "?"


def raum_farbe(tenant: Any) -> str:
    """Farbe des Mandanten für das Raumzeichen; nur gültige #rrggbb-Werte, sonst Indigo."""
    farbe = str(getattr(tenant, "primary_color", "") or "")
    return farbe if _HEX_FARBE.match(farbe) else STANDARD_FARBE


def _erlaubt(ziel: Ziel, rechte: Collection[str]) -> bool:
    return not ziel.rechte or any(recht in rechte for recht in ziel.rechte)


def _url(ziel: Ziel, slug: str) -> str:
    try:
        return reverse(ziel.url_name, kwargs={"tenant_slug": slug})
    except NoReverseMatch:
        return ""


def _ist_seite(ziel: Ziel, url_name: str) -> bool:
    """True, wenn der Eintrag genau auf die geöffnete Seite zeigt."""
    seite = "dashboard" if url_name == "dashboard_explicit" else url_name
    return ziel.url_name == f"session:{seite}"


def _eintrag(ziel: Ziel, slug: str, aktiv: bool, url_name: str, zahl: int = 0) -> dict[str, Any]:
    current = ("page" if _ist_seite(ziel, url_name) else "true") if aktiv else ""
    return {
        "key": ziel.key,
        "label": ziel.label,
        "kurz": ziel.kurz or ziel.label,
        "icon": ziel.icon,
        "url": _url(ziel, slug),
        "aktiv": aktiv,
        "current": current,
        "zahl": zahl,
        "unterpunkte": [],
        "offen": False,
    }


def _zahlen(zaehler: dict[str, Any] | None) -> dict[str, int]:
    """Zahlen der Arbeitsvorräte (zu prüfende Vorlagen, offene Mitzeichnungen) aus dem Seitenkontext."""
    werte = zaehler or {}
    return {key: int(werte.get(variable) or 0) for key, variable in ZAEHLER.items()}


def navigation(
    tenant: Any,
    rechte: Collection[str],
    url_name: str,
    *,
    raeume: Iterable[dict[str, str]] = (),
    leitstellen: Iterable[dict[str, str]] = (),
    zaehler: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Alles, was der neue Rahmen zum Zeichnen braucht: Bereiche der Seitenleiste und der Leiste unten, Reiter,
    Brotkrumen, Raumzeichen und Mandanten. Rechte wie in der bisherigen Seitenleiste: Jeder Eintrag nur mit dem Recht
    seiner Seite. ``raeume`` und ``leitstellen`` kommen aus dem Zwischenspeicher der Anmeldung (keine Abfrage).
    """
    slug = tenant.slug
    bereich, reiter_key, seite = einordnen(url_name)
    zahlen = _zahlen(zaehler)
    arbeit = zahlen["pruefen"] + zahlen["mitzeichnungen"]

    bereiche: list[dict[str, Any]] = []
    for ziel in BEREICHE:
        if ziel.key == "vorlagen" and not _erlaubt(ziel, rechte) and _erlaubt(VORLAGEN_PRUEFEN, rechte):
            ziel = VORLAGEN_PRUEFEN
        if not _erlaubt(ziel, rechte):
            continue
        bereiche.append(_eintrag(ziel, slug, ziel.key == bereich, url_name, arbeit if ziel.key == "vorlagen" else 0))

    # Verwaltung: aufklappbar, offen auf ihren Seiten; nur, wenn mindestens eine Seite erlaubt ist
    unten: list[dict[str, Any]] = []
    for leitstelle in leitstellen:
        url = reverse("session:leitstelle", kwargs={"group_slug": leitstelle["slug"]})
        unten.append(
            {
                "key": f"leitstelle-{leitstelle['slug']}",
                "label": f"Leitstelle {leitstelle['name']}",
                "kurz": "Leitstelle",
                "icon": "radar",
                "url": url,
                "aktiv": False,
                "current": "",
                "zahl": 0,
                "unterpunkte": [],
                "offen": False,
            }
        )
    verwaltung = [_eintrag(z, slug, z.key == bereich, url_name) for z in VERWALTUNG_SEITEN if _erlaubt(z, rechte)]
    if verwaltung:
        gruppe = _eintrag(VERWALTUNG, slug, any(u["aktiv"] for u in verwaltung), url_name)
        gruppe["url"] = verwaltung[0]["url"]
        gruppe["current"] = "true" if gruppe["aktiv"] else ""
        gruppe["unterpunkte"] = verwaltung
        gruppe["offen"] = gruppe["aktiv"]
        unten.append(gruppe)
    if _erlaubt(EINSTELLUNGEN, rechte):
        unten.append(_eintrag(EINSTELLUNGEN, slug, bereich == "einstellungen", url_name))

    leiste_unten = [b for b in bereiche if b["key"] in LEISTE_UNTEN]
    mehr = [b for b in bereiche if b["key"] not in LEISTE_UNTEN]
    for eintrag in unten:
        mehr.extend(eintrag["unterpunkte"] or [eintrag])
    mehr_aktiv = any(m["aktiv"] for m in mehr)

    # Reiter nur auf den Listen selbst; Detailseiten tragen den Weg in den Brotkrumen
    reiter_ziele = [z for z in REITER.get(bereich or "", ()) if _erlaubt(z, rechte)]
    reiter_aktuell = next((z for z in reiter_ziele if _ist_seite(z, url_name)), None)
    reiter = (
        [_eintrag(z, slug, z is reiter_aktuell, url_name, zahlen.get(z.key, 0)) for z in reiter_ziele]
        if reiter_aktuell is not None and len(reiter_ziele) > 1
        else []
    )
    return {
        "bereich": bereich,
        "bereiche": bereiche,
        "unten": unten,
        "leiste_unten": leiste_unten,
        "mehr": mehr,
        "mehr_aktiv": mehr_aktiv,
        "reiter": reiter,
        "reiter_label": next((b["label"] for b in bereiche if b["aktiv"]), ""),
        "brotkrumen": brotkrumen(tenant, bereiche, unten, bereich, reiter_key, seite, url_name),
        "start_url": _url(BEREICHE[0], slug),
        "suche_url": _url(SUCHE, slug),
        "raum_kuerzel": raum_kuerzel(tenant.short_name or tenant.name),
        "raum_farbe": raum_farbe(tenant),
        "raeume": [
            {
                "name": raum["name"],
                "url": reverse("session:dashboard", kwargs={"tenant_slug": raum["slug"]}),
                "kuerzel": raum_kuerzel(raum["name"]),
                "aktuell": raum["slug"] == slug,
            }
            for raum in raeume
        ],
        "leitstellen": [eintrag for eintrag in unten if eintrag["key"].startswith("leitstelle-")],
    }


def brotkrumen(
    tenant: Any,
    bereiche: list[dict[str, Any]],
    unten: list[dict[str, Any]],
    bereich: str | None,
    reiter_key: str | None,
    seite: str | None,
    url_name: str,
) -> list[dict[str, Any]]:
    """
    Mandant › Bereich › Reiter › Seite; der letzte Eintrag ist die aktuelle Seite, sofern der Rahmen sie kennt. Sonst
    (Detail- und Formularseiten) ergänzt ``session/base_session_neu.html`` den Seitentitel als letzte Krume. Die Seiten
    der Verwaltung stehen unter „Verwaltung“ (ohne eigene Seite, nur Text).
    """
    krumen: list[dict[str, Any]] = [{"label": tenant.name, "url": _url(BEREICHE[0], tenant.slug), "aktuell": False}]
    if bereich == "start":
        krumen[0]["aktuell"] = True
        return krumen
    alle = {e["key"]: e for e in bereiche}
    for eintrag in unten:
        alle[eintrag["key"]] = eintrag
        for unterpunkt in eintrag["unterpunkte"]:
            alle[unterpunkt["key"]] = {**unterpunkt, "gruppe": eintrag}
    if bereich == "suche":
        alle["suche"] = _eintrag(SUCHE, tenant.slug, True, url_name)
    ziel = alle.get(bereich or "")
    if ziel is None:
        return krumen
    if "gruppe" in ziel:
        krumen.append({"label": ziel["gruppe"]["label"], "url": "", "aktuell": False})
    reiter_ziele = REITER.get(ziel["key"], ())
    reiter = next((z for z in reiter_ziele if z.key == reiter_key), None)
    # Der erste Reiter ist die Startseite des Bereichs und bekommt keine eigene Krume
    if reiter is not None and reiter_ziele and reiter is reiter_ziele[0]:
        reiter = None
    auf_bereich = ziel["current"] == "page" and seite is None and reiter is None
    krumen.append({"label": ziel["label"], "url": ziel["url"], "aktuell": auf_bereich})
    if reiter is not None:
        krumen.append(
            {"label": reiter.label, "url": _url(reiter, tenant.slug), "aktuell": _ist_seite(reiter, url_name)}
        )
    if seite:
        krumen.append({"label": seite, "url": "", "aktuell": True})
    return krumen


def rahmen_kontext(view: Any, kontext: dict[str, Any]) -> dict[str, Any]:
    """
    Kontext des Rahmens für eine Seite des Sitzungsdienstes (aufgerufen aus ``SessionMixin.get_context_data``):
    ``session_neues_design`` immer, ``session_rahmen`` mit der Navigation nur, wenn der Schalter an ist. Rechte,
    Mandanten, Leitstellen und Zahlen stammen aus dem schon berechneten Seitenkontext – keine weitere Abfrage.
    """
    tenant = kontext.get("session_tenant")
    aktiv = neues_design(tenant)
    ergebnis: dict[str, Any] = {"session_neues_design": aktiv}
    if not aktiv:
        return ergebnis
    match = getattr(getattr(view, "request", None), "resolver_match", None)
    url_name = match.url_name if match is not None and match.namespace == "session" else ""
    checker = kontext.get("permission_checker")
    rechte: Collection[str] = checker.permissions if checker is not None else set()
    ergebnis["session_rahmen"] = navigation(
        tenant,
        rechte,
        url_name or "",
        raeume=kontext.get("user_tenants") or (),
        leitstellen=kontext.get("user_leitstellen") or (),
        zaehler=kontext,
    )
    return ergebnis
