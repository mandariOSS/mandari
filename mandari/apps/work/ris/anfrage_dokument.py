# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Antwortentwurf als Dokument übernehmen (Issue #1488, Schritt 2).

Aus einem Entwurf von „Anfrage beantworten“ (``apps.work.ris.anfragen``) entsteht ein Dokument in Work. Ab da gilt für
die Antwort, was für jedes Dokument gilt: Freigabe, Versionen, Briefkopf, Export als Word und PDF. Die Anfrage wird so
zu einem Vorgang der Fraktion, ohne eine zweite Vorgangsverwaltung.

* **Der Entwurf bleibt bis dahin ungespeichert.** Die Seite trägt ihn als signiertes Paket im Formular mit
  (``siegel``): Brieftext mit Fußnoten, Quellenverzeichnis, offene Punkte, Prüfhinweise und die Anfrage. Das Paket ist
  an Organisation und Mitgliedschaft gebunden und einen Tag gültig. Beim Übernehmen zählt nur ein Paket mit gültiger
  Signatur: Im Dokument steht genau der Entwurf, den Work geprüft hat, mit genau den Fundstellen, die die Werkzeuge
  geliefert haben.
* **Aufbau des Dokuments.** Anrede, Brieftext mit Fußnoten als ``[1]``, Grußformel und Absender, darunter „Quellen“
  mit denselben Nummern und den Adressen der öffentlichen Seiten im Bürgerportal. Die Adresse steht ausgeschrieben
  da, weil der Export als Word Linkziele nicht mitnimmt. Die Anfrage im Wortlaut folgt nach einem Seitenumbruch: Sie
  gehört zum Vorgang, liegt mit dem Inhalt verschlüsselt ab und lässt sich vor dem Versand als ganze Seite entfernen.
* **Was der Mensch noch tun muss, steht in der Checkliste des Dokuments:** der Abgleich mit den Quellen, jeder
  Absatz mit Prüfhinweis, jeder Platzhalter und jeder offene Punkt.
* **Dokumenttyp „Antwortschreiben“.** Freigabe ja, Einreichung bei der Verwaltung nein. Er entsteht beim ersten
  Übernehmen; die Organisation kann ihn umbenennen oder abschalten (dann bleibt das Dokument ohne eigenen Typ).

Es entsteht kein KI-Aufruf und kein Versand. Das Dokument ist privat, bis die Person es teilt.
"""

from __future__ import annotations

import re
import uuid
from datetime import date
from typing import Any, Final

from django.core import signing
from django.core.cache import cache
from django.db import transaction
from django.utils.html import escape

from apps.work.sanitize import sanitize_editor_html

from . import anfragen

#: Aufbau des Pakets; ein Paket mit anderer Nummer gilt als ungültig (Formular aus einer älteren Version)
VERSION: Final = 1
#: So lange lässt sich ein angezeigter Entwurf übernehmen (Sekunden)
MAX_ALTER: Final = 24 * 60 * 60
MAX_PRUEFPUNKTE: Final = 20
ABSATZ_ANFANG: Final = 60

TYP_SLUG: Final = "antwortschreiben"
TYP_NAME: Final = "Antwortschreiben"
TYP_BESCHREIBUNG: Final = "Antwort auf eine Anfrage aus Bürgerschaft oder Presse"

UNGUELTIG: Final = (
    "Der Entwurf ließ sich nicht übernehmen: Er ist älter als einen Tag oder wurde nicht in dieser Sitzung erstellt. "
    "Bitte erstellen Sie ihn neu."
)
ANGELEGT: Final = (
    "Dokument angelegt, sichtbar nur für Sie. Was noch zu prüfen und zu ergänzen ist, steht in der Checkliste."
)
SCHON_ANGELEGT: Final = "Aus diesem Entwurf ist bereits ein Dokument entstanden."
GEGENPRUEFEN: Final = "KI-Entwurf: Aussagen gegen die Quellen prüfen"

LINK_KLASSEN: Final = "text-primary-600 underline hover:text-primary-700"
SEITENUMBRUCH: Final = '<div data-page-break="true" class="page-break"></div>'

#: Platzhalter des Entwurfs („[Position der Fraktion ergänzen]“); Fußnoten wie „[12]“ sind keine
_PLATZHALTER: Final = re.compile(r"\[(?!\d+\])([^\[\]\n]{3,120})\]")


def _salz(organization: Any, membership: Any) -> str:
    return f"work.ris.anfrage-dokument.{organization.pk}.{membership.pk}"


def siegel(entwurf: anfragen.Entwurf, organization: Any, membership: Any) -> str:
    """Entwurf als signiertes Paket für das Formular „Als Dokument übernehmen“; leer, wenn es keinen Entwurf gibt."""
    if entwurf.hinweis or not entwurf.absaetze:
        return ""
    paket = {
        "v": VERSION,
        "id": uuid.uuid4().hex,
        "anfrage": entwurf.anfrage,
        "art": entwurf.art,
        "anrede": entwurf.anrede,
        "betreff": entwurf.betreff,
        "absaetze": [[absatz.text_belegt, absatz.liste, list(absatz.hinweise)] for absatz in entwurf.absaetze],
        "fundstellen": [[eintrag.nummer, eintrag.titel, eintrag.oeffentlich] for eintrag in entwurf.fundstellen],
        "offen": list(entwurf.offen),
        "absender": list(entwurf.absender),
    }
    return signing.dumps(paket, salt=_salz(organization, membership), compress=True)


def oeffnen(post: Any, organization: Any, membership: Any) -> dict[str, Any] | None:
    """Paket aus dem Formular; ``None``, wenn Signatur, Alter, Mitgliedschaft oder Aufbau nicht stimmen."""
    wert = str(post.get("siegel", ""))
    if not wert:
        return None
    try:
        paket = signing.loads(wert, salt=_salz(organization, membership), max_age=MAX_ALTER)
    except signing.BadSignature:  # auch abgelaufen (SignatureExpired)
        return None
    if not isinstance(paket, dict) or paket.get("v") != VERSION or not paket.get("absaetze"):
        return None
    return paket


def titel(paket: dict[str, Any], heute: date) -> str:
    """Titel des Dokuments: der geprüfte Betreff, sonst Art der Anfrage und Tag."""
    betreff = str(paket.get("betreff") or "").strip()
    if betreff:
        return f"Antwort: {betreff}"[:500]
    art = anfragen.ARTEN.get(str(paket.get("art")), anfragen.ARTEN[anfragen.STANDARD_ART])
    return f"Antwort auf eine {art.label} vom {heute.strftime('%d.%m.%Y')}"


def _auszeichnen(text: str) -> str:
    """Text maskiert; Platzhalter hervorgehoben, damit sie im Editor und im Export auffallen."""
    return _PLATZHALTER.sub(r"<mark>[\1]</mark>", escape(text))


def _zeilen(zeilen: list[str]) -> str:
    return "<br>".join(escape(zeile) for zeile in zeilen if zeile)


def dokument_html(paket: dict[str, Any]) -> str:
    """Inhalt des Dokuments im Format des Editors: Brief, Quellenverzeichnis und die Anfrage auf eigener Seite."""
    teile = [f"<p>{escape(str(paket.get('anrede') or anfragen.STANDARD_ANREDE))}</p>"]
    liste: list[str] = []

    def liste_schliessen() -> None:
        if liste:
            teile.append(f"<ul>{''.join(liste)}</ul>")
            liste.clear()

    for text, ist_liste, _hinweise in paket["absaetze"]:
        if ist_liste:
            liste.append(f"<li><p>{_auszeichnen(str(text))}</p></li>")
        else:
            liste_schliessen()
            teile.append(f"<p>{_auszeichnen(str(text))}</p>")
    liste_schliessen()
    teile.append(f"<p>{_zeilen([anfragen.GRUSS, *map(str, paket.get('absender') or [])])}</p>")

    fundstellen = paket.get("fundstellen") or []
    if fundstellen:
        teile.append("<h3>Quellen</h3>")
        for nummer, quelle, adresse in fundstellen:
            eintrag = f"[{int(nummer)}] {escape(str(quelle))}"
            if adresse:
                ziel = escape(str(adresse))
                eintrag += (
                    f'<br><a href="{ziel}" target="_blank" rel="noopener noreferrer" class="{LINK_KLASSEN}">{ziel}</a>'
                )
            teile.append(f"<p>{eintrag}</p>")

    anfrage = str(paket.get("anfrage") or "").strip()
    if anfrage:
        absaetze = [absatz.split("\n") for absatz in anfrage.split("\n\n") if absatz.strip()]
        zitat = "".join(f"<p>{_zeilen(absatz)}</p>" for absatz in absaetze)
        teile.append(f"{SEITENUMBRUCH}<h3>Anfrage im Wortlaut</h3><blockquote>{zitat}</blockquote>")
    return sanitize_editor_html("".join(teile))


def _anfang(text: str) -> str:
    """Anfang eines Absatzes, an einer Wortgrenze gekürzt (nicht mitten in einer Zahl oder einem Datum)."""
    if len(text) <= ABSATZ_ANFANG:
        return text
    return text[:ABSATZ_ANFANG].rsplit(" ", 1)[0].rstrip(" ,;:") + " …"


def pruefpunkte(paket: dict[str, Any]) -> list[str]:
    """Checkliste des Dokuments: Abgleich mit den Quellen, Prüfhinweise je Absatz, Platzhalter, offene Punkte."""
    punkte = [GEGENPRUEFEN]
    platzhalter: list[str] = []
    for text, _ist_liste, hinweise in paket["absaetze"]:
        punkte.extend(f"Prüfen: {grund} Absatz „{_anfang(str(text))}“" for grund in hinweise)
        platzhalter.extend(treffer.group(0) for treffer in _PLATZHALTER.finditer(str(text)))
    punkte.extend(f"Ergänzen: {stelle}" for stelle in dict.fromkeys(platzhalter))
    punkte.extend(f"Offen: {punkt}" for punkt in paket.get("offen") or [])
    return [punkt[:300] for punkt in punkte][:MAX_PRUEFPUNKTE]


def _dokumenttyp(organization: Any) -> Any:
    """Typ „Antwortschreiben“ der Organisation; entsteht beim ersten Mal, ``None``, wenn sie ihn abgeschaltet hat."""
    from apps.work.motions.models import MotionType

    typ, _neu = MotionType.objects.get_or_create(
        organization=organization,
        slug=TYP_SLUG,
        defaults={
            "name": TYP_NAME,
            "description": TYP_BESCHREIBUNG,
            "icon": "message-square",
            "requires_approval": True,
            # Ein Brief an Bürger:innen oder Presse geht nicht an die Verwaltung
            "is_submittable": False,
            "sort_order": 90,
        },
    )
    return typ if typ.is_active else None


def _schluessel(paket: dict[str, Any]) -> str:
    return f"work:anfrage-dokument:{paket.get('id')}"


def anlegen(organization: Any, membership: Any, paket: dict[str, Any], *, heute: date) -> tuple[Any, bool]:
    """
    Dokument aus dem Paket anlegen: (Dokument, neu angelegt).

    Ein zweites Absenden desselben Entwurfs (Doppelklick, Zurück-Taste) legt nichts an und liefert das Dokument des
    ersten; läuft das erste noch, ``(None, False)``.
    """
    from apps.work.motions.models import Motion, MotionChecklistItem, OrganizationLetterhead

    schluessel = _schluessel(paket)
    if not cache.add(schluessel, "", MAX_ALTER):
        erstes = cache.get(schluessel)
        if not erstes:
            return None, False
        vorhanden = (
            Motion.objects.filter(pk=erstes, organization=organization, author=membership)
            .exclude(status="deleted")
            .first()
        )
        if vorhanden is not None:
            return vorhanden, False
        # Das erste Dokument gibt es nicht mehr (gelöscht): neu anlegen
    try:
        with transaction.atomic():
            motion = Motion(
                organization=organization,
                author=membership,
                responsible=membership,
                title=titel(paket, heute),
                status="draft",
                visibility="private",
                motion_type="statement",
                document_type=_dokumenttyp(organization),
                letterhead=OrganizationLetterhead.objects.filter(
                    organization=organization, is_active=True, is_default=True
                ).first(),
            )
            ablage: Any = motion  # EncryptionMixin ohne Typangaben
            ablage.set_content_encrypted(dokument_html(paket))
            motion.save()
            MotionChecklistItem.objects.bulk_create(
                MotionChecklistItem(motion=motion, title=punkt, position=position)
                for position, punkt in enumerate(pruefpunkte(paket))
            )
    except Exception:
        cache.delete(schluessel)
        raise
    cache.set(schluessel, str(motion.pk), MAX_ALTER)
    return motion, True
