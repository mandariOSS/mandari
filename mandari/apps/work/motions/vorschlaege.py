# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Änderungsvorschläge im Antragseditor (Teil von #856, Modus „Vorschlagen“).

Ein Vorschlag ist ein Inline-Kommentar (``MotionComment`` mit Marke im Text), der zusätzlich den
vorgeschlagenen Ersatz für die markierte Stelle trägt (``vorschlag``; leer = Stelle streichen). Am Rand lässt er
sich annehmen (der Editor ersetzt die Stelle, wenn ihr Text noch der markierte ist) oder ablehnen; beides
erledigt den Kommentar und hält die Entscheidung fest (``vorschlag_angenommen``). Ein volles „Änderungen
nachverfolgen“ jeder Eingabe ist das nicht (bleibt nach #856 außen vor).

Der Kommentartext beschreibt den Vorschlag, wenn die Person nichts dazuschreibt: So lesen bisherige Ansicht,
Kommentarliste und Benachrichtigungen den Vorschlag ohne eigene Darstellung.
"""

from __future__ import annotations

from typing import Any

#: Länge des vorgeschlagenen Textes (Zeichen); längere Änderungen gehören in den Text selbst
VORSCHLAG_MAX = 10_000
#: Entscheidungen beim Erledigen eines Vorschlags (POST-Feld ``entscheidung``)
ANNEHMEN = "annehmen"
ABLEHNEN = "ablehnen"
#: Zitate in der Beschreibung höchstens so lang (Zeichen); der volle Text steht in ``vorschlag``
ZITAT_MAX = 300


def _zitat(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= ZITAT_MAX else text[: ZITAT_MAX - 1].rstrip() + "…"


def beschreibung(alt: str, neu: str) -> str:
    """Kommentartext eines Vorschlags ohne eigene Notiz, z. B. „Änderungsvorschlag: „a“ ersetzen durch „b““."""
    if not neu.strip():
        return f"Änderungsvorschlag: „{_zitat(alt)}“ streichen"
    return f"Änderungsvorschlag: „{_zitat(alt)}“ ersetzen durch „{_zitat(neu)}“"


def vorschlag_aus_anfrage(post: Any) -> str | None:
    """
    Vorgeschlagener Text aus einem Kommentar-POST oder ``None`` (gewöhnlicher Kommentar).

    Nur für Inline-Kommentare an einer markierten Stelle, nicht für Antworten. Zeilenenden werden vereinheitlicht,
    sonst bleibt der Text, wie er eingegeben wurde (er ersetzt die Stelle wörtlich).
    """
    if "vorschlag" not in post or post.get("parent") or not post.get("selected_text"):
        return None
    return str(post.get("vorschlag", "")).replace("\r\n", "\n").replace("\r", "\n")


def kommentar_anfrage(post: Any) -> tuple[Any, str | None, str]:
    """
    Kommentar-POST für das Formular aufbereiten: (Formulardaten, Vorschlag oder ``None``, Fehlertext oder leer).

    Ohne eigene Notiz beschreibt der Kommentartext den Vorschlag (bisherige Ansicht, Liste, Benachrichtigungen).
    """
    vorschlag = vorschlag_aus_anfrage(post)
    if vorschlag is None:
        return post, None, ""
    alt = str(post.get("selected_text", ""))
    grund = fehler(alt, vorschlag)
    if grund:
        return post, vorschlag, grund
    if not str(post.get("content", "")).strip():
        post = post.copy()
        post["content"] = beschreibung(alt, vorschlag)
    return post, vorschlag, ""


def entscheidung_aus(post: Any) -> str:
    """``annehmen`` oder ``ablehnen`` aus dem POST zum Erledigen, sonst leer (gewöhnliches Erledigen)."""
    wert = str(post.get("entscheidung", ""))
    return wert if wert in (ANNEHMEN, ABLEHNEN) else ""


def fehler(alt: str, neu: str) -> str:
    """Grund, warum der Vorschlag nicht angelegt wird, oder leer."""
    if len(neu) > VORSCHLAG_MAX:
        return "Der Vorschlag ist zu lang (höchstens 10.000 Zeichen)."
    if neu == alt:
        return "Der Vorschlag ändert nichts an der markierten Stelle."
    return ""


def darf_entscheiden(comment: Any, membership: Any, entscheidung: str) -> bool:
    """
    Annehmen ändert den Text: nur, wer ihn gerade bearbeiten darf (Editor-Stufe mit Status-Sperre). Ablehnen
    darf außerdem, wer den Vorschlag gemacht hat (zurückziehen) oder alle Dokumente bearbeiten darf.
    """
    darf_text = comment.motion.editor_access_level(membership) in ("edit", "admin")
    if entscheidung == ANNEHMEN:
        return darf_text
    return darf_text or comment.author_id == membership.id or membership.has_permission("motions.edit_all")


def offene_zahlen(comments: Any) -> tuple[int, int]:
    """(offene Kommentare, offene Vorschläge) der obersten Ebene."""
    kommentare = vorschlaege = 0
    for comment in comments:
        if comment.is_resolved:
            continue
        if getattr(comment, "vorschlag", None) is not None:
            vorschlaege += 1
        else:
            kommentare += 1
    return kommentare, vorschlaege


def notiz(comment: Any) -> str:
    """Eigene Notiz zu einem Vorschlag: der Kommentartext, wenn er nicht nur den Vorschlag beschreibt."""
    if comment.vorschlag is None:
        return ""
    if comment.content == beschreibung(comment.selected_text or "", comment.vorschlag):
        return ""
    return str(comment.content)
