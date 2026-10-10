# SPDX-License-Identifier: AGPL-3.0-or-later
"""
SEO Utilities für mandari Insight.

Generiert SEO-relevante Metadaten für alle öffentlichen Seiten.
- Open Graph Tags
- Twitter Cards
- Canonical URLs
- JSON-LD Structured Data (je Seite das Objekt und als zweites Element die Brotkrumen)

Titel und Descriptions der Detailseiten (Issue #914) nennen, wonach Menschen suchen: Betreff bzw. Gremium,
Person oder Beschluss und die Kommune – genau einmal, auch im Bürgerportal einer Körperschaft, dessen
Titelzusatz sonst ebenfalls die Kommune ist. Die Description ergänzt den Titel um Stand, Termin und Umfang.
Alle Angaben kommen aus dem, was die Views ohnehin laden; hier entstehen keine Abfragen außer über die
schon genutzten Beziehungen (``body``, vorgeladene Gremien). Ausnahme ist der Ort einer Sitzung
(``services.sitzungsort``): höchstens je eine kleine Abfrage, wenn die Sitzung ihn nur verweist bzw. keinen nennt.

Die Brotkrumen der strukturierten Daten führen nur über Adressen, die Suchmaschinen direkt erreichen
(Bürgerportal → Kommune → Objekt, Issue #939).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from urllib.parse import urljoin

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.http import HttpRequest
from django.templatetags.static import static
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.utils.safestring import SafeString, mark_safe

from .services.indexierung import stadtseite_url
from .services.paper_status import NONE as STAND_UNBEKANNT
from .services.paper_status import PaperStatus, in_committee
from .services.search_presentation import normalize_paper_type

#: Wie ``django.utils.html.json_script``: Maskiert, kann JSON nie aus einem ``<script>``-Element
#: ausbrechen (``</script>``, ``<!--``) und bleibt gültiges JSON. U+2028/2029 dazu für Alt-Parser.
_SCRIPT_JSON_ESCAPES = {
    ord("<"): "\\u003C",
    ord(">"): "\\u003E",
    ord("&"): "\\u0026",
    0x2028: "\\u2028",
    0x2029: "\\u2029",
}

#: Längster Betreff bzw. Name im Seitentitel (Zeichen, an einer Wortgrenze gekürzt)
TITEL_BETREFF_MAX = 70
#: Längste Description (Zeichen); mehr zeigen Suchmaschinen nicht
DESCRIPTION_MAX = 160
#: Ein Teil der Description, der nicht mehr ganz passt, erscheint gekürzt nur ab so viel Platz
_DESCRIPTION_REST_MIN = 30
#: Längste Art eines Vorgangs im Titel („Vorlage“, „Antrag“, „Große Anfrage“); längere Kategorien → „Vorgang“
_ART_MAX = 25
#: Längster Sitzungsort in der Description
_ORT_MAX = 60
#: So viele Gremien nennt die Description einer Person (JSON-LD: so viele Mitgliedschaften)
_GREMIEN_MAX = 4
_MITGLIEDSCHAFTEN_LD_MAX = 10
#: „Fraktion “ vor dem Namen einer Fraktion (in der Description steht schon „Fraktion:“)
_FRAKTION_VORAN = re.compile(r"^Fraktion\s+", re.IGNORECASE)
_WOCHENTAGE = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")


def script_json(value: Any) -> SafeString:
    """JSON zum Einbetten in ein ``<script>``-Element (JSON-LD, Diagrammdaten).

    Die Werte stammen oft aus fremden Ratsinformationssystemen oder von Bürger:innen.
    """
    text = json.dumps(value, ensure_ascii=False, cls=DjangoJSONEncoder).translate(_SCRIPT_JSON_ESCAPES)
    return mark_safe(text)  # durch die Maskierung oben sicher


@dataclass
class SEOContext:
    """
    Container für SEO-Metadaten einer Seite.

    Wird in Templates für Meta-Tags verwendet. ``title`` ist der Seitentitel ohne den Zusatz des
    Rahmens („| mandari Insight“) und zugleich ``og:title``. ``breadcrumbs`` (Name, absolute URL)
    erscheinen als zweites JSON-LD-Element (``BreadcrumbList``).
    """

    title: str
    description: str
    canonical_url: str
    og_type: str = "website"
    og_image: str | None = None
    og_locale: str = "de_DE"
    twitter_card: str = "summary"
    json_ld: dict[str, Any] | None = None
    keywords: list[str] = field(default_factory=list)
    robots: str = "index, follow"
    author: str = "mandari"
    breadcrumbs: list[tuple[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Konvertiert zu Dictionary für Template-Rendering."""
        return {
            "title": self.title,
            "description": self.description,
            "canonical_url": self.canonical_url,
            "og_type": self.og_type,
            "og_image": self.og_image,
            "og_locale": self.og_locale,
            "twitter_card": self.twitter_card,
            "json_ld": script_json(self.json_ld) if self.json_ld else None,
            "json_ld_brotkrumen": script_json(breadcrumb_json_ld(self.breadcrumbs)) if self.breadcrumbs else None,
            "keywords": ", ".join(k for k in self.keywords if k) if self.keywords else None,
            "robots": self.robots,
            "author": self.author,
        }


def get_site_url() -> str:
    """Gibt die konfigurierte Site-URL zurück."""
    return getattr(settings, "SITE_URL", "https://mandari.de")


def build_canonical_url(request: HttpRequest, path: str | None = None) -> str:
    """
    Baut die kanonische URL für eine Seite.

    Args:
        request: Django HttpRequest
        path: Optionaler Pfad (sonst request.path)

    Returns:
        Vollständige kanonische URL
    """
    site_url = get_site_url()
    path = path or request.path
    return urljoin(site_url, path)


# =============================================================================
# Bausteine für Titel, Descriptions und Brotkrumen
# =============================================================================


def kuerzen(wert: Any, limit: int) -> str:
    """Text auf höchstens ``limit`` Zeichen an einer Wortgrenze kürzen, mit „…“ am Ende."""
    text = " ".join(str(wert or "").split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    # Endet der Schnitt mitten im Wort, fällt das angeschnittene Wort weg; ein ganzes Wort davor bleibt
    if text[limit - 1] != " " and " " in cut:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip(" ,;:-–") + "…"


def beschreibung(*teile: str, limit: int = DESCRIPTION_MAX) -> str:
    """Description aus Sätzen in Rangfolge: ganze Sätze, solange sie passen, dann höchstens ein gekürzter."""
    text = ""
    for teil in teile:
        teil = " ".join(str(teil or "").split())
        if not teil:
            continue
        kandidat = f"{text} {teil}".strip()
        if len(kandidat) <= limit:
            text = kandidat
            continue
        rest = limit - len(text) - (1 if text else 0)
        if rest >= _DESCRIPTION_REST_MIN or not text:
            text = f"{text} {kuerzen(teil, rest)}".strip()
        break
    return text


def _datum(value: date | datetime | None) -> str:
    """Datum als „TT.MM.JJJJ“ in Ortszeit (Sitzungen am späten Abend liegen in UTC schon am Folgetag)."""
    if value is None:
        return ""
    if isinstance(value, datetime) and timezone.is_aware(value):
        value = timezone.localtime(value)
    return value.strftime("%d.%m.%Y")


def _kommune(body: Any) -> str:
    return str(body.get_display_name() or "") if body is not None else ""


def _nennt(text: str, name: str) -> bool:
    """Steht ``name`` als eigenes Wort im Text („Rat der Stadt Münster“ nennt „Münster“)?"""
    return bool(name) and re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text, re.IGNORECASE) is not None


def mit_kommune(haupt: str, kommune: str, trenner: str = ", ") -> str:
    """Kommune an den Titel hängen – nur, wenn er sie nicht schon nennt (nie doppelt)."""
    if not kommune or _nennt(haupt, kommune):
        return haupt
    return f"{haupt}{trenner}{kommune}" if haupt else kommune


def _ohne_leere(wert: Any) -> Any:
    """JSON-LD ohne leere Angaben (``None``, „“, leere Listen und Objekte)."""
    if isinstance(wert, dict):
        bereinigt = {k: _ohne_leere(v) for k, v in wert.items()}
        return {k: v for k, v in bereinigt.items() if v not in (None, "", [], {})}
    if isinstance(wert, list):
        return [v for v in (_ohne_leere(v) for v in wert) if v not in (None, "", [], {})]
    return wert


def _gebiet(kommune: str) -> dict[str, str] | None:
    return {"@type": "AdministrativeArea", "name": kommune} if kommune else None


def _verwaltung(kommune: str, url: str = "") -> dict[str, str] | None:
    """Verwaltung der Kommune als Organisation, mit ``url`` (Seite der Kommune), sofern bekannt."""
    if not kommune:
        return None
    verwaltung = {"@type": "GovernmentOrganization", "name": kommune}
    if url:
        verwaltung["url"] = url
    return verwaltung


def _absolut(path: str) -> str:
    return urljoin(get_site_url(), path)


def seite_der_kommune(body: Any) -> str:
    """Absolute Adresse der Seite einer Kommune (``/insight/k/<slug>/``), leer, wo es keine erreichbare gibt.

    Wie die sichtbaren Brotkrumen über ``stadtseite_url``: nur gelistete, nicht gelöschte Kommunen mit Slug.
    Der Einstieg einer nicht gelisteten Kommune (z. B. einer ausgeblendeten Pilotquelle) antwortet mit 404,
    obwohl ihre Detailseiten direkt erreichbar bleiben.
    """
    try:
        seite = stadtseite_url(body)
    except NoReverseMatch:  # Slug aus Altbestand, der nicht mehr zum Muster passt
        return ""
    return _absolut(seite) if seite else ""


def brotkrumen(body: Any, name: str, url: str) -> list[tuple[str, str]]:
    """Brotkrumen einer Detailseite für die strukturierten Daten: Bürgerportal → Kommune → Objekt.

    Die Kommune erscheint mit ihrer Seite (``/insight/k/<slug>/``), sofern sie eine erreichbare hat
    (``seite_der_kommune``: gelistet, mit Slug). Eine Ebene für den Bereich (Sitzungen, Vorgänge …) gibt es nicht:
    Dessen Listen haben keine eigene Adresse, die Suchmaschinen ohne gewählte Kommune erreichen
    (``/insight/k/<slug>/termine/`` leitet weiter, Issue #939). Die sichtbaren Brotkrumen der Kopfzeile bleiben
    davon unberührt.
    """
    krumen: list[tuple[str, str]] = [("mandari Insight", _absolut(reverse("insight_core:insight:portal_home")))]
    seite = seite_der_kommune(body)
    if seite:
        krumen.append((_kommune(body), seite))
    krumen.append((name, url))
    return krumen


def breadcrumb_json_ld(krumen: Sequence[tuple[str, str]]) -> dict[str, Any]:
    """``BreadcrumbList`` nach schema.org aus (Name, absolute URL)."""
    return {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": nummer, "name": name, "item": url}
            for nummer, (name, url) in enumerate(krumen, start=1)
        ],
    }


# =============================================================================
# Entitäts-spezifische SEO-Generatoren
# =============================================================================


def art_kurz(paper_type: str | None, body_slug: str = "") -> str:
    """Art eines Vorgangs für den Titel: „Vorlage“, „Antrag“, „Anfrage“ …; Sammelkategorien → „Vorgang“."""
    art = normalize_paper_type(paper_type, body_slug)
    if art.lower() == "anträge":  # Kategorie im Plural („Vorlagen“ fasst normalize_paper_type schon)
        art = "Antrag"
    if not art or len(art) > _ART_MAX or len(art.split()) > 2:
        return "Vorgang"
    return art


def betreff(paper: Any) -> str:
    """Betreff eines Vorgangs ohne vorangestellte Drucksachennummer („V/0881/2011 Radweg …“ → „Radweg …“)."""
    name = " ".join(str(paper.name or "").split())
    nummer = (paper.reference or "").strip()
    if nummer and name.lower().startswith(nummer.lower()):
        name = name[len(nummer) :].lstrip(" :–-,")
    return name


def _kennung(paper: Any) -> str:
    """Art und Drucksachennummer: „Vorlage V/0881/2011“."""
    art = art_kurz(paper.paper_type, paper.body.slug or "")
    return " ".join(t for t in (art, (paper.reference or "").strip()) if t)


def paper_title(paper: Any) -> str:
    """„{Betreff} – {Art} {Nr.}, {Kommune}“, ohne Betreff „{Art} {Nr.}, {Kommune}“."""
    kennung = _kennung(paper)
    text = betreff(paper)
    haupt = f"{kuerzen(text, TITEL_BETREFF_MAX)} – {kennung}" if text else kennung
    return mit_kommune(haupt, _kommune(paper.body))


def get_paper_seo(paper: Any, request: HttpRequest, stand: PaperStatus | None = None) -> SEOContext:
    """
    SEO-Kontext einer Vorgangsseite.

    ``stand`` ist der Stand-Satz der Seite (``paper_status``, aus dem schon geladenen Beratungsverlauf). Die
    Description nennt Kommune, Art, Nummer und Datum, dann den Stand und, wenn Platz bleibt, den Anfang der
    gespeicherten Zusammenfassung.
    """
    body = paper.body
    kommune = _kommune(body)
    title = paper_title(paper)
    datum = _datum(paper.date)
    kopf = f"{_kennung(paper)} vom {datum}" if datum else _kennung(paper)
    stand_satz = stand.text if stand is not None and stand.kind != STAND_UNBEKANNT else ""
    description = beschreibung(f"{kommune}: {kopf}." if kommune else f"{kopf}.", stand_satz, paper.summary or "")
    canonical = build_canonical_url(request)
    ort = _gebiet(kommune)

    json_ld = _ohne_leere(
        {
            "@context": "https://schema.org",
            "@type": "CreativeWork",
            "name": paper.name or "Vorgang",
            "identifier": paper.reference,
            "genre": paper.paper_type,
            "description": description,
            "url": canonical,
            "inLanguage": "de",
            "dateCreated": paper.date.isoformat() if paper.date else None,
            "spatialCoverage": ort,
            "about": ort,
            "author": _verwaltung(kommune) or {"@type": "Organization", "name": "Kommune"},
            "publisher": {"@type": "Organization", "name": "mandari", "url": get_site_url()},
        }
    )

    return SEOContext(
        title=title,
        description=description,
        canonical_url=canonical,
        og_type="article",
        json_ld=json_ld,
        keywords=[kommune, paper.paper_type or "Vorgang", "Kommunalpolitik"],
        # Letzte Brotkrume wie in der Kopfzeile: Drucksachennummer, sonst der gekürzte Betreff
        breadcrumbs=brotkrumen(
            body, (paper.reference or "").strip() or kuerzen(betreff(paper) or "Vorgang", 50), canonical
        ),
    )


def _uhrzeit(start: datetime) -> str:
    lokal = timezone.localtime(start) if timezone.is_aware(start) else start
    return f"{_WOCHENTAGE[lokal.weekday()]}, {lokal:%d.%m.%Y}, {lokal:%H:%M} Uhr"


def _zeitpunkt(value: datetime) -> str:
    """ISO 8601 mit dem Versatz der Ortszeit („2030-11-05T17:00:00+01:00“)."""
    return (timezone.localtime(value) if timezone.is_aware(value) else value).isoformat()


def _webseite(body: Any) -> str:
    website = str(getattr(body, "website", "") or "") if body is not None else ""
    return website if website.startswith(("https://", "http://")) else ""


def _bild_der_kommune(body: Any) -> str:
    """Bild einer Veranstaltung der Kommune (absolut): ihr Logo bzw. Wappen, sonst das Standardbild der Seiten.

    Das Logo im Original: Die Vorschaubilder (``logo_vorschau``, 64 und 128 Pixel) sind für Suchmaschinen zu
    klein. Das Bild der Portalseite (``hero_image``) bleibt außen vor – es trägt oft einen Bildnachweis, den
    Suchergebnisse nicht zeigen.
    """
    logo = getattr(body, "logo", None) if body is not None else None
    return _absolut(logo.url if logo else static("images/og-default.png"))


def _gremien_der_sitzung(meeting: Any) -> list[dict[str, str]]:
    """Gremien einer Sitzung als Mitwirkende mit der Adresse ihrer Seite (aus den vorgeladenen Gremien)."""
    gremien = []
    for org in meeting.organizations.all():
        if org.name and not org.withdrawn_by_publisher:
            seite = reverse("insight_core:insight:organization_detail", kwargs={"pk": org.pk})
            gremien.append({"@type": "Organization", "name": org.name, "url": _absolut(seite)})
    return gremien


def ist_oeffentlich(tagesordnung: Iterable[Any]) -> bool:
    """Hat die Sitzung laut Quelle einen öffentlichen Teil?

    Ja, wenn mindestens ein angezeigter Tagesordnungspunkt ausdrücklich öffentlich ist (OParl
    ``AgendaItem.public: true`` in den Rohdaten). Fehlt die Angabe, steht die Spalte ``public`` nur auf ihrem
    Standardwert – dann gilt die Sitzung hier nicht als öffentlich. An der Sitzung selbst kennt OParl keine
    Angabe zur Öffentlichkeit.
    """
    return any(
        getattr(punkt, "public", False) and isinstance(punkt.raw_json, dict) and punkt.raw_json.get("public") is True
        for punkt in tagesordnung
    )


def _gueltig_ab(meeting: Any) -> str | None:
    """
    ``offers.validFrom``: seit wann die Sitzung bekannt ist – ``created`` der Quelle, sonst unser erster Abgleich.

    Unglaubhafte Zeitpunkte (vor 2000, in der Zukunft; z. B. Ersatzwerte der Quelle) zählen nicht, wie bei
    ``lastmod`` der Sitemaps. Nie später als der Beginn der Sitzung.
    """
    from .services.sitemaps import FRUEHESTE_AENDERUNG, ZUKUNFT_SPIELRAUM

    grenze = timezone.now() + ZUKUNFT_SPIELRAUM
    for wert in (getattr(meeting, "oparl_created", None), getattr(meeting, "created_at", None)):
        if isinstance(wert, datetime) and timezone.is_aware(wert) and FRUEHESTE_AENDERUNG <= wert <= grenze:
            wert = wert.replace(microsecond=0)
            return _zeitpunkt(min(wert, meeting.start) if meeting.start else wert)
    return None


def get_meeting_seo(
    meeting: Any,
    request: HttpRequest,
    agenda_count: int | None = None,
    tagesordnung: Iterable[Any] = (),
) -> SEOContext:
    """
    SEO-Kontext einer Sitzungsseite: „{Gremium} am {Datum} – {Kommune}“.

    ``agenda_count`` ist die Zahl der Tagesordnungspunkte, die die Seite zeigt, ``tagesordnung`` sind diese
    Punkte (schon geladen, für den Hinweis auf eine öffentliche Sitzung).

    Die Veranstaltung (``Event``) erscheint nur mit Beginn und einem verlässlichen Ort samt Postanschrift
    (``services.sitzungsort``, Issue #939) – Google wertet sie sonst als ungültig. Ohne beides trägt die Seite nur
    die Brotkrumen. Veranstalter ist die Verwaltung der Kommune (mit der Seite der Kommune, ohne erreichbare Seite
    mit ihrer Webseite), Mitwirkende sind die Gremien (mit ihren Seiten). Eintritt frei (``offers`` mit Preis 0, ``isAccessibleForFree``) nur bei einer
    Sitzung mit ausdrücklich öffentlichem Teil, die nicht abgesagt ist.
    """
    from .services.sitzungsort import ist_platzhalter, sitzungsort

    gremium = meeting.get_display_name()
    body = meeting.body
    kommune = _kommune(body)
    datum = _datum(meeting.start)
    haupt = f"{kuerzen(gremium, TITEL_BETREFF_MAX)} am {datum}" if datum else kuerzen(gremium, TITEL_BETREFF_MAX)
    title = mit_kommune(haupt, kommune, " – ")

    # Zahl der Gremien aus den vorgeladenen Gremien (Namen mit Komma sind sonst mehrere)
    anzahl = len([org for org in meeting.organizations.all() if org.name]) or None
    wo = in_committee(gremium, anzahl)
    satz = " ".join(t for t in ("Sitzung", wo, f"am {_uhrzeit(meeting.start)}" if meeting.start else "") if t)
    if not ist_platzhalter(meeting.location_name):
        satz += f", {kuerzen(meeting.location_name, _ORT_MAX)}"
    teile = [f"{kommune}: {satz}." if kommune else f"{satz}."]
    if meeting.cancelled:
        teile.append("Die Sitzung ist abgesagt.")
    if agenda_count:
        teile.append(
            "Tagesordnung mit einem Punkt." if agenda_count == 1 else f"Tagesordnung mit {agenda_count} Punkten."
        )
    # Nie leer: Der erste Satz nennt mindestens „Sitzung“; der Titel ist nur die Rückfallebene
    description = beschreibung(*teile) or haupt
    canonical = build_canonical_url(request)

    ort = sitzungsort(meeting, body) if meeting.start else None
    json_ld = None
    if ort is not None:
        eintritt_frei = not meeting.cancelled and ist_oeffentlich(tagesordnung)
        json_ld = _ohne_leere(
            {
                "@context": "https://schema.org",
                "@type": "Event",
                "name": haupt,
                "description": description,
                "url": canonical,
                "inLanguage": "de",
                "startDate": _zeitpunkt(meeting.start),
                "endDate": _zeitpunkt(meeting.end) if meeting.end else None,
                "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
                "eventStatus": "https://schema.org/EventCancelled"
                if meeting.cancelled
                else "https://schema.org/EventScheduled",
                # Ohne Raumangabe heißt der Ort wie die Gemeinde (Google erwartet location.name)
                "location": {"@type": "Place", "name": ort.name or ort.ort, "address": ort.postanschrift()},
                "image": _bild_der_kommune(body),
                "organizer": _verwaltung(kommune, seite_der_kommune(body) or _webseite(body))
                or {"@type": "Organization", "name": "Kommune"},
                "performer": _gremien_der_sitzung(meeting),
                "isAccessibleForFree": True if eintritt_frei else None,
                "offers": {
                    "@type": "Offer",
                    "price": "0",
                    "priceCurrency": "EUR",
                    "availability": "https://schema.org/InStock",
                    "url": canonical,
                    "validFrom": _gueltig_ab(meeting),
                }
                if eintritt_frei
                else None,
            }
        )

    return SEOContext(
        title=title,
        description=description,
        canonical_url=canonical,
        og_type="event",
        json_ld=json_ld,
        keywords=["Sitzung", gremium, kommune],
        breadcrumbs=brotkrumen(body, haupt, canonical),
    )


def _art_des_gremiums(organization: Any) -> str:
    return " ".join(str(organization.classification or organization.organization_type or "").split())


def get_organization_seo(
    organization: Any,
    request: HttpRequest,
    mitglieder: int | None = None,
    naechste_sitzung: Any = None,
) -> SEOContext:
    """
    SEO-Kontext einer Gremienseite: „{Gremium} – {Kommune}“.

    Die Description nennt die Art (Rat, Ausschuss, Fraktion …), sofern der Name sie nicht schon trägt, die Zahl
    der laufenden Mitglieder und die nächste Sitzung – beides lädt die Seite ohnehin.
    """
    name = organization.name or organization.short_name or "Gremium"
    kommune = _kommune(organization.body)
    title = mit_kommune(kuerzen(name, TITEL_BETREFF_MAX), kommune, " – ")
    art = _art_des_gremiums(organization)

    kopf = name if not art or art.lower() in name.lower() else f"{name} ({art})"
    teile = [f"{mit_kommune(kopf, kommune)}."]
    if mitglieder:
        mitglied = "1 aktuelles Mitglied" if mitglieder == 1 else f"{mitglieder} aktuelle Mitglieder"
        teile.append(f"{mitglied}, Sitzungstermine und Tagesordnungen.")
    else:
        teile.append("Mitglieder, Sitzungstermine und Tagesordnungen.")
    if naechste_sitzung is not None and getattr(naechste_sitzung, "start", None):
        teile.append(f"Nächste Sitzung am {_datum(naechste_sitzung.start)}.")
    description = beschreibung(*teile)
    canonical = build_canonical_url(request)

    json_ld = _ohne_leere(
        {
            "@context": "https://schema.org",
            "@type": "Organization",
            "name": organization.name,
            "alternateName": organization.short_name if organization.short_name != organization.name else None,
            "description": description,
            "url": canonical,
            "areaServed": _gebiet(kommune),
            "parentOrganization": _verwaltung(kommune),
        }
    )

    return SEOContext(
        title=title,
        description=description,
        canonical_url=canonical,
        og_type="website",
        json_ld=json_ld,
        keywords=[name, art or "Gremium", kommune],
        breadcrumbs=brotkrumen(organization.body, kuerzen(name, TITEL_BETREFF_MAX), canonical),
    )


def _gremien_der_person(mitgliedschaften: Iterable[Any]) -> list[str]:
    """Namen der laufenden Gremien einer Person, Hauptorgan zuerst, ohne Fraktion und ohne Dopplungen."""
    from .services.personen_liste import ist_fraktion, ist_hauptorgan

    namen: dict[str, bool] = {}
    for mitgliedschaft in mitgliedschaften:
        org = mitgliedschaft.organization
        name = " ".join(str(org.name or org.short_name or "").split())
        if name and not ist_fraktion(org):
            namen[name] = namen.get(name, False) or ist_hauptorgan(org)
    return sorted(namen, key=lambda n: not namen[n])


def get_person_seo(
    person: Any,
    request: HttpRequest,
    funktion: str = "",
    fraktion: Any = None,
    mitgliedschaften: Iterable[Any] = (),
) -> SEOContext:
    """
    SEO-Kontext einer Personenseite: „{Name} – {Funktion}, {Kommune}“, ohne Funktion „{Name}, {Kommune}“.

    ``funktion`` ist die wichtigste laufende Rolle (``personen_liste.funktion_aus``, wie der Kopf der Seite),
    ``fraktion`` die Fraktion und ``mitgliedschaften`` die laufenden Mitgliedschaften der Seite. Niemand heißt
    hier pauschal „Ratsmitglied“.
    """
    name = person.display_name or str(person)
    kommune = _kommune(person.body)
    funktion = " ".join(str(funktion or "").split())
    haupt = f"{name} – {kuerzen(funktion, TITEL_BETREFF_MAX)}" if funktion else name
    title = mit_kommune(haupt, kommune)

    gremien = _gremien_der_person(mitgliedschaften)
    fraktion_name = " ".join(str(fraktion.short_name or fraktion.name or "").split()) if fraktion is not None else ""
    kopf = f"{name}, {funktion}" if funktion else name
    teile = [f"{mit_kommune(kopf, kommune)}."]
    if fraktion_name:
        # „Fraktion: GRÜNE“, nicht „Fraktion: Fraktion Bunte Liste“
        teile.append(f"Fraktion: {_FRAKTION_VORAN.sub('', fraktion_name) or fraktion_name}.")
    if gremien:
        weitere = len(gremien) - _GREMIEN_MAX
        liste = ", ".join(gremien[:_GREMIEN_MAX]) + (f" und {weitere} weitere" if weitere > 0 else "")
        teile.append(f"Mitglied in: {liste}.")
    else:
        teile.append("Mitgliedschaften in Gremien und Fraktionen.")
    description = beschreibung(*teile)
    canonical = build_canonical_url(request)

    zugehoerig = [_verwaltung(kommune)]
    if fraktion_name:
        zugehoerig.append({"@type": "Organization", "name": fraktion.name or fraktion_name})
    json_ld = _ohne_leere(
        {
            "@context": "https://schema.org",
            "@type": "Person",
            "name": name,
            "givenName": person.given_name,
            "familyName": person.family_name,
            "honorificPrefix": person.title,
            "jobTitle": funktion or None,
            "description": description,
            "url": canonical,
            "affiliation": zugehoerig,
            "memberOf": [{"@type": "Organization", "name": g} for g in gremien[:_MITGLIEDSCHAFTEN_LD_MAX]],
        }
    )

    return SEOContext(
        title=title,
        description=description,
        canonical_url=canonical,
        og_type="profile",
        json_ld=json_ld,
        keywords=[name, funktion or "Kommunalpolitik", kommune],
        breadcrumbs=brotkrumen(person.body, name, canonical),
    )


def get_decision_seo(item: Any, body: Any, status_label: str, request: HttpRequest) -> SEOContext:
    """
    SEO-Kontext eines öffentlichen Beschlusses (Umsetzungsstand, Issue #48): „{Beschluss} – Umsetzungsstand,
    {Kommune}“; die Description nennt Gremium, Datum, Stand der Umsetzung und den Anfang des Beschlusstexts.
    """
    kommune = _kommune(body)
    titel = kuerzen(item.name, TITEL_BETREFF_MAX) or "Beschluss"
    title = mit_kommune(f"{titel} – Umsetzungsstand", kommune)
    meeting = item.meeting
    gremium = getattr(getattr(meeting, "organization", None), "name", "") or ""
    datum = _datum(meeting.start) if meeting.start else ""
    satz = " ".join(t for t in ("Beschluss", in_committee(gremium), f"vom {datum}" if datum else "") if t)
    teile = [f"{kommune}: {satz}." if kommune else f"{satz}."]
    if status_label:
        teile.append(f"Stand der Umsetzung: {status_label}.")
    teile.append(item.resolution_text or "")
    description = beschreibung(*teile)
    canonical = build_canonical_url(request)

    json_ld = _ohne_leere(
        {
            "@context": "https://schema.org",
            "@type": "CreativeWork",
            "name": item.name,
            "identifier": getattr(item, "resolution_number", "") or None,
            "description": description,
            "url": canonical,
            "inLanguage": "de",
            "dateCreated": meeting.start.isoformat() if meeting.start else None,
            "creativeWorkStatus": status_label,
            "spatialCoverage": _gebiet(kommune),
            "author": _verwaltung(kommune),
        }
    )

    return SEOContext(
        title=title,
        description=description,
        canonical_url=canonical,
        og_type="article",
        json_ld=json_ld,
        keywords=["Beschluss", "Umsetzung", kommune],
        breadcrumbs=brotkrumen(body, titel, canonical),
    )


def get_question_seo(question: Any, request: HttpRequest) -> SEOContext:
    """SEO-Kontext einer öffentlichen Ratsfrage (schema.org QAPage): „{Betreff} – Frage an {Name}, {Kommune}“."""
    person = question.recipient
    body_name = _kommune(question.body)
    title = mit_kommune(f"{kuerzen(question.subject, TITEL_BETREFF_MAX)} – Frage an {person.display_name}", body_name)
    description = question.question_text[:157].rstrip() + ("…" if len(question.question_text) > 157 else "")

    question_ld = {
        "@type": "Question",
        "name": question.subject,
        "text": question.question_text,
        "dateCreated": (question.published_at or question.created_at).isoformat(),
        "author": {"@type": "Person", "name": question.questioner_name},
        "answerCount": 1 if question.is_answered else 0,
    }
    if question.is_answered:
        question_ld["acceptedAnswer"] = {
            "@type": "Answer",
            "text": question.answer_text,
            "dateCreated": question.answered_at.isoformat() if question.answered_at else None,
            "author": {"@type": "Person", "name": person.display_name},
        }
    json_ld = {"@context": "https://schema.org", "@type": "QAPage", "mainEntity": question_ld}

    return SEOContext(
        title=title,
        description=description[:160],
        canonical_url=build_canonical_url(request),
        og_type="article",
        json_ld=json_ld,
        keywords=["Ratsfrage", person.display_name, body_name, question.get_topic_display()],
    )


def get_page_seo(
    request: HttpRequest,
    title: str,
    description: str,
    body: Any = None,
    robots: str = "index, follow",
    keywords: list[str] | None = None,
) -> SEOContext:
    """Generischer SEO-Kontext für Listen- und Funktionsseiten.

    Ergänzt den Kommune-Namen im Titel, setzt Canonical/OG/Twitter und
    erlaubt noindex für personalisierte Seiten (z.B. Merkliste).
    """
    if body:
        title = f"{title} - {body.get_display_name()}"

    return SEOContext(
        title=title[:60],
        description=description[:160],
        canonical_url=build_canonical_url(request),
        og_type="website",
        robots=robots,
        keywords=keywords or ["Kommunalpolitik", "Ratsinformationen", "Transparenz"],
    )


def get_portal_home_seo(request: HttpRequest, body: Any = None) -> SEOContext:
    """SEO für die Portal-Startseite: Übersicht einer Kommune (``/insight/k/<slug>/``) bzw. Kommunenauswahl."""
    if body:
        name = _kommune(body)
        title = f"Ratsinformationen {name} – Sitzungen, Vorlagen, Beschlüsse"
        description = beschreibung(
            f"Ratsinformationen {name}: Sitzungen mit Tagesordnung, Vorlagen, Anträge und Beschlüsse aus Rat und "
            "Ausschüssen – verständlich aufbereitet und durchsuchbar."
        )
    else:
        title = "Kommune wählen – mandari Insight"
        description = (
            "Lokalpolitik, die alle verstehen: Wählen Sie Ihre Kommune und sehen Sie "
            "Sitzungen, Vorgänge, Gremien und die Menschen hinter den Entscheidungen."
        )

    return SEOContext(
        title=title,
        description=description[:160],
        canonical_url=build_canonical_url(request),
        og_type="website",
        keywords=["Ratsinformationen", _kommune(body), "Sitzungen", "Vorlagen", "Beschlüsse"] if body else [],
    )
