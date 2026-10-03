# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Nummernkreise für Vorlagen und Drucksachen (Issue #150).

Jede Vorlage bekommt ihre Nummer aus einem Nummernkreis des Mandanten – beim Anlegen oder bei
der Freigabe, je nach Einstellung. Eine vergebene Nummer ändert sich nicht mehr und wird nie
wiederverwendet: Der Zähler steigt nur und wird in derselben Transaktion wie die Vorlage
gespeichert, sodass ein fehlgeschlagenes Speichern keine Nummer verbraucht.

Platzhalter im Muster:

========== ==========================================================================
``{lfd}``   laufende Nummer, ``{lfd:4}`` mit führenden Nullen auf vier Stellen
``{jahr}``  Jahr der Vergabe (vierstellig), ``{jj}`` zweistellig
``{wp}``    Nummer der Wahlperiode (Einstellungen → Wahlperioden)
``{prefix}`` Präfix des Nummernkreises (z. B. ``AN``)
``{gremium}`` Kurzname des federführenden Gremiums (eigener Zähler je Gremium)
``{koerperschaft}`` Kurzname der Körperschaft der Vorlage (eigener Zähler je Körperschaft)
========== ==========================================================================

Körperschaften (Issue #756): Ein Nummernkreis gilt für alle Körperschaften des Mandanten oder nur für eine
(``SessionNumberRange.body``). Für eine Vorlage zählt zuerst ein Kreis ihrer Körperschaft, dann ein
allgemeiner; jeweils der Kreis der Vorlagenart vor dem für alle Arten. Vorlagennummern bleiben je Mandant
eindeutig – gleiche Muster in zwei Körperschaften brauchen ``{koerperschaft}`` oder ein eigenes Präfix.

Unternummern (Ergänzung, Neufassung, Antwort …) hängen an der Nummer der Bezugsvorlage:
``{parent}.{sub}`` ergibt „22-0593.1“, ``{parent}/{sub}`` ergibt „V/0599/2024/1“.

Die Presets bilden verbreitete Praxis ab (Stand 09/2026, recherchiert an öffentlichen
Ratsinformationssystemen): Bezirksversammlungen in Stadtstaaten zählen je Bezirk und Wahlperiode
über alle Drucksachenarten; NRW-Kommunen meist je Jahr, oft mit Präfixen je Art.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any, cast

from django.db import transaction
from django.utils import timezone

if TYPE_CHECKING:
    from apps.session.models import SessionBody, SessionNumberRange, SessionPaper, SessionTenant

TOKEN_RE = re.compile(r"\{(\w+)(?::(\d{1,2}))?\}")
HAUPT_PLATZHALTER = {"lfd", "jahr", "jj", "wp", "prefix", "gremium", "koerperschaft"}
UNTER_PLATZHALTER = {"parent", "sub"}
#: Status, ab denen eine Vorlage als freigegeben gilt (Vergabe „bei Freigabe“)
FREIGEGEBEN = {"approved", "scheduled", "completed"}
#: Vorlagenart, die eine Unternummer je Bezugsart bekommt
ART_JE_BEZUG = {"answer": "answer", "recommendation": "recommendation", "amendment": "amendment"}


class NumberingError(ValueError):
    """Die Nummer lässt sich mit der aktuellen Konfiguration nicht bilden."""


# ---------------------------------------------------------------------------
# Muster
# ---------------------------------------------------------------------------


def validate_pattern(pattern: str, reset: str) -> list[str]:
    """Fehlermeldungen zu einem Hauptmuster (leer = gültig)."""
    fehler: list[str] = []
    namen = [m.group(1) for m in TOKEN_RE.finditer(pattern or "")]
    unbekannt = sorted(set(namen) - HAUPT_PLATZHALTER)
    if unbekannt:
        fehler.append("Unbekannte Platzhalter: " + ", ".join("{" + n + "}" for n in unbekannt))
    if namen.count("lfd") != 1:
        fehler.append("Das Muster braucht genau einmal die laufende Nummer {lfd} (z. B. {lfd:4}).")
    # Der Zählerbereich muss im Muster sichtbar sein, sonst entstehen gleiche Nummern
    if reset == "yearly" and not {"jahr", "jj"} & set(namen):
        fehler.append("Bei jährlichem Zurücksetzen muss das Jahr ({jahr} oder {jj}) im Muster stehen.")
    if reset == "term" and "wp" not in namen:
        fehler.append("Beim Zurücksetzen je Wahlperiode muss {wp} im Muster stehen.")
    if TOKEN_RE.sub("", pattern or "").count("{") or TOKEN_RE.sub("", pattern or "").count("}"):
        fehler.append("Geschweifte Klammern nur für Platzhalter verwenden.")
    return fehler


def validate_sub_pattern(sub_pattern: str) -> list[str]:
    namen = [m.group(1) for m in TOKEN_RE.finditer(sub_pattern or "")]
    fehler = []
    if sorted(set(namen) - UNTER_PLATZHALTER):
        fehler.append("Unternummern kennen nur {parent} und {sub}.")
    if namen.count("parent") != 1 or namen.count("sub") != 1:
        fehler.append("Das Unternummern-Muster braucht genau einmal {parent} und {sub}.")
    return fehler


def _format(pattern: str, werte: dict[str, Any]) -> str:
    def ersetzen(m: re.Match[str]) -> str:
        name, breite = m.group(1), m.group(2)
        wert = werte.get(name)
        if wert is None or wert == "":
            raise NumberingError(f"Für den Platzhalter {{{name}}} fehlt ein Wert.")
        if breite and isinstance(wert, int):
            return f"{wert:0{int(breite)}d}"
        return str(wert)

    return TOKEN_RE.sub(ersetzen, pattern)


# ---------------------------------------------------------------------------
# Kontext der Vergabe
# ---------------------------------------------------------------------------


def _wahlperiode(tenant: SessionTenant, stichtag: date) -> int:
    from apps.session.models import SessionLegislativeTerm

    term = cast(Any, SessionLegislativeTerm).for_date(tenant, stichtag)
    if term is None or term.number is None:
        raise NumberingError(
            "Das Muster enthält {wp}, aber für den Mandanten ist keine aktuelle Wahlperiode mit Nummer "
            "hinterlegt (Einstellungen → Wahlperioden)."
        )
    return int(term.number)


def _body_of(number_range: SessionNumberRange, paper: SessionPaper | None) -> SessionBody | None:
    """Körperschaft für {koerperschaft}: die der Vorlage, ohne Vorlage die des Nummernkreises."""
    if paper is not None and paper.body_id is not None:
        return paper.body
    if number_range.body_id is not None:
        return number_range.body
    return None


def _werte(
    number_range: SessionNumberRange, paper: SessionPaper | None, stichtag: date, *, vorschau: bool = False
) -> dict[str, Any]:
    """
    Werte der Platzhalter am Stichtag. ``vorschau``: ohne federführendes Gremium mit Kurzname steht
    „GREMIUM“ für {gremium} (ohne Körperschaft mit Kurzname „KS“ für {koerperschaft}) – die Vorschau zeigt
    dann das Schema statt eines Fehlers.
    """
    muster = number_range.pattern
    werte: dict[str, Any] = {
        "jahr": stichtag.year,
        "jj": f"{stichtag.year % 100:02d}",
        "prefix": number_range.prefix,
    }
    if "{wp" in muster:
        werte["wp"] = _wahlperiode(number_range.tenant, stichtag)
    if "{gremium" in muster:
        gremium = getattr(paper, "main_organization", None) if paper is not None else None
        kurz = (gremium.short_name or "").strip() if gremium else ""
        if not kurz and vorschau:
            kurz = "GREMIUM"
        if not kurz:
            raise NumberingError("Das Muster enthält {gremium}: Bitte ein federführendes Gremium mit Kurzname wählen.")
        werte["gremium"] = kurz
    if "{koerperschaft" in muster:
        body = _body_of(number_range, paper)
        kurz = (body.short_name or "").strip() if body else ""
        if not kurz and vorschau:
            kurz = "KS"
        if not kurz:
            raise NumberingError(
                "Das Muster enthält {koerperschaft}: Bitte für die Körperschaft der Vorlage einen Kurznamen "
                "hinterlegen (Einstellungen → Körperschaften)."
            )
        werte["koerperschaft"] = kurz
    return werte


def _scope(number_range: SessionNumberRange, werte: dict[str, Any]) -> str:
    teile = []
    if number_range.reset == "yearly":
        teile.append(str(werte["jahr"]))
    elif number_range.reset == "term":
        teile.append(f"wp{werte['wp']}")
    if "gremium" in werte:
        teile.append(str(werte["gremium"]))
    if "koerperschaft" in werte:
        teile.append(f"ks:{werte['koerperschaft']}")
    return "|".join(teile)


def range_for(tenant: SessionTenant, paper_type: str, body: SessionBody | None = None) -> SessionNumberRange | None:
    """
    Aktiver Nummernkreis für eine Vorlagenart in einer Körperschaft; None ohne Kreis.

    Vorrang (Issue #756): Kreis der Körperschaft vor dem Kreis für alle Körperschaften, jeweils der Kreis der
    Vorlagenart vor dem für alle Arten. Kreise anderer Körperschaften gelten nie. Ohne ``body`` (Vorschau ohne
    Vorlage) zählen nur die Kreise für alle Körperschaften.
    """
    kandidaten: dict[tuple[bool, bool], SessionNumberRange] = {}
    body_id = body.pk if body is not None else None
    for rng in tenant.number_ranges.filter(is_active=True).order_by("order", "created_at"):
        if rng.body_id is not None and rng.body_id != body_id:
            continue
        arten = rng.paper_types or []
        if arten and paper_type not in arten:
            continue
        kandidaten.setdefault((rng.body_id is not None, bool(arten)), rng)
    for schluessel in ((True, True), (True, False), (False, True), (False, False)):
        if schluessel in kandidaten:
            return kandidaten[schluessel]
    return None


def is_due(number_range: SessionNumberRange, paper: SessionPaper) -> bool:
    if number_range.assign_on == "release":
        return paper.status in FREIGEGEBEN
    return True


# ---------------------------------------------------------------------------
# Vergabe
# ---------------------------------------------------------------------------


def _reference_taken(paper: SessionPaper, reference: str) -> bool:
    from apps.session.models import SessionPaper

    return SessionPaper.objects.filter(tenant_id=paper.tenant_id, reference=reference).exclude(pk=paper.pk).exists()


def _naechster_zaehler(number_range: SessionNumberRange, scope: str) -> int:
    """Zähler atomar erhöhen: Zeilensperre auf dem Zählerstand, Anlage bei Bedarf."""
    from apps.session.models import SessionNumberCounter

    counter, _ = SessionNumberCounter.objects.select_for_update().get_or_create(number_range=number_range, scope=scope)
    counter.value += 1
    counter.save(update_fields=["value", "updated_at"])
    return counter.value


def _assign_sub_number(paper: SessionPaper) -> bool:
    from apps.session.models import SessionPaper

    parent = SessionPaper.objects.select_for_update().get(pk=cast(Any, paper.parent_paper_id))
    if not parent.reference:
        return False  # Bezugsvorlage hat selbst noch keine Nummer
    number_range = range_for(parent.tenant, parent.paper_type, parent.body)
    sub_pattern = number_range.sub_pattern if number_range else "{parent}.{sub}"
    belegt = {
        n
        for n in SessionPaper.objects.filter(parent_paper=parent, sub_number__isnull=False).values_list(
            "sub_number", flat=True
        )
        if n is not None
    }
    sub = max(belegt, default=0) + 1
    while True:
        reference = _format(sub_pattern, {"parent": parent.reference, "sub": sub})
        if not _reference_taken(paper, reference):
            break
        sub += 1
    paper.sub_number = sub
    paper.reference = reference
    paper.reference_assigned_at = timezone.now()
    return True


def assign_if_due(paper: SessionPaper) -> bool:
    """
    Nummer vergeben, wenn sie fällig ist; True, wenn ``paper`` jetzt eine Nummer trägt.

    Muss innerhalb einer Transaktion laufen (``SessionPaper.save`` sorgt dafür). Wirft
    ``NumberingError``, wenn die Konfiguration keine Nummer bilden kann.
    """
    if paper.reference:
        return False
    if paper.parent_paper_id and paper.relation_type:
        return _assign_sub_number(paper)
    number_range = range_for(paper.tenant, paper.paper_type, paper.body)
    if number_range is None or not is_due(number_range, paper):
        return False
    stichtag = timezone.localdate()
    werte = _werte(number_range, paper, stichtag)
    scope = _scope(number_range, werte)
    for _ in range(1000):
        werte["lfd"] = _naechster_zaehler(number_range, scope)
        reference = _format(number_range.pattern, werte)
        # Altbestand (z. B. aus ALLRIS übernommen) kann Nummern schon belegen: überspringen, nie doppelt
        if not _reference_taken(paper, reference):
            paper.reference = reference
            paper.reference_assigned_at = timezone.now()
            return True
    raise NumberingError("Keine freie Nummer gefunden – bitte den Zählerstand prüfen.")


# ---------------------------------------------------------------------------
# Vorschau, Startwert, Presets
# ---------------------------------------------------------------------------


def preview(number_range: SessionNumberRange, paper: SessionPaper | None = None) -> str:
    """Nächste Nummer ohne Vergabe (Einstellungen, Formularhinweis); Fehlertext statt Ausnahme."""
    from apps.session.models import SessionNumberCounter

    try:
        werte = _werte(number_range, paper, timezone.localdate(), vorschau=True)
    except NumberingError as exc:
        return f"– ({exc})"
    scope = _scope(number_range, werte)
    stand = (
        SessionNumberCounter.objects.filter(number_range=number_range, scope=scope)
        .values_list("value", flat=True)
        .first()
        or 0
    )
    werte["lfd"] = stand + 1
    return _format(number_range.pattern, werte)


def current_scope(number_range: SessionNumberRange) -> str:
    if "{koerperschaft" in number_range.pattern and number_range.body_id is None:
        # Der Kreis zählt je Körperschaft – für welche der Startwert gelten soll, ist offen
        raise NumberingError(
            "Bei Nummernkreisen für alle Körperschaften mit {koerperschaft} den Startwert über den Admin setzen."
        )
    werte = _werte(number_range, None, timezone.localdate()) if "{gremium" not in number_range.pattern else None
    if werte is None:
        raise NumberingError("Bei Nummernkreisen je Gremium den Startwert über den Admin setzen.")
    return _scope(number_range, werte)


def set_next_number(number_range: SessionNumberRange, next_number: int) -> None:
    """
    Nächste laufende Nummer im aktuellen Zählerbereich setzen – z. B. beim Umstieg aus einem
    Altsystem mitten in der Wahlperiode (bisher bis 22-2614 vergeben → nächste 2615). Nur
    aufwärts: Bereits vergebene Nummern dürfen nie ein zweites Mal entstehen.
    """
    from apps.session.models import SessionNumberCounter

    if next_number < 1:
        raise NumberingError("Die nächste Nummer muss mindestens 1 sein.")
    with transaction.atomic():
        counter, _ = SessionNumberCounter.objects.select_for_update().get_or_create(
            number_range=number_range, scope=current_scope(number_range)
        )
        if next_number - 1 < counter.value:
            raise NumberingError(
                f"Bis {counter.value} ist bereits vergeben – die nächste Nummer kann nur höher gesetzt werden."
            )
        counter.value = next_number - 1
        counter.save(update_fields=["value", "updated_at"])


@dataclass(frozen=True)
class Kreis:
    name: str
    pattern: str
    reset: str = "yearly"
    assign_on: str = "create"
    paper_types: tuple[str, ...] = ()
    prefix: str = ""
    sub_pattern: str = "{parent}.{sub}"


@dataclass(frozen=True)
class Preset:
    label: str
    beschreibung: str
    reference_label: str
    kreise: tuple[Kreis, ...] = field(default_factory=tuple)


VERWALTUNG = ("proposal", "report", "answer", "recommendation", "resolution", "bylaw", "budget", "other")
POLITIK = ("motion", "inquiry", "major_inquiry", "amendment")

PRESETS: dict[str, Preset] = {
    "standard": Preset(
        "Standard (Jahreszähler)",
        "Eine Nummernfolge je Jahr für alle Vorlagen: V/2026/0001.",
        "Vorlagen-Nr.",
        (Kreis("Vorlagen", "V/{jahr}/{lfd:4}", sub_pattern="{parent}/{sub}"),),
    ),
    "stadtstaat_bezirk": Preset(
        "Bezirksversammlung (Stadtstaat)",
        "Ein Zähler je Bezirk und Wahlperiode über alle Drucksachenarten: 22-0593, Unternummern 22-0593.1.",
        "Drucksache",
        (Kreis("Drucksachen", "{wp}-{lfd:4}", reset="term", sub_pattern="{parent}.{sub}"),),
    ),
    "nrw_verwaltung_politik": Preset(
        "Verwaltung und Politik getrennt (z. B. Köln)",
        "Verwaltungsvorlagen 1344/2026, Anträge und Anfragen der Politik AN/1492/2026; Fortschreibung /1.",
        "Vorlagen-Nr.",
        (
            Kreis("Verwaltungsvorlagen", "{lfd:4}/{jahr}", paper_types=VERWALTUNG, sub_pattern="{parent}/{sub}"),
            Kreis(
                "Anträge und Anfragen",
                "{prefix}/{lfd:4}/{jahr}",
                paper_types=POLITIK,
                prefix="AN",
                sub_pattern="{parent}/{sub}",
            ),
        ),
    ),
    "nrw_praefix_je_art": Preset(
        "Präfix je Vorlagenart (z. B. Münster)",
        "V/0599/2026 für Vorlagen, A/0012/2026 für Anträge, AF/0003/2026 für Anfragen; Ergänzung V/0599/2026/1.",
        "Vorlagen-Nr.",
        (
            Kreis(
                "Vorlagen", "{prefix}/{lfd:4}/{jahr}", paper_types=VERWALTUNG, prefix="V", sub_pattern="{parent}/{sub}"
            ),
            Kreis(
                "Anträge",
                "{prefix}/{lfd:4}/{jahr}",
                paper_types=("motion", "amendment"),
                prefix="A",
                sub_pattern="{parent}/{sub}",
            ),
            Kreis(
                "Anfragen",
                "{prefix}/{lfd:4}/{jahr}",
                paper_types=("inquiry", "major_inquiry"),
                prefix="AF",
                sub_pattern="{parent}/{sub}",
            ),
        ),
    ),
    "nrw_vo_kurzjahr": Preset(
        "VO mit zweistelligem Jahr (z. B. Wuppertal)",
        "VO/1044/26, Ergänzung VO/1044/26/1.",
        "Vorlagen-Nr.",
        (Kreis("Vorlagen", "VO/{lfd:4}/{jj}", sub_pattern="{parent}/{sub}"),),
    ),
    "nrw_fuenfstellig": Preset(
        "Fünfstellig mit Jahr (z. B. Dortmund)",
        "22957-26, Ergänzung 22957-26-E1.",
        "Drucksache Nr.",
        (Kreis("Drucksachen", "{lfd:5}-{jj}", sub_pattern="{parent}-E{sub}"),),
    ),
    "gemeinde": Preset(
        "Kleine Gemeinde",
        "126/2026, Ergänzung 126/2026/1.",
        "Vorlagen-Nr.",
        (Kreis("Vorlagen", "{lfd}/{jahr}", sub_pattern="{parent}/{sub}"),),
    ),
}


def apply_preset(tenant: SessionTenant, key: str) -> list[SessionNumberRange]:
    """
    Preset übernehmen: passende bestehende Kreise (gleiches Muster, gleiche Arten) werden
    reaktiviert und behalten ihre Zählerstände, alle anderen deaktiviert – nie gelöscht, damit
    vergebene Nummern nachvollziehbar bleiben.
    """
    from apps.session.models import SessionNumberRange

    preset = PRESETS[key]
    with transaction.atomic():
        # Nur die Kreise für alle Körperschaften; Kreise einzelner Körperschaften bleiben unberührt (Issue #756)
        bestehend = list(SessionNumberRange.objects.select_for_update().filter(tenant=tenant, body__isnull=True))
        aktiv = []
        for order, kreis in enumerate(preset.kreise):
            treffer = next(
                (
                    r
                    for r in bestehend
                    if r.pattern == kreis.pattern
                    and sorted(r.paper_types or []) == sorted(kreis.paper_types)
                    and r.prefix == kreis.prefix
                ),
                None,
            )
            rng = treffer or SessionNumberRange(tenant=tenant)
            rng.name, rng.pattern, rng.reset, rng.assign_on = kreis.name, kreis.pattern, kreis.reset, kreis.assign_on
            rng.paper_types, rng.prefix, rng.sub_pattern = list(kreis.paper_types), kreis.prefix, kreis.sub_pattern
            rng.order, rng.is_active = order, True
            rng.save()
            aktiv.append(rng)
        SessionNumberRange.objects.filter(tenant=tenant, body__isnull=True).exclude(
            pk__in=[r.pk for r in aktiv]
        ).update(is_active=False)
        SessionTenantModel = type(tenant)
        SessionTenantModel.objects.filter(pk=tenant.pk).update(reference_label=preset.reference_label)
        tenant.reference_label = preset.reference_label
    return aktiv


def ensure_default(tenant: SessionTenant) -> None:
    """Neuer Mandant ohne Nummernkreis: Standard-Preset anlegen."""
    if not tenant.number_ranges.exists():
        apply_preset(tenant, "standard")
