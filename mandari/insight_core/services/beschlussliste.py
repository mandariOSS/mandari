# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Beschlüsse als Liste der Entscheidungen (Issue #1270).

Die Seite „Beschlüsse“ zeigt, was Rat, Ausschüsse und Bezirksvertretungen einer Kommune entschieden haben: die
öffentlichen Tagesordnungspunkte mit Ergebnis aus vergangenen, nicht abgesagten Sitzungen, nach Sitzung gruppiert,
neueste Sitzung zuerst, innerhalb der Sitzung in der Reihenfolge der Tagesordnung. Je Punkt das Ergebnis-Etikett
(``services.ergebnis``), der Titel des Vorgangs und rechts die antragstellenden Fraktionen (``VorgangUrheber``) bzw.
„Verwaltung“ oder „Bürgerantrag“ nach der Vorlagenart.

Regeln, die hier und nur hier stehen:

- **Formalien** (``ist_formalie``, ``FORMALIEN``): Eröffnung, Feststellungen zur Sitzung (Beschlussfähigkeit,
  Einberufung, Tagesordnung), Tagesordnung und Niederschrift, Bestellung der Schriftführung, Verpflichtung von
  Mitgliedern, Schließung, Fragestunden, Sammelpunkte ohne eigenen Inhalt („Mitteilungen“, „Verschiedenes“) und die
  Bekanntgabe nichtöffentlicher Beschlüsse entfallen. Der Ausdruck läuft gleich in Python und in der Datenbank
  (``name__iregex``): nur Zeichenklassen, ``\\s``, Gruppen ``(?:…)`` und Anker, kein ``\\b`` (das hieße in PostgreSQL
  Rückschritt). „Feststellung des Jahresabschlusses“, „Verpflichtungsermächtigung“, „Verpflichtung der Stadt zur …“
  oder „Änderung des Protokolls zur Städtepartnerschaft“ sind keine Formalien.
- **Gremiengruppe** (``gremiengruppe``): Bezirksvertretungen, Bezirks- und Ortsbeiräte, Orts- und Stadtbezirksräte
  bilden die Gruppe „Bezirksvertretungen“; Rat, Kreistag, Ausschüsse und Kommissionen „Rat und Ausschüsse“. Beiräte,
  Aufsichtsräte, Gesellschaften und Fraktionen gehören zu keiner Gruppe und erscheinen unter „Alle“.
- **Stellungnahmen bündeln** (``_stellungnahmen_buendeln``): Eine Stellungnahme oder Antwort der Verwaltung, die in
  derselben Sitzung mit ihrem Antrag bzw. ihrer Anfrage beraten wurde, ist keine eigene Zeile, sondern ein Satz
  darunter („Mit Stellungnahme der Verwaltung.“). Zugehörig ist sie über ``superordinatedPaper`` der Quelle oder die
  Stammnummer (``260745-01 ST`` gehört zu ``260745``, ``V/0591/2026/1`` zu ``V/0591/2026``).
- **Vertagungen bündeln** (``_vertagungen_buendeln``): Ab ``BUENDEL_AB`` gleich vertagten bzw. abgesetzten Punkten
  derselben Vorlagenart in einer Sitzung (etwa 15 Große Anfragen oder eine abgebrochene Sitzung) steht eine Zeile
  „15 Punkte vertagt“, die Punkte zum Aufklappen darunter.

Abfragen je Seite, unabhängig von der Zahl der Punkte (keine Abfrage je Punkt): Gremien der Kommune, Gremien mit
Sitzungen im Zeitraum, bei einem Ergebnisfilter die Ergebnistexte des Zeitraums, Zahl der Punkte, Zahl und Ausschnitt
der Sitzungen (``SITZUNGEN_JE_SEITE`` je Seite), die Punkte dieser Sitzungen, ihre Gremien, die beratenen Vorgänge und
deren Fraktionen. Geblättert wird nach Sitzungen, damit eine Sitzung nie über zwei Seiten läuft.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Final

from django.core.paginator import Page, Paginator
from django.db.models import Exists, OuterRef, QuerySet
from django.http import QueryDict
from django.urls import reverse
from django.utils import timezone

from ..models import (
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlLegislativeTerm,
    OParlMeeting,
    OParlOrganization,
    VorgangUrheber,
    withdrawn_q,
)
from ..ranking import RANKING_RULES
from .ergebnis import (
    ABGELEHNT,
    ABGESETZT,
    BESCHLOSSEN,
    KENNTNIS,
    NEUTRAL,
    VERTAGT,
    VERWIESEN,
    VOKABULAR,
    Ergebnis,
    einordnen,
    ergebnis,
)

# =============================================================================
# Formalien
# =============================================================================

_KEIN_BUCHSTABE: Final = "[^a-zäöüß]"
#: Nummer vor dem Titel („1.“, „TOP 3:“, „Ö 1.2“), wie sie manche Quellen in den Namen schreiben
_ANFANG: Final = rf"^{_KEIN_BUCHSTABE}*(?:(?:top|ö)\s*[0-9]{_KEIN_BUCHSTABE}*)?"
#: „Protokoll“ ist nur mit Bezug zur Sitzung eine Formalie: am Ende des Titels, mit Nummer oder Datum, „über die
#: 12. Sitzung“, „der letzten Sitzung“, „des Rates“; nicht „Änderung des Protokolls zur Städtepartnerschaft“
_PROTOKOLL_DER_SITZUNG: Final = (
    rf"protokoll(?:s|es|e|en)?(?:{_KEIN_BUCHSTABE}*$|{_KEIN_BUCHSTABE}*(?:[0-9]|nr{_KEIN_BUCHSTABE}|nummer|teil"
    r"|öffentlich|nichtöffentlich)|\s+vom\s+[0-9]|\s+(?:über|der|des|zu[rm]?|aus)\s+(?:\S+\s+){0,3}\S*(?:sitzung"
    rf"|sitzungen|rates|rats|ausschusses|vertretung|beirats|beirates|tages|tags)(?:{_KEIN_BUCHSTABE}|$))"
)
#: Personen, die zu Beginn ihres Mandats verpflichtet werden („Verpflichtung eines neuen Ratsmitglieds“, „… von Frau
#: Müller“, „… sachkundiger Bürger“); nicht „Verpflichtung der Stadt zur Übernahme der Kosten“ oder „… der
#: Mitgliedsgemeinden zur Umlage“
_VERPFLICHTETE: Final = (
    rf"(?:\S*mitglied(?:er|ern|s|es)?(?:{_KEIN_BUCHSTABE}|$)|\S*(?:bezirksvertreter|verordnete|abgeordnete"
    r"|beigeordnete|sachkundig|nachfolger|nachrücker|vorsteher|ratsherr|ratsfrau|räte|rätin)|herrn?\s|frau\s)"
)
_FORMALIEN_TEILE: Final = (
    # Eröffnung der Sitzung, auch „Eröffnung, Begrüßung und …“, nicht „Eröffnung des Hallenbads“
    rf"eröffnung(?:{_KEIN_BUCHSTABE}*$|\s+der\s+(?:öffentlichen\s+)?sitzung|\s*,|\s+(?:und|sowie)\s+(?:begrüßung"
    r"|feststellung))",
    rf"begrüßung(?:{_KEIN_BUCHSTABE}*$|\s*,|\s+und\s+eröffnung)",
    r"feststellung\s+der\s+(?:beschlussfähigkeit|anwesenheit|ordnungsgemäßen|ordnungsmäßigen|form-|frist|rechtzeitigen"
    r"|einberufung|ladung|einladung|tagesordnung)",
    r"(?:genehmigung|billigung|anerkennung|unterzeichnung|feststellung|festsetzung|festlegung|änderung|ergänzung"
    r"|erweiterung|beschlussfassung\s+über)\s+(?:der|des|die)\s+(?:sitzungsprotokoll|(?:sitzungs)?niederschrift"
    rf"|tagesordnung|{_PROTOKOLL_DER_SITZUNG})",
    rf"sitzungsprotokoll|(?:sitzungs)?niederschrift(?:{_KEIN_BUCHSTABE}|$)",
    rf"tagesordnung{_KEIN_BUCHSTABE}*$",
    r"anträge\s+zur\s+tagesordnung",
    r"bestellung\s+(?:eines|einer|des|der)\s+(?:schrift|protokoll)führ",
    # Verpflichtung neuer Mitglieder, nicht die „Verpflichtungsermächtigung“ im Haushalt und nicht „Verpflichtung der
    # Stadt zur …“
    r"(?:einführung\s+und\s+)?verpflichtung(?:\s+und\s+einführung)?\s+(?:(?:von|eines|einer|des|der|die|neuer|neuen"
    rf"|neu|gewählter|gewählten|stellvertretende[nr]?)\s+)*{_VERPFLICHTETE}",
    r"(?:schließung|schluss|ende)\s+der\s+(?:öffentlichen\s+)?sitzung",
    r"(?:einwohner(?:innen)?|bürger(?:innen)?)?fragestunde",
    # Sammelpunkte ohne eigenen Inhalt, nur als ganzer Titel („Mitteilungen der Verwaltung“, nicht „Mitteilung zum …“)
    rf"(?:mitteilungen|bekanntgaben|anfragen|anträge|verschiedenes|sonstiges)(?:\s+der\s+verwaltung|\s+des\s+\S+)?"
    rf"{_KEIN_BUCHSTABE}*$",
    r"bekanntgabe\s+(?:der\s+|von\s+)?(?:in|aus)\s+(?:der\s+)?nicht\s?öffentlich",
)
#: Formalien im Titel eines Tagesordnungspunkts (ohne Unterscheidung der Groß- und Kleinschreibung); dieselbe
#: Zeichenkette prüfen ``ist_formalie`` und die Abfrage (``name__iregex``)
FORMALIEN: Final = _ANFANG + "(?:" + "|".join(_FORMALIEN_TEILE) + ")"
_FORMALIEN_RE: Final = re.compile(FORMALIEN, re.IGNORECASE)


def ist_formalie(name: object) -> bool:
    """Ist der Tagesordnungspunkt eine Formalie (Eröffnung, Tagesordnung, Niederschrift …) ohne eigene Entscheidung?"""
    return bool(_FORMALIEN_RE.search(" ".join(str(name or "").split())))


# =============================================================================
# Gremiengruppen
# =============================================================================

GRUPPE_RAT: Final = "rat"
GRUPPE_BEZIRK: Final = "bezirk"
#: Schlüssel → Bezeichnung (lang, kurz für das Handy) im Umschalter; leer ist „Alle“
GRUPPEN: Final[dict[str, tuple[str, str]]] = {
    GRUPPE_RAT: ("Rat und Ausschüsse", "Rat, Ausschüsse"),
    GRUPPE_BEZIRK: ("Bezirksvertretungen", "Bezirke"),
}

_BEZIRK_RE: Final = re.compile(
    r"^(?:stadt)?bezirks(?:vertretung|beirat|rat|ausschuss|ausschuß)|^orts(?:beirat|rat|teilrat|ausschuss)"
    r"|^ortschaftsrat|^stadtteil(?:beirat|rat)|^bv\s",
    re.IGNORECASE,
)
_RAT_RE: Final = re.compile(
    r"^(?:der\s+)?(?:rat|stadtrat|gemeinderat|marktgemeinderat|kreistag|ratsversammlung|stadtverordnetenversammlung"
    r"|gemeindevertretung|bürgerschaft)(?:[^a-zäöüß]|$)|ausschuss|ausschuß|kommission",
    re.IGNORECASE,
)
_KEIN_RAT_RE: Final = re.compile(r"beirat|aufsichtsrat|verwaltungsrat|gesellschaft|gmbh|zweckverband|fraktion", re.I)


def gremiengruppe(name: object, classification: object = "") -> str:
    """Gruppe eines Gremiums für den Umschalter: ``GRUPPE_BEZIRK``, ``GRUPPE_RAT`` oder leer (nur unter „Alle“)."""
    texte = [" ".join(str(wert or "").split()) for wert in (name, classification)]
    if any(_BEZIRK_RE.search(text) for text in texte):
        return GRUPPE_BEZIRK
    if _KEIN_RAT_RE.search(texte[0]):
        return ""
    if any(_RAT_RE.search(text) for text in texte):
        return GRUPPE_RAT
    return ""


def _rang(name: str) -> int:
    """Rang eines Gremiums wie in der Gremienliste (``ranking.RANKING_RULES``): Rat vor Hauptausschuss vor Ausschüssen."""
    for muster, rang, _genau in RANKING_RULES:
        if re.search(muster, name, re.IGNORECASE):
            return rang
    return 300


@dataclass(frozen=True)
class Gremium:
    """Gremium der Kommune mit seiner Gruppe."""

    id: Any
    name: str
    gruppe: str


def gremien_der_kommune(body: OParlBody) -> dict[str, Gremium]:
    """Gremien der Kommune (ohne Fraktionen) nach Kennung, mit Gruppe. Eine Abfrage."""
    from .urheber_erkennung import ist_fraktion

    gremien: dict[str, Gremium] = {}
    for org_id, name, kurz, art, klasse in OParlOrganization.objects.filter(body=body, deleted=False).values_list(
        "id", "name", "short_name", "organization_type", "classification"
    ):
        anzeige = " ".join(str(name or kurz or "").split())
        if anzeige and not ist_fraktion(anzeige, art or "", klasse or ""):
            gremien[str(org_id)] = Gremium(org_id, anzeige, gremiengruppe(anzeige, klasse))
    return gremien


# =============================================================================
# Auswahl (Filter in der Adresse)
# =============================================================================

#: Zeiträume: Wert in der Adresse → Bezeichnung; leer ist die Voreinstellung (Ergebnisse brauchen nach der Sitzung oft
#: einige Tage, in einer Woche ohne Sitzung wäre die Seite sonst leer)
ZEITRAUM_WOCHE: Final = "woche"
ZEITRAUM_30_TAGE: Final = ""
ZEITRAUM_JAHR: Final = "jahr"
ZEITRAUM_WAHLPERIODE: Final = "wahlperiode"
ZEITRAEUME: Final[dict[str, str]] = {
    ZEITRAUM_WOCHE: "Diese Woche",
    ZEITRAUM_30_TAGE: "Letzte 30 Tage",
    ZEITRAUM_JAHR: "Letzte 12 Monate",
    ZEITRAUM_WAHLPERIODE: "Seit Beginn der Wahlperiode",
}
#: Nächstlängerer Zeitraum für den Leerzustand („Letzte 12 Monate zeigen“)
LAENGER: Final[dict[str, str]] = {
    ZEITRAUM_WOCHE: ZEITRAUM_30_TAGE,
    ZEITRAUM_30_TAGE: ZEITRAUM_JAHR,
    ZEITRAUM_JAHR: ZEITRAUM_WAHLPERIODE,
}

VON_VERWALTUNG: Final = "verwaltung"
VON_EINWOHNER: Final = "einwohner"
#: Vorlagenarten der Verwaltung bzw. aus der Einwohnerschaft (Filter „Eingebracht von“ und Absender rechts)
_VERWALTUNG_RE: Final = (
    r"(?:beschluss|informations|mitteilungs|verwaltungs|entscheidungs|empfehlungs|sitzungs|berichts)vorlage"
    r"|^vorlage|vorlage der verwaltung|stellungnahme|mitteilung|bericht|informationsbrief|beantwortung|antwort"
)
#: „Bürgerantrag“, „Einwohnerantrag“, „Anregungen und Beschwerden“, nicht „Mitteilung des Oberbürgermeisters“
_EINWOHNER_RE: Final = (
    r"bürger(?:innen)?(?:antrag|anregung|begehren|eingabe|anfrage)|einwohner|anregung|beschwerde|petition"
)

ERGEBNIS_SONSTIGE: Final = "sonstige"
#: Ergebnisse im Filter: das Vokabular ohne „angekündigt“, dazu alles, was sich nicht zuordnen lässt
ERGEBNISSE: Final[dict[str, str]] = {
    **{s: VOKABULAR[s][0] for s in (BESCHLOSSEN, ABGELEHNT, KENNTNIS, VERTAGT, VERWIESEN, ABGESETZT)},
    ERGEBNIS_SONSTIGE: "Sonstige, etwa zurückgezogen",
}

#: Parameter der Adresse in der Reihenfolge des Formulars
PARAMETER: Final = ("gruppe", "gremium", "von", "ergebnis", "zeitraum")


@dataclass(frozen=True)
class Auswahl:
    """Gewählte Filter aus der Adresse; Unbekanntes gilt als nicht gewählt (kein Serverfehler)."""

    gruppe: str = ""
    gremium: str = ""
    von: str = ""
    ergebnis: str = ""
    zeitraum: str = ZEITRAUM_30_TAGE

    @classmethod
    def aus_anfrage(cls, params: QueryDict | Mapping[str, str]) -> Auswahl:
        def wert(name: str) -> str:
            return " ".join(str(params.get(name) or "").split())

        gruppe = wert("gruppe")
        zeitraum = wert("zeitraum")
        ergebnis_wert = wert("ergebnis")
        return cls(
            gruppe=gruppe if gruppe in GRUPPEN else "",
            gremium=wert("gremium")[:64],
            von=wert("von")[:500],
            ergebnis=ergebnis_wert if ergebnis_wert in ERGEBNISSE else "",
            zeitraum=zeitraum if zeitraum in ZEITRAEUME else ZEITRAUM_30_TAGE,
        )

    @property
    def weitere_filter(self) -> int:
        """Zahl der gewählten Filter hinter „Weitere Filter“ (ohne Umschalter)."""
        return sum(bool(wert) for wert in (self.gremium, self.von, self.ergebnis, self.zeitraum))

    @property
    def gefiltert(self) -> bool:
        return bool(self.gruppe) or bool(self.weitere_filter)

    def adresse(self, **aenderungen: str) -> str:
        """Abfrageteil der Adresse mit gewählten Werten (leere entfallen), ``?…`` oder leer."""
        werte = {name: getattr(self, name) for name in PARAMETER} | aenderungen
        params = QueryDict(mutable=True)
        for name in PARAMETER:
            if werte[name]:
                params[name] = werte[name]
        query = params.urlencode()
        return f"?{query}" if query else ""


@dataclass(frozen=True)
class Zeitraum:
    """Zeitraum der Liste: Beginn (Sitzungsbeginn ab), Ende (bis jetzt) und Text für die Trefferzahl."""

    beginn: datetime
    ende: datetime
    text: str


_MONATE: Final = (
    "Januar",
    "Februar",
    "März",
    "April",
    "Mai",
    "Juni",
    "Juli",
    "August",
    "September",
    "Oktober",
    "November",
    "Dezember",
)


def _tag(datum: date, *, jahr: bool = False) -> str:
    text = f"{datum.day}. {_MONATE[datum.month - 1]}"
    return f"{text} {datum.year}" if jahr else text


def _spanne(von: date, bis: date) -> str:
    """„vom 2. bis 8. November“, „vom 8. Oktober bis 7. November“, über den Jahreswechsel mit Jahr."""
    if von.year != bis.year:
        return f"vom {_tag(von, jahr=True)} bis {_tag(bis, jahr=True)}"
    if von.month == bis.month:
        return f"vom {von.day}. bis {_tag(bis)}"
    return f"vom {_tag(von)} bis {_tag(bis)}"


def wahlperiode_beginn(body: OParlBody, heute: date | None = None) -> date | None:
    """Beginn der laufenden Wahlperiode der Kommune (``OParlLegislativeTerm``), ``None`` ohne Angabe. Eine Abfrage."""
    heute = heute or timezone.localdate()
    return (
        OParlLegislativeTerm.objects.filter(body=body, deleted=False, start_date__isnull=False, start_date__lte=heute)
        .order_by("-start_date")
        .values_list("start_date", flat=True)
        .first()
    )


def zeitraum(schluessel: str, *, wahlperiode: date | None, jetzt: datetime | None = None) -> Zeitraum:
    """Zeitraum zum Schlüssel; ohne bekannte Wahlperiode gilt für ``wahlperiode`` das letzte Jahr."""
    jetzt = jetzt or timezone.now()
    heute = timezone.localtime(jetzt).date()
    if schluessel == ZEITRAUM_WOCHE:
        montag = heute - timedelta(days=heute.weekday())
        von, text = montag, _spanne(montag, montag + timedelta(days=6))
    elif schluessel == ZEITRAUM_WAHLPERIODE and wahlperiode is not None:
        von, text = wahlperiode, f"seit {_tag(wahlperiode, jahr=True)}"
    elif schluessel in (ZEITRAUM_JAHR, ZEITRAUM_WAHLPERIODE):
        von = heute - timedelta(days=365)
        text = "in den letzten 12 Monaten"
    else:
        von = heute - timedelta(days=30)
        text = _spanne(von, heute)
    beginn = timezone.make_aware(datetime.combine(von, time.min), timezone.get_current_timezone())
    return Zeitraum(beginn=beginn, ende=jetzt, text=text)


# =============================================================================
# Abfrage
# =============================================================================

_SITZUNG_GREMIUM = OParlMeeting.organizations.through


def _sitzungen_der_gremien(gremium_ids: Iterable[Any]) -> QuerySet[Any]:
    return _SITZUNG_GREMIUM.objects.filter(oparlorganization_id__in=list(gremium_ids)).values("oparlmeeting_id")


def _beratung_mit(**bedingungen: Any) -> Exists:
    """Der Punkt hat eine Beratung eines Vorgangs mit diesen Bedingungen (``paper__…``)."""
    return Exists(OParlConsultation.objects.filter(agenda_item_external_id=OuterRef("external_id"), **bedingungen))


def punkte(
    body: OParlBody,
    auswahl: Auswahl,
    *,
    zeit: Zeitraum,
    gremien: Mapping[str, Gremium],
    fraktions_bezeichnungen: Iterable[str] = (),
) -> QuerySet[OParlAgendaItem]:
    """
    Tagesordnungspunkte mit Ergebnis für die Auswahl, ohne Formalien. ``fraktions_bezeichnungen``: gespeicherte
    Bezeichnungen der gewählten Fraktion (``vorgang_urheber.bezeichnungen_fuer``), nur für ``von`` mit einer Fraktion.
    """
    qs = (
        OParlAgendaItem.objects.filter(
            meeting__body=body,
            meeting__deleted=False,
            meeting__cancelled=False,
            meeting__start__gte=zeit.beginn,
            meeting__start__lte=zeit.ende,
            public=True,
            deleted=False,
        )
        .exclude(result__isnull=True)
        .exclude(result="")
        .exclude(name__iregex=FORMALIEN)
    )
    if auswahl.gruppe:
        qs = qs.filter(
            meeting_id__in=_sitzungen_der_gremien(g.id for g in gremien.values() if g.gruppe == auswahl.gruppe)
        )
    if auswahl.gremium:
        gremium = gremien.get(auswahl.gremium)
        qs = qs.filter(meeting_id__in=_sitzungen_der_gremien([gremium.id])) if gremium else qs.none()
    if auswahl.von == VON_VERWALTUNG:
        qs = qs.filter(_beratung_mit(paper__paper_type__iregex=_VERWALTUNG_RE))
    elif auswahl.von == VON_EINWOHNER:
        qs = qs.filter(_beratung_mit(paper__paper_type__iregex=_EINWOHNER_RE))
    elif auswahl.von:
        vorgaenge = VorgangUrheber.objects.filter(body_id=body.pk, bezeichnung__in=list(fraktions_bezeichnungen))
        qs = qs.filter(_beratung_mit(paper_id__in=vorgaenge.values("paper_id")))
    if auswahl.ergebnis:
        qs = qs.filter(result__in=_ergebnistexte(qs, auswahl.ergebnis))
    return qs


def _ergebnistexte(qs: QuerySet[OParlAgendaItem], schluessel: str) -> list[str]:
    """Ergebnistexte der Auswahl, die zum gewählten Ergebnis gehören (Zuordnung in Python, eine Abfrage)."""
    gesucht = NEUTRAL if schluessel == ERGEBNIS_SONSTIGE else schluessel
    texte = qs.order_by().values_list("result", flat=True).distinct()
    return [text for text in texte if text and einordnen(text) == gesucht]


def gremien_im_zeitraum(body: OParlBody, zeit: Zeitraum, gremien: Mapping[str, Gremium]) -> list[Gremium]:
    """Gremien mit einer Sitzung im Zeitraum für die Auswahlliste, wichtige zuerst. Eine Abfrage."""
    ids = set(
        _SITZUNG_GREMIUM.objects.filter(
            oparlmeeting__body=body,
            oparlmeeting__deleted=False,
            oparlmeeting__start__gte=zeit.beginn,
            oparlmeeting__start__lte=zeit.ende,
        )
        .values_list("oparlorganization_id", flat=True)
        .distinct()
    )
    auswahl = [g for g in gremien.values() if g.id in ids]
    return sorted(auswahl, key=lambda g: (_rang(g.name), g.name.casefold()))


# =============================================================================
# Seite: Sitzungen mit ihren Einträgen
# =============================================================================

SITZUNGEN_JE_SEITE: Final = 10
#: Ab so vielen gleich vertagten (bzw. abgesetzten) Punkten derselben Vorlagenart einer Sitzung steht eine Zeile
BUENDEL_AB: Final = 5
#: So viele Absender stehen rechts, danach „und weitere“
ABSENDER_HOECHSTENS: Final = 3

_ANTWORT_RE: Final = re.compile(r"stellungnahme|antwort|beantwortung", re.IGNORECASE)
_STAMMNUMMER_RE: Final = re.compile(r"^(.+?)\s*[-/]\s*\d{1,2}(?:\s*[a-z]{1,3})?$", re.IGNORECASE)


@dataclass
class Eintrag:
    """Eine Zeile der Liste: ein Tagesordnungspunkt oder ein Bündel gleich vertagter Punkte (``teile``)."""

    punkt_id: Any
    sitzung_id: Any
    titel: str
    ergebnis: Ergebnis | None
    vorgang_id: Any = None
    art: str = ""
    #: Eine Zeile „Worum es geht“, sobald es eine geprüfte Kurzfassung gibt (Issue #1271); bis dahin leer
    worum: str = ""
    #: Sätze unter dem Titel („Mit Stellungnahme der Verwaltung.“)
    dazu: list[str] = field(default_factory=list)
    #: Absender rechts: Fraktionen bzw. „Verwaltung“, „Bürgerantrag“, höchstens ``ABSENDER_HOECHSTENS``
    von: list[str] = field(default_factory=list)
    #: Weitere Absender über ``von`` hinaus („und weitere“)
    von_weitere: int = 0
    teile: list[Eintrag] = field(default_factory=list)
    # Für das Bündeln der Stellungnahmen
    _antwort: str = ""
    _vorgang_ext: str = ""
    _nummer: str = ""
    _uebergeordnet: tuple[str, ...] = ()

    @property
    def ziel(self) -> str:
        """Ziel des Titels: der Vorgang, sonst (und für Bündel) der Punkt auf der Sitzungsseite."""
        if self.vorgang_id and not self.teile:
            return reverse("insight_core:insight:paper_detail", args=[self.vorgang_id])
        return reverse("insight_core:insight:meeting_detail", args=[self.sitzung_id]) + f"#top-{self.punkt_id}"


@dataclass
class Sitzung:
    """Eine Sitzung mit ihren Einträgen."""

    id: Any
    gremium: str
    beginn: datetime | None
    eintraege: list[Eintrag]


def seite(
    qs: QuerySet[OParlAgendaItem], nummer: int, gremien: Mapping[str, Gremium]
) -> tuple[Page[Any], list[Sitzung]]:
    """
    Seite ``nummer`` der Sitzungen mit Punkten aus ``qs``, neueste zuerst, und die Sitzungen mit ihren Einträgen.
    Ungültige oder zu große Seitenzahlen ergeben die nächstgelegene Seite.
    """
    sitzungen_qs = (
        OParlMeeting.objects.filter(pk__in=qs.values("meeting_id")).order_by("-start", "pk").only("id", "start", "name")
    )
    blatt = Paginator(sitzungen_qs, SITZUNGEN_JE_SEITE).get_page(nummer)
    sitzungen = list(blatt.object_list)
    if not sitzungen:
        return blatt, []
    ids = [s.pk for s in sitzungen]
    alle_punkte = list(
        qs.filter(meeting_id__in=ids).only("id", "external_id", "meeting_id", "number", "order", "name", "result")
    )
    vorgaenge = _vorgaenge_der_punkte(alle_punkte)
    urheber = _urheber({v["paper_id"] for liste in vorgaenge.values() for v in liste})
    namen = _gremiennamen(ids, gremien)

    from hub.ris.selectors import agenda_number_key

    je_sitzung: dict[Any, list[OParlAgendaItem]] = defaultdict(list)
    for punkt in alle_punkte:
        je_sitzung[punkt.meeting_id].append(punkt)
    ergebnis_liste = []
    for sitzung in sitzungen:
        reihe = sorted(je_sitzung[sitzung.pk], key=lambda p: (agenda_number_key(p.number), p.order or 0))
        eintraege = [_eintrag(p, vorgaenge.get(p.external_id, []), urheber) for p in reihe]
        eintraege = _vertagungen_buendeln(_stellungnahmen_buendeln(eintraege))
        ergebnis_liste.append(
            Sitzung(
                id=sitzung.pk,
                gremium=namen.get(sitzung.pk) or sitzung.name or "Sitzung",
                beginn=sitzung.start,
                eintraege=eintraege,
            )
        )
    return blatt, ergebnis_liste


def _gremiennamen(sitzung_ids: list[Any], gremien: Mapping[str, Gremium]) -> dict[Any, str]:
    """Gremien je Sitzung als Überschrift („Rat“, „Ausschuss A und Ausschuss B“). Eine Abfrage."""
    je_sitzung: dict[Any, list[str]] = defaultdict(list)
    for sitzung_id, gremium_id in _SITZUNG_GREMIUM.objects.filter(oparlmeeting_id__in=sitzung_ids).values_list(
        "oparlmeeting_id", "oparlorganization_id"
    ):
        gremium = gremien.get(str(gremium_id))
        if gremium is not None:
            je_sitzung[sitzung_id].append(gremium.name)
    return {sid: " und ".join(sorted(set(namen), key=lambda n: (_rang(n), n))) for sid, namen in je_sitzung.items()}


def _vorgaenge_der_punkte(alle_punkte: list[OParlAgendaItem]) -> dict[str, list[dict[str, Any]]]:
    """Beratene Vorgänge je Punkt (Kennung der Quelle), wie die Sitzungsseite (``papers_of_agenda_items``)."""
    from hub.ris.selectors import consultation_agenda_item_in_source

    kennungen = [p.external_id for p in alle_punkte if p.external_id]
    if not kennungen:
        return {}
    zeilen = (
        OParlConsultation.objects.filter(agenda_item_external_id__in=kennungen, paper__isnull=False)
        .filter(consultation_agenda_item_in_source())
        .exclude(withdrawn_q())
        .exclude(withdrawn_q("paper"))
        .order_by("paper__reference", "paper__name", "paper_id")
        .values(
            "agenda_item_external_id",
            "paper_id",
            "paper__name",
            "paper__reference",
            "paper__paper_type",
            "paper__external_id",
            "paper__raw_json__superordinatedPaper",
        )
    )
    je_punkt: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for zeile in zeilen:
        liste = je_punkt[zeile["agenda_item_external_id"] or ""]
        if all(v["paper_id"] != zeile["paper_id"] for v in liste):
            liste.append(dict(zeile))
    return je_punkt


def _urheber(vorgang_ids: set[Any]) -> dict[Any, list[str]]:
    """Antragstellende Fraktionen je Vorgang als Kurzform. Eine Abfrage (keine ohne Vorgänge)."""
    je_vorgang: dict[Any, list[str]] = defaultdict(list)
    if not vorgang_ids:
        return je_vorgang
    for paper_id, bezeichnung, kurzform in (
        VorgangUrheber.objects.filter(paper_id__in=list(vorgang_ids))
        .order_by("bezeichnung")
        .values_list("paper_id", "bezeichnung", "kurzform")
    ):
        name = kurzform or bezeichnung
        if name not in je_vorgang[paper_id]:
            je_vorgang[paper_id].append(name)
    return je_vorgang


def _antwortart(vorgang: Mapping[str, Any]) -> str:
    """„Stellungnahme“ bzw. „Antwort“, wenn der Vorgang eine Stellungnahme oder Antwort der Verwaltung ist, sonst leer."""
    text = f"{vorgang.get('paper__paper_type') or ''} {vorgang.get('paper__name') or ''}"
    treffer = _ANTWORT_RE.search(text)
    if treffer is None:
        return ""
    return "Stellungnahme" if treffer.group(0).lower() == "stellungnahme" else "Antwort"


def _absender(vorgang: Mapping[str, Any] | None, urheber: Mapping[Any, list[str]]) -> list[str]:
    """Antragstellende Fraktionen, sonst „Bürgerantrag“ bzw. „Verwaltung“ nach der Vorlagenart, sonst nichts."""
    if vorgang is None:
        return []
    fraktionen = urheber.get(vorgang["paper_id"], [])
    if fraktionen:
        return list(fraktionen)
    art = str(vorgang.get("paper__paper_type") or "")
    if art and re.search(_EINWOHNER_RE, art, re.IGNORECASE):
        return ["Bürgerantrag"]
    if art and re.search(_VERWALTUNG_RE, art, re.IGNORECASE):
        return ["Verwaltung"]
    return []


def _uebergeordnet(wert: Any) -> tuple[str, ...]:
    """Kennungen aus ``superordinatedPaper`` (Adresse, eingebettetes Objekt oder Liste davon)."""
    werte = wert if isinstance(wert, list) else [wert]
    kennungen = []
    for eintrag in werte:
        kennung = eintrag.get("id") if isinstance(eintrag, dict) else eintrag
        if isinstance(kennung, str) and kennung:
            kennungen.append(kennung)
    return tuple(kennungen)


def _satz(art: str, anzahl: int) -> str:
    if art == "Stellungnahme":
        return "Mit Stellungnahme der Verwaltung." if anzahl == 1 else f"Mit {anzahl} Stellungnahmen der Verwaltung."
    return "Mit Antwort der Verwaltung." if anzahl == 1 else f"Mit {anzahl} Antworten der Verwaltung."


def _eintrag(punkt: OParlAgendaItem, vorgaenge: list[dict[str, Any]], urheber: Mapping[Any, list[str]]) -> Eintrag:
    """Zeile für einen Punkt: Hauptvorgang ist der erste, der keine Stellungnahme oder Antwort ist."""
    from .ortsschutz import oeffentlicher_titel

    haupt = next((v for v in vorgaenge if not _antwortart(v)), vorgaenge[0] if vorgaenge else None)
    name = (haupt or {}).get("paper__name") or punkt.name or "Tagesordnungspunkt"
    # Ohne Hausnummer bei Eingaben aus der Einwohnerschaft und Bauanträgen (Issue #1269)
    titel = oeffentlicher_titel(" ".join(str(name).split()), (haupt or {}).get("paper__paper_type"))
    eintrag = Eintrag(
        punkt_id=punkt.pk,
        sitzung_id=punkt.meeting_id,
        titel=titel,
        ergebnis=ergebnis(punkt.result),
        vorgang_id=haupt["paper_id"] if haupt else None,
        art=str((haupt or {}).get("paper__paper_type") or ""),
    )
    _absender_setzen(eintrag, _absender(haupt, urheber))
    if haupt is not None:
        eintrag._antwort = _antwortart(haupt)
        eintrag._vorgang_ext = str(haupt.get("paper__external_id") or "")
        eintrag._nummer = " ".join(str(haupt.get("paper__reference") or "").split())
        eintrag._uebergeordnet = _uebergeordnet(haupt.get("paper__raw_json__superordinatedPaper"))
    # Stellungnahmen am selben Punkt
    zaehler: dict[str, int] = defaultdict(int)
    for vorgang in vorgaenge:
        if vorgang is not haupt and (art := _antwortart(vorgang)):
            zaehler[art] += 1
    eintrag.dazu.extend(_satz(art, anzahl) for art, anzahl in sorted(zaehler.items()))
    return eintrag


def _absender_setzen(eintrag: Eintrag, namen: list[str]) -> None:
    eintrag.von = namen[:ABSENDER_HOECHSTENS]
    eintrag.von_weitere = max(len(namen) - ABSENDER_HOECHSTENS, 0)


def _stammnummer(nummer: str) -> str:
    treffer = _STAMMNUMMER_RE.match(nummer)
    return treffer.group(1) if treffer else ""


def _stellungnahmen_buendeln(eintraege: list[Eintrag]) -> list[Eintrag]:
    """Stellungnahmen und Antworten, deren Antrag in derselben Sitzung steht, werden ein Satz unter dem Antrag."""
    nach_kennung = {e._vorgang_ext: e for e in eintraege if e._vorgang_ext and not e._antwort}
    nach_nummer = {e._nummer: e for e in eintraege if e._nummer and not e._antwort}
    bleiben: list[Eintrag] = []
    angehaengt: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    ziele: dict[int, Eintrag] = {}
    for eintrag in eintraege:
        ziel = None
        if eintrag._antwort:
            ziel = next((nach_kennung[k] for k in eintrag._uebergeordnet if k in nach_kennung), None)
            stamm = _stammnummer(eintrag._nummer)
            if ziel is None and stamm:
                ziel = nach_nummer.get(stamm)
        if ziel is None or ziel is eintrag:
            bleiben.append(eintrag)
            continue
        angehaengt[id(ziel)][eintrag._antwort] += 1
        ziele[id(ziel)] = ziel
    for schluessel, arten in angehaengt.items():
        ziel = ziele[schluessel]
        vorhanden = set(ziel.dazu)
        for art, anzahl in sorted(arten.items()):
            satz = _satz(art, anzahl)
            if satz not in vorhanden:
                ziel.dazu.append(satz)
    return bleiben


def _vertagungen_buendeln(eintraege: list[Eintrag]) -> list[Eintrag]:
    """Ab ``BUENDEL_AB`` gleich vertagten bzw. abgesetzten Punkten derselben Vorlagenart eine Zeile mit Aufklapper."""
    gruppen: dict[tuple[str, str], list[Eintrag]] = defaultdict(list)
    for eintrag in eintraege:
        schluessel = eintrag.ergebnis.schluessel if eintrag.ergebnis else ""
        if schluessel in (VERTAGT, ABGESETZT):
            gruppen[(schluessel, eintrag.art)].append(eintrag)
    buendel = {id(g[0]): g for g in gruppen.values() if len(g) >= BUENDEL_AB}
    if not buendel:
        return eintraege
    gebuendelt = {id(e) for g in buendel.values() for e in g}
    ergebnis_liste: list[Eintrag] = []
    for eintrag in eintraege:
        gruppe = buendel.get(id(eintrag))
        if gruppe is not None:
            ergebnis_liste.append(_buendel(gruppe, alle=len(gruppe) == len(eintraege)))
        elif id(eintrag) not in gebuendelt:
            ergebnis_liste.append(eintrag)
    return ergebnis_liste


def _buendel(gruppe: list[Eintrag], *, alle: bool) -> Eintrag:
    erster = gruppe[0]
    schluessel = erster.ergebnis.schluessel if erster.ergebnis else VERTAGT
    begriff, erklaerung = VOKABULAR[schluessel]
    von: list[str] = []
    for eintrag in gruppe:
        for name in eintrag.von:
            if name not in von:
                von.append(name)
    buendel = Eintrag(
        punkt_id=erster.punkt_id,
        sitzung_id=erster.sitzung_id,
        titel=f"{'Alle ' if alle else ''}{len(gruppe)} Punkte {begriff}",
        ergebnis=Ergebnis(schluessel, begriff, erklaerung),
        art=erster.art,
        dazu=[f"Vorlagenart: {erster.art}."] if erster.art else [],
        teile=gruppe,
    )
    _absender_setzen(buendel, von)
    return buendel
