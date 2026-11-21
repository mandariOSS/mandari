# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Vorlagen mit kritischem Personenbezug (Issue #1560, Datensparsamkeit): kein Suchindex.

Manche Vorgänge nennen Menschen, die kein Amt haben: Eingaben aus der Einwohnerschaft, Bauanträge, Einwendungen und
Stellungnahmen aus der Öffentlichkeit, Personalangelegenheiten, Vorschlagslisten für Schöffinnen und Schöffen,
Stundungen, Widersprüche, Grundstücksgeschäfte. Oder ihre Dokumente enthalten Angaben wie Geburtsdatum, Wohnanschrift,
Bankverbindung oder eine private E-Mail-Adresse. Für diese Vorgänge gilt:

* Die Seite des Vorgangs trägt ``noindex``, die Sitemap nennt sie nicht, ihre Dokumente liefert mandari mit
  ``X-Robots-Tag: noindex`` aus.
* Der Volltext ihrer Dokumente steht nicht im Suchindex: weder am Vorgang (Vorschau) noch an der Datei. Über Titel und
  Vorlagennummer bleibt der Vorgang auffindbar.

Zwei Wege der Erkennung, beide ohne gespeicherte Kennzeichnung (sie gelten sofort für den ganzen Bestand):

* ``grund_nach_art``: nach Art und Titel, ohne Datenbankzugriff. Baut auf dem Schutz der Privatanschriften auf
  (``ortsschutz.grund_nach_art``, Issue #1269).
* ``im_text``: Muster im erkannten Text eines Dokuments. Das Muster ist so geschrieben, dass PostgreSQL (``~*``) und
  Python es gleich lesen (keine Wortgrenzen, keine Rückbezüge): ``grund`` prüft die Dokumente eines Vorgangs in der
  Datenbank, ohne die Texte zu laden, und merkt sich das Ergebnis.

Lieber eine Vorlage zu viel ohne Volltext als eine zu wenig: Die Vorlage selbst bleibt lesbar, es geht nur um die
Auffindbarkeit über Suchmaschinen und die Volltextsuche.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Final

from django.core.cache import cache
from django.db.models import Q

from . import ortsschutz

if TYPE_CHECKING:
    from insight_core.models import OParlPaper

PERSONAL: Final = "personal"
EINWENDUNGEN: Final = "einwendungen"
EINZELFALL: Final = "einzelfall"
TEXT: Final = "text"

#: Personalangelegenheiten und Vorschlagslisten mit Privatpersonen (Schöffinnen und Schöffen)
_PERSONAL: Final = re.compile(
    r"personalangelegenheit|personalsache|stellenbesetzung|höhergruppierung|beförderung\s+(?:von|der|des)|"
    r"einstellung\s+(?:einer|eines|von)\s|abmahnung|disziplinar|dienstaufsichtsbeschwerde|"
    r"versetzung\s+in\s+den\s+ruhestand|schöff|vorschlagsliste",
    re.IGNORECASE,
)
#: Einwendungen und Stellungnahmen aus der Öffentlichkeit (Bauleitplanung, Planfeststellung)
_EINWENDUNGEN: Final = re.compile(
    r"einwendung|einwender|abwägung|stellungnahmen?\s+(?:aus\s+)?der\s+öffentlichkeit|"
    r"anregungen\s+(?:aus\s+)?der\s+öffentlichkeit",
    re.IGNORECASE,
)
#: Einzelfälle mit Privatpersonen: Forderungen, Widersprüche, Grundstücksgeschäfte, Hilfen
_EINZELFALL: Final = re.compile(
    r"stundung|niederschlagung|erlass\s+(?:von|einer)\s+forderung|widerspruch\s+(?:gegen|des|der|von)\s|"
    r"vorkaufsrecht|grundstücks\s*(?:-\s*)?(?:angelegenheit|verkauf|ankauf|kauf|tausch)|erbbaurecht|"
    r"einbürgerung|härtefall|zwangsvollstreckung|zwangsversteigerung",
    re.IGNORECASE,
)

#: Angaben zu Privatpersonen im Text eines Dokuments. Für PostgreSQL und Python gleich: nur Zeichenklassen,
#: Wiederholungen und Alternativen
TEXT_MUSTER: Final = (
    r"geb(\.|oren)[[:space:]]+am[[:space:]]+[0-9]|geburtsdatum|geburtsname|wohnhaft|"
    r"iban[[:space:]]*:?[[:space:]]*[a-z]{2}[0-9]{2}|"
    r"steuer-?id|steueridentifikationsnummer|sozialversicherungsnummer|personalausweisnummer|"
    r"unterschriftenliste|"
    r"@(gmx|web|gmail|googlemail|t-online|yahoo|hotmail|outlook|freenet|arcor|aol|icloud|posteo|mailbox)\.[a-z]"
)
#: Dasselbe Muster für Python (``[[:space:]]`` ist die POSIX-Schreibweise von PostgreSQL)
_TEXT: Final = re.compile(TEXT_MUSTER.replace("[[:space:]]", r"\s"), re.IGNORECASE)

#: So lange gilt das Ergebnis der Prüfung der Dokumente eines Vorgangs
_MERKEN_SEKUNDEN: Final = 6 * 3600


def grund_nach_art(paper_type: str | None, name: str | None) -> str:
    """Grund des Personenbezugs nach Art und Titel, sonst leer. Ohne Datenbankzugriff, für Listen und den Index."""
    eigener = ortsschutz.grund_nach_art(paper_type, name)
    if eigener:
        return eigener
    worte = f"{paper_type or ''} {name or ''}"
    if _PERSONAL.search(worte):
        return PERSONAL
    if _EINWENDUNGEN.search(worte):
        return EINWENDUNGEN
    if _EINZELFALL.search(worte):
        return EINZELFALL
    return ""


def nach_art_q() -> Q:
    """
    ``grund_nach_art`` als Bedingung für Querysets von Vorgängen (Sitemap): dieselben Muster auf ``paper_type`` und
    ``name``. PostgreSQL schreibt die Wortgrenze ``\\y`` statt ``\\b``; sonst lesen beide die Muster gleich.
    """
    from django.db import connection

    def muster(regel: re.Pattern[str]) -> str:
        return regel.pattern.replace("\\b", "\\y") if connection.vendor == "postgresql" else regel.pattern

    bedingung = Q()
    for regel in (ortsschutz._EINWOHNER_ART, ortsschutz._BAU_ART, _PERSONAL, _EINWENDUNGEN, _EINZELFALL):
        bedingung |= Q(paper_type__iregex=muster(regel))
    for regel in (ortsschutz._EINWOHNER_TITEL, ortsschutz._BAU_TITEL, _PERSONAL, _EINWENDUNGEN, _EINZELFALL):
        bedingung |= Q(name__iregex=muster(regel))
    return bedingung


def im_text(text: str | None) -> bool:
    """Nennt der Text Angaben zu Privatpersonen (``TEXT_MUSTER``)?"""
    return bool(text) and _TEXT.search(text or "") is not None


def _dokumente_mit_angaben(paper: OParlPaper) -> bool:
    """Enthält ein Dokument des Vorgangs Angaben zu Privatpersonen? Eine Abfrage in der Datenbank, gemerkt."""
    schluessel = f"personenbezug:{paper.pk}:{getattr(paper, 'updated_at', '')}"
    gemerkt = cache.get(schluessel)
    if gemerkt is None:
        from django.db import connection

        dateien = paper.files.filter(deleted=False, text_content__isnull=False)
        if connection.vendor == "postgresql":
            gemerkt = dateien.filter(text_content__iregex=TEXT_MUSTER).exists()
        else:
            # Andere Datenbanken (Tests mit SQLite) kennen die POSIX-Klassen nicht
            gemerkt = any(im_text(text) for text in dateien.values_list("text_content", flat=True).iterator())
        cache.set(schluessel, gemerkt, _MERKEN_SEKUNDEN)
    return bool(gemerkt)


def grund(paper: OParlPaper) -> str:
    """
    Grund des Personenbezugs eines Vorgangs, sonst leer: nach Art und Titel, nach federführendem Amt und verbundenen
    Vorgängen (``ortsschutz.grund``; hat die Seite ihn schon ermittelt, steht er als ``_ortsschutz`` am Objekt),
    zuletzt nach den Dokumenten. Für die Seite des Vorgangs und seine Dateien.
    """
    eigener = grund_nach_art(paper.paper_type, paper.name)
    if eigener:
        return eigener
    orte = getattr(paper, "_ortsschutz", None)
    if orte is None:
        orte = ortsschutz.grund(paper)
    return orte or (TEXT if _dokumente_mit_angaben(paper) else "")


def datei_ohne_index(datei: Any) -> bool:
    """
    Soll eine ausgelieferte Datei ``X-Robots-Tag: noindex`` tragen? Bei Dateien eines Vorgangs entscheidet der
    Vorgang (``grund``), sonst (Dateien einer Sitzung) ihr eigener Text; eine Abfrage, gemerkt.
    """
    from insight_core.models import OParlFile

    if getattr(datei, "paper_id", None):
        return bool(grund(datei.paper))
    schluessel = f"personenbezug:datei:{datei.pk}:{getattr(datei, 'updated_at', '')}"
    gemerkt = cache.get(schluessel)
    if gemerkt is None:
        from django.db import connection

        eigene = OParlFile.objects.filter(pk=datei.pk, text_content__isnull=False)
        if connection.vendor == "postgresql":
            gemerkt = eigene.filter(text_content__iregex=TEXT_MUSTER).exists()
        else:
            gemerkt = any(im_text(text) for text in eigene.values_list("text_content", flat=True))
        cache.set(schluessel, gemerkt, _MERKEN_SEKUNDEN)
    return bool(gemerkt)


def ohne_volltext(paper: Any, texte: Any = ()) -> bool:
    """
    Gehört der Volltext dieses Vorgangs nicht in den Suchindex? Nach Art und Titel, sonst nach den übergebenen
    Texten seiner Dokumente (im Index der Anfang jedes Dokuments).
    """
    if paper is not None and grund_nach_art(getattr(paper, "paper_type", None), getattr(paper, "name", None)):
        return True
    return any(im_text(text) for text in texte)
