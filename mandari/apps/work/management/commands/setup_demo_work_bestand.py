# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Bestand der Musterstadt für Work: eine Fraktion mit einem Jahr Arbeit (Issue #99).

Die Basisdemo (``setup_demo_environment``, ``setup_demo_work``) erzählt wenige Geschichten. Dieser Befehl füllt die
Musterfraktion (im RIS die Fraktion Bunte Liste) so, dass sie wie eine echte, aktive Fraktion aussieht. Grundlage ist
der Plan aus ``apps.common.demo_stadt.stadtplan`` (Aufbautag und Umfang gibt ``setup_demo_environment`` weiter):

- Mitglieder: die Ratsmitglieder und sachkundigen Bürger der Bunten Liste aus dem Katalog, je mit Konto **ohne
  nutzbares Passwort** (keine Anmeldung), Rolle, RIS-Person und Ausschüssen laut Besetzung; Ankündigungen und
  E-Mails sind für sie abgeschaltet
- Dokumenttypen (Antrag, Anfrage, Haushaltsantrag, Pressemitteilung, Rede …) und eine Ordnerstruktur
- Dokumente: jeder Antrag und jede Anfrage der Bunten Liste aus dem Plan mit Stand passend zur Beratung, dazu
  Pressemitteilungen, Reden, Haushaltsanträge, Positionspapiere, Arbeitskreisnotizen, Vermerke im Ordner
  „Nichtöffentliche Vorgänge“ und Änderungsanträge zu kommenden Vorlagen – mit Kommentaren, Checklisten,
  Fassungen, Mitarbeit, Freigaben an den Gast und offenen Freigabeanfragen
- Sitzungsvorbereitung: Positionen, Notizen, private Notizen, Redebeiträge und Kommentare zu Vorlagen für die
  kommenden Sitzungen, abgeschlossene Vorbereitungen mit Ergebnis für vergangene
- Aufgaben mit Labels, Zuständigen, Fälligkeiten (überfällig, heute, kommend, erledigt), Kommentaren und
  Checklisten; Abwesenheiten mit Vertretung; Benachrichtigungen für die Demo-Konten

Idempotent: Der Befehl entfernt zuerst den eigenen Bestand (feste Titel aus dem Katalog, Verweise auf Bestands-
Objekte im RIS) und legt ihn neu an. Konten, Mitgliedschaften, Dokumenttypen, Ordner und Labels werden nur
aktualisiert. Basisdemo und Präsentation (``setup_demo_praesentation``) bleiben unberührt.

``--entfernen`` entfernt nur den Bestand. ``setup_demo_environment`` braucht das, bevor Insight seinen Bestand neu
anlegt: Positionen, Notizen und Vorbereitungen schützen ihre RIS-Objekte vor dem Löschen (``PROTECT``).

Fehlen RIS-Objekte (Insight-Bestand nicht angelegt), entstehen die Work-Inhalte ohne Verknüpfung.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from typing import Any, cast

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.common.demo_daten import DEMO_ORG_SLUG, DEMO_USERS, demo_ext
from apps.common.demo_stadt import (
    BESTAND,
    PERSON,
    PERSONEN,
    UMFAENGE,
    Sitzung,
    Stadtplan,
    Top,
    Vorgang,
    demo_umfang,
    stabil,
    stadtplan,
    top_kennung,
)
from apps.tenants.models import Membership, Organization, Role
from apps.work.meetings.models import (
    AgendaItemNote,
    AgendaItemPosition,
    AgendaPrivateNote,
    AgendaSpeechNote,
    AgendaSupplementaryDocument,
    FileAnnotation,
    MeetingPreparation,
    PaperComment,
)
from apps.work.motions.models import (
    DocumentFolder,
    Motion,
    MotionApproval,
    MotionChecklistItem,
    MotionComment,
    MotionRevision,
    MotionShare,
    MotionType,
)
from apps.work.notifications.models import Notification, NotificationPreference, NotificationType
from apps.work.organization.models import MemberAbsence
from apps.work.tasks.models import Task, TaskActivity, TaskChecklistItem, TaskComment, TaskLabel
from hub.ris import selectors as ris
from insight_core.models import OParlAgendaItem, OParlMeeting, OParlPaper

FRAKTION = "fraktion-bunte-liste"
#: Personen der Basisdemo mit eigenem Demo-Konto in Work (Schlüssel aus DEMO_USERS)
BASIS_KONTEN = {
    "anna-amberg": "vorsitz",
    "bernd-birkholz": "mitglied",
    "dieter-dahl": "unvereidigt",
    "sofia-sachkundig": "sachkundig",
}
#: Stellvertretender Vorsitz im Bestand
STELLV_VORSITZ = "jana-jacobs"
#: Marke an Benachrichtigungen des Bestands (``metadata``)
MARKE = "demo_bestand"
#: Gründe der Abwesenheiten (natürlicher Schlüssel)
ABWESENHEITEN = (
    ("nadine-novak", -10, 40, "Elternzeit (Demo)", "lea-lorenz"),
    ("ole-olsen", 12, 26, "Urlaub (Demo)", "ruben-roth"),
)

DOKUMENTTYPEN: tuple[tuple[str, str, str, str, bool, bool, list[str]], ...] = (
    (
        "antrag",
        "Antrag",
        "file-text",
        "blue",
        True,
        True,
        ["Fraktionsbeschluss eingeholt", "Kosten und Deckung geklärt", "Begründung gegengelesen"],
    ),
    ("anfrage", "Anfrage", "help-circle", "teal", False, True, ["Fragen mit dem Arbeitskreis abgestimmt"]),
    ("aenderungsantrag", "Änderungsantrag", "edit", "gray", True, True, ["Mit der Fraktion abgestimmt"]),
    (
        "haushaltsantrag",
        "Haushaltsantrag",
        "scale",
        "amber",
        True,
        True,
        ["Deckungsvorschlag mit der Kämmerei abgestimmt", "Fraktionsbeschluss eingeholt"],
    ),
    (
        "pressemitteilung",
        "Pressemitteilung",
        "megaphone",
        "pink",
        True,
        False,
        ["Zitate freigegeben", "Foto ausgewählt"],
    ),
    ("rede", "Rede", "message-square", "purple", False, False, []),
    ("positionspapier", "Positionspapier", "book-open", "green", True, False, []),
    ("vermerk", "Vermerk", "clipboard-list", "gray", False, False, []),
)

# Ordner: Schlüssel, Name, Farbe, übergeordneter Schlüssel
ORDNER: tuple[tuple[str, str, str, str | None], ...] = (
    ("antraege", "Anträge (Demo)", "indigo", None),
    ("beschlossen", "Beschlossen (Demo)", "green", "antraege"),
    ("erledigt", "Abgelehnt und zurückgezogen (Demo)", "gray", "antraege"),
    ("anfragen", "Anfragen (Demo)", "teal", None),
    ("haushalt", "Haushalt (Demo)", "amber", None),
    ("presse", "Presse und Social Media (Demo)", "pink", None),
    ("reden", "Reden (Demo)", "purple", None),
    ("strategie", "Klausur und Strategie (Demo)", "orange", None),
    ("ak", "Arbeitskreise (Demo)", "blue", None),
    ("ak-bau", "AK Bauen und Verkehr (Demo)", "blue", "ak"),
    ("ak-sozial", "AK Soziales und Jugend (Demo)", "blue", "ak"),
    ("ak-umwelt", "AK Umwelt und Klima (Demo)", "blue", "ak"),
)

LABELS: tuple[tuple[str, str], ...] = (
    ("Presse", "pink"),
    ("Haushalt", "amber"),
    ("Ratssitzung", "indigo"),
    ("Ausschuss", "blue"),
    ("Bürgeranliegen", "green"),
    ("Social Media", "purple"),
    ("Veranstaltung", "teal"),
    ("Wahlkreis", "orange"),
)


@dataclass(frozen=True)
class Dokument:
    """Eigene Arbeit der Fraktion (nicht aus dem RIS-Katalog)."""

    titel: str
    typ: str  # Schlüssel aus DOKUMENTTYPEN
    ordner: str  # Schlüssel aus ORDNER, "noe" für „Nichtöffentliche Vorgänge“
    status: str
    tag: int  # angelegt am (relativ zum Aufbautag)
    autor: str  # Personenschlüssel
    zusammenfassung: str
    absaetze: tuple[str, ...]
    mitarbeit: tuple[str, ...] = ()
    kommentare: tuple[tuple[str, str, bool], ...] = ()  # Autor, Text, erledigt
    frist: int | None = None
    gast: bool = False  # für den Gast freigegeben (Lesen)
    freigabe_offen: bool = False  # Freigabe beim Vorsitz angefragt
    fassungen: int = 1


EIGENE_DOKUMENTE: tuple[Dokument, ...] = (
    # Presse
    Dokument(
        "Pressemitteilung: Sichere Schulwege für alle Kinder (Demo)",
        "pressemitteilung",
        "presse",
        "completed",
        -210,
        "lea-lorenz",
        "Pressemitteilung zum Antrag „Sichere Radwege zu allen Schulen“.",
        (
            "Die Fraktion Bunte Liste fordert ein Konzept für sichere Radwege zu allen Schulen in Musterstadt.",
            "„Kinder sollen selbstständig und sicher zur Schule kommen“, sagt Fraktionsvorsitzende Vera Beispiel.",
        ),
        kommentare=(("vorsitz", "Zitat passt so, bitte noch das Foto vom Ortstermin anhängen.", True),),
        gast=True,
        fassungen=2,
    ),
    Dokument(
        "Pressemitteilung: Livestream der Ratssitzungen kommt (Demo)",
        "pressemitteilung",
        "presse",
        "completed",
        -150,
        "karim-kaya",
        "Der Rat hat unseren Antrag auf einen Livestream beschlossen.",
        (
            "Ab dem kommenden Jahr werden die öffentlichen Ratssitzungen im Internet übertragen.",
            "Damit wird Kommunalpolitik für alle Bürgerinnen und Bürger transparenter.",
        ),
        gast=True,
    ),
    Dokument(
        "Pressemitteilung: Sozialticket beschlossen (Demo)",
        "pressemitteilung",
        "presse",
        "completed",
        -60,
        "jana-jacobs",
        "Mobilität für alle: Das Sozialticket kommt.",
        (
            "Menschen mit geringem Einkommen erhalten künftig ein vergünstigtes Monatsticket.",
            "Die Fraktion dankt den Sozialverbänden für die Unterstützung.",
        ),
        fassungen=2,
    ),
    Dokument(
        "Pressemitteilung: Hitzeschutz in der Innenstadt (Demo)",
        "pressemitteilung",
        "presse",
        "approved",
        -6,
        "ole-olsen",
        "Mehr Schatten, Trinkwasser und Bänke für heiße Sommer.",
        (
            "Die Bunte Liste begrüßt den Hitzeaktionsplan und fordert eine schnelle Umsetzung.",
            "Besonders ältere Menschen und Kinder brauchen kühle Orte in der Innenstadt.",
        ),
        kommentare=(
            ("mitglied", "Bitte die Zahl der Hitzetage aus dem Klimabericht ergänzen.", True),
            ("vorsitz", "Freigegeben, Versand am Montag.", False),
        ),
    ),
    Dokument(
        "Pressemitteilung zur kommenden Ratssitzung (Demo)",
        "pressemitteilung",
        "presse",
        "internal_review",
        -2,
        "lea-lorenz",
        "Vorschau auf unsere Themen in der nächsten Ratssitzung.",
        (
            "In der kommenden Ratssitzung bringt die Bunte Liste mehrere Anträge ein.",
            "Schwerpunkte sind Trinkwasser in der Innenstadt und mehr Beteiligung junger Menschen.",
        ),
        kommentare=(("sachkundig", "Den Jugendbeirat würde ich an den Anfang stellen.", False),),
        frist=5,
        freigabe_offen=True,
        mitarbeit=("vorsitz",),
    ),
    Dokument(
        "Social-Media-Plan für das Quartal (Demo)",
        "vermerk",
        "presse",
        "draft",
        -20,
        "karim-kaya",
        "Themen, Formate und Zuständigkeiten für Instagram, Mastodon und den Newsletter.",
        (
            "Wöchentlich ein Beitrag aus der Ratsarbeit, monatlich ein Video aus dem Wahlkreis.",
            "Zuständig: Karim (Planung), Lea (Texte), Moritz (Fotos).",
        ),
        mitarbeit=("lea-lorenz", "moritz-meyer"),
    ),
    # Reden
    Dokument(
        "Rede zum Haushalt (Demo)",
        "rede",
        "reden",
        "completed",
        -120,
        "anna-amberg",
        "Haushaltsrede der Fraktionsvorsitzenden.",
        (
            "Sehr geehrte Frau Bürgermeisterin, liebe Kolleginnen und Kollegen,",
            "dieser Haushalt setzt die richtigen Schwerpunkte bei Kitas, Schulen und Klimaschutz.",
            "Wir stimmen zu – und bleiben bei der Sanierung der Schultoiletten dran.",
        ),
        fassungen=3,
    ),
    Dokument(
        "Rede: Klimacheck für alle Beschlussvorlagen (Demo)",
        "rede",
        "reden",
        "completed",
        -40,
        "ole-olsen",
        "Redebeitrag zu unserem Antrag im Rat.",
        (
            "Klimafolgen müssen bei jeder Entscheidung auf dem Tisch liegen.",
            "Der Klimacheck kostet wenig und schafft Klarheit.",
        ),
        fassungen=2,
    ),
    Dokument(
        "Redeentwurf: Wohnraumversorgungskonzept (Demo)",
        "rede",
        "reden",
        "draft",
        -3,
        "ruben-roth",
        "Entwurf für die Ratssitzung.",
        (
            "Musterstadt braucht 1.400 zusätzliche Wohnungen – ein Drittel davon bezahlbar.",
            "Die Quote von 30 Prozent gefördertem Wohnraum ist das Mindeste.",
        ),
        kommentare=(("vorsitz", "Bitte kürzer, maximal drei Minuten.", False),),
        frist=10,
    ),
    Dokument(
        "Redeentwurf: Kommunale Wärmeplanung (Demo)",
        "rede",
        "reden",
        "internal_review",
        -5,
        "karim-kaya",
        "Redebeitrag zur Wärmeplanung.",
        (
            "Die Wärmeplanung gibt Hausbesitzerinnen und Mietern endlich Planungssicherheit.",
            "Wir wollen eine unabhängige Energieberatung im Rathaus.",
        ),
        mitarbeit=("ole-olsen",),
        frist=12,
    ),
    # Haushalt
    Dokument(
        "Haushaltsantrag: Schulsozialarbeit an allen Grundschulen (Demo)",
        "haushaltsantrag",
        "haushalt",
        "adopted",
        -130,
        "jana-jacobs",
        "190.000 Euro für Schulsozialarbeit an allen fünf Grundschulen.",
        (
            "Der Ansatz für Schulsozialarbeit wird um 190.000 Euro erhöht.",
            "Deckung: Minderaufwand bei den Energiekosten durch die LED-Umstellung.",
        ),
        fassungen=2,
    ),
    Dokument(
        "Haushaltsantrag: Radverkehrsbudget verdoppeln (Demo)",
        "haushaltsantrag",
        "haushalt",
        "rejected",
        -130,
        "ruben-roth",
        "Mehr Geld für Radwege – im Rat ohne Mehrheit.",
        (
            "Das Budget für den Radverkehr wird von 250.000 auf 500.000 Euro erhöht.",
            "Deckung: Verschiebung der Sanierung des Parkhauses Altstadt um ein Jahr.",
        ),
    ),
    Dokument(
        "Haushaltsantrag: Stadtteilcafé im Musterviertel (Demo)",
        "haushaltsantrag",
        "haushalt",
        "adopted",
        -128,
        "nadine-novak",
        "Zuschuss für das Stadtteilcafé, beschlossen.",
        ("Der Verein Nachbarschaft Musterviertel erhält einen jährlichen Zuschuss von 16.000 Euro.",),
    ),
    Dokument(
        "Haushaltsantrag: Baumpflanzungen im Musterviertel (Demo)",
        "haushaltsantrag",
        "haushalt",
        "draft",
        -4,
        "ole-olsen",
        "Für die nächsten Haushaltsberatungen.",
        ("Im Musterviertel werden zusätzlich 40 Straßenbäume gepflanzt.", "Kosten: 120.000 Euro brutto."),
        kommentare=(("mitglied", "Deckungsvorschlag fehlt noch.", False),),
        frist=30,
    ),
    Dokument(
        "Haushaltsklausur: Übersicht unserer Anträge (Demo)",
        "positionspapier",
        "haushalt",
        "completed",
        -140,
        "anna-amberg",
        "Ergebnis der Haushaltsklausur.",
        (
            "Sieben Haushaltsanträge, Schwerpunkte Bildung, Klima und Soziales.",
            "Gesamtvolumen: 640.000 Euro, vollständig gedeckt.",
        ),
        mitarbeit=("jana-jacobs", "bernd-birkholz"),
    ),
    # Strategie
    Dokument(
        "Positionspapier: Mobilität in Musterstadt 2035 (Demo)",
        "positionspapier",
        "strategie",
        "approved",
        -90,
        "ruben-roth",
        "Leitlinien der Fraktion für Rad, Bus und Fußverkehr.",
        (
            "Bis 2035 sollen die Hälfte aller Wege mit dem Rad, zu Fuß oder mit dem Bus zurückgelegt werden.",
            "Dafür braucht es durchgehende Radwege, einen dichteren Bustakt und sichere Schulwege.",
        ),
        gast=True,
        fassungen=3,
        mitarbeit=("clara-cornelsen", "sofia-sachkundig"),
    ),
    Dokument(
        "Positionspapier: Bezahlbar wohnen (Demo)",
        "positionspapier",
        "strategie",
        "internal_review",
        -15,
        "clara-cornelsen",
        "Unsere Antworten auf die Wohnungsnot.",
        (
            "Wir wollen eine Quote von 30 Prozent gefördertem Wohnraum in jedem neuen Baugebiet.",
            "Die städtische Wohnungsbaugesellschaft soll jährlich 50 Wohnungen bauen.",
        ),
        kommentare=(
            ("vorsitz", "Sollen wir eine Zweckentfremdungssatzung aufnehmen?", False),
            ("mitglied", "Zahlen zur Wohnungsbaugesellschaft bitte prüfen.", True),
        ),
        freigabe_offen=True,
        fassungen=2,
    ),
    Dokument(
        "Klausurtagung: Arbeitsprogramm bis zur Kommunalwahl (Demo)",
        "positionspapier",
        "strategie",
        "completed",
        -250,
        "anna-amberg",
        "Ergebnisse der Klausurtagung im Frühjahr.",
        (
            "Zehn Vorhaben bis zur Kommunalwahl, je mit Zuständigkeit und Zeitplan.",
            "Halbjährliche Überprüfung in der Fraktionssitzung.",
        ),
        mitarbeit=("jana-jacobs",),
    ),
    Dokument(
        "Wahlprogramm – Entwurf Kapitel Bildung (Demo)",
        "positionspapier",
        "strategie",
        "draft",
        -8,
        "jana-jacobs",
        "Erster Entwurf, Rückmeldungen bis Monatsende.",
        (
            "Jede Schule in Musterstadt bekommt Schulsozialarbeit und einen Ganztag mit Qualität.",
            "Wir wollen Schulgebäude, in denen man gern lernt.",
        ),
        mitarbeit=("lea-lorenz", "tim-tessmer"),
        frist=21,
    ),
    # Arbeitskreise
    Dokument(
        "AK Bauen: Notizen zur Erneuerung der Hauptstraße (Demo)",
        "vermerk",
        "ak-bau",
        "draft",
        -30,
        "sofia-sachkundig",
        "Offene Fragen für den Bauausschuss.",
        ("Wie lange dauert die Vollsperrung?", "Bleibt der Radfahrstreifen in Variante 2 durchgehend?"),
        mitarbeit=("clara-cornelsen",),
    ),
    Dokument(
        "Ortsbegehung Lindenstraße: Ergebnisse (Demo)",
        "vermerk",
        "ak-bau",
        "completed",
        -75,
        "ruben-roth",
        "Begehung mit Anwohnenden und dem Fahrradclub.",
        ("Die Fahrradstraße wird gut angenommen; an zwei Einmündungen fehlt die Markierung.",),
    ),
    Dokument(
        "AK Soziales: Gespräch mit der Tafel (Demo)",
        "vermerk",
        "ak-sozial",
        "completed",
        -45,
        "nadine-novak",
        "Ergebnis des Gesprächs mit dem Vorstand der Tafel.",
        ("Die Tafel versorgt inzwischen 640 Haushalte pro Woche.", "Das Kühlfahrzeug ist dringend nötig."),
    ),
    Dokument(
        "Fragenkatalog Kita-Bedarfsplanung (Demo)",
        "vermerk",
        "ak-sozial",
        "draft",
        -9,
        "tim-tessmer",
        "Fragen für den Jugendhilfeausschuss.",
        ("Wie viele Kinder stehen auf der Warteliste?", "Wann öffnet die Kita am Südhang?"),
        kommentare=(("mitglied", "Frage zur Personalsituation ergänzen.", False),),
    ),
    Dokument(
        "AK Umwelt: Stellungnahme zum Hitzeaktionsplan (Demo)",
        "positionspapier",
        "ak-umwelt",
        "approved",
        -50,
        "ole-olsen",
        "Stellungnahme des Arbeitskreises.",
        ("Der Plan ist gut, braucht aber ein eigenes Budget.", "Trinkwasserstellen gehören in jedes Quartier."),
    ),
    Dokument(
        "Checkliste Klimacheck für Beschlussvorlagen (Demo)",
        "vermerk",
        "ak-umwelt",
        "draft",
        -12,
        "karim-kaya",
        "Kriterien für die Einschätzung der Klimafolgen.",
        ("Energieverbrauch, Flächenversiegelung, Verkehr, Bäume und Grün.",),
        mitarbeit=("ole-olsen",),
    ),
    # Nichtöffentlich (nur vereidigte Mitglieder)
    Dokument(
        "Vermerk: Grundstücksangelegenheit Gewerbegebiet Ost (Demo)",
        "vermerk",
        "noe",
        "draft",
        -18,
        "anna-amberg",
        "Vertraulich: Stand der Grundstücksverhandlungen.",
        (
            "Die Verwaltung verhandelt mit zwei Eigentümern über insgesamt 4,2 Hektar.",
            "Preisvorstellungen liegen noch auseinander; Beratung im nicht-öffentlichen Teil.",
        ),
    ),
    Dokument(
        "Vergabe Winterdienst: Bewertung der Angebote (Demo)",
        "vermerk",
        "noe",
        "completed",
        -70,
        "bernd-birkholz",
        "Vertraulich: Auswertung für den nicht-öffentlichen Teil des Bauausschusses.",
        ("Drei Angebote, das wirtschaftlichste liegt 8 Prozent unter der Kostenschätzung.",),
    ),
)

# Aufgaben: Titel, Beschreibung, Status, Priorität, zuständig, Fälligkeit (Tage), Labels, Checkliste, Kommentare
AUFGABEN: tuple[
    tuple[str, str, str, str, str, int | None, tuple[str, ...], tuple[str, ...], tuple[tuple[str, str], ...]], ...
] = (
    (
        "Bürgersprechstunde auf dem Marktplatz organisieren (Demo)",
        "Stand, Flyer und Dienstplan für Samstag.",
        "in_progress",
        "medium",
        "lea-lorenz",
        3,
        ("Bürgeranliegen", "Veranstaltung"),
        ("Stand beim Ordnungsamt angemeldet", "Flyer gedruckt", "Dienstplan verschickt"),
        (("vorsitz", "Ich komme von 10 bis 11 Uhr dazu."),),
    ),
    (
        "Anwohnerbrief Lindenstraße beantworten (Demo)",
        "Beschwerde über Falschparker an der Fahrradstraße.",
        "todo",
        "high",
        "ruben-roth",
        -2,
        ("Bürgeranliegen", "Wahlkreis"),
        (),
        (("mitglied", "Ordnungsamt in Kopie nehmen?"),),
    ),
    (
        "Newsletter für den Monat schreiben (Demo)",
        "Themen: Hitzeaktionsplan, Sozialticket, Termine.",
        "todo",
        "medium",
        "karim-kaya",
        6,
        ("Presse", "Social Media"),
        ("Texte sammeln", "Fotos auswählen", "Versand planen"),
        (),
    ),
    (
        "Fotos vom Ortstermin Spielplatz hochladen (Demo)",
        "",
        "done",
        "low",
        "moritz-meyer",
        -12,
        ("Social Media",),
        (),
        (),
    ),
    (
        "Haushaltsklausur vorbereiten (Demo)",
        "Raum, Verpflegung und Tagesordnung für die Klausur.",
        "todo",
        "high",
        "vorsitz",
        18,
        ("Haushalt", "Veranstaltung"),
        ("Raum reservieren", "Tagesordnung entwerfen", "Kämmerin einladen"),
        (("mitglied", "Der Saal im Bürgerhaus ist am Samstag frei."),),
    ),
    (
        "Deckungsvorschläge für Haushaltsanträge sammeln (Demo)",
        "Mit der Kämmerei abstimmen.",
        "in_progress",
        "high",
        "bernd-birkholz",
        9,
        ("Haushalt",),
        ("Liste der Anträge", "Termin Kämmerei"),
        (),
    ),
    (
        "Gespräch mit dem Seniorenbeirat (Demo)",
        "Themen: Seniorentaxi, Bänke, Digital fit im Alter.",
        "todo",
        "medium",
        "nadine-novak",
        14,
        ("Bürgeranliegen",),
        (),
        (),
    ),
    (
        "Pressemitteilung Hitzeschutz versenden (Demo)",
        "Verteiler Lokalpresse und Radio.",
        "todo",
        "high",
        "lea-lorenz",
        0,
        ("Presse",),
        ("Freigabe Vorsitz", "Versand"),
        (("vorsitz", "Freigegeben."),),
    ),
    (
        "Fraktionsfest im Sommer planen (Demo)",
        "Ort, Termin, Einladungen.",
        "todo",
        "low",
        "petra-pohl",
        60,
        ("Veranstaltung",),
        (),
        (),
    ),
    (
        "Antwort der Verwaltung zur Anfrage Radwege auswerten (Demo)",
        "Ergebnisse in den AK Bauen einbringen.",
        "done",
        "medium",
        "ruben-roth",
        -30,
        ("Ausschuss",),
        (),
        (("sachkundig", "Zusammenfassung liegt im AK-Ordner."),),
    ),
    (
        "Stellungnahme zum Bebauungsplan Südhang abstimmen (Demo)",
        "Frist der Offenlage beachten.",
        "in_progress",
        "urgent",
        "sachkundig",
        2,
        ("Ausschuss",),
        ("Pläne sichten", "Stellungnahme schreiben", "Mit AK abstimmen"),
        (),
    ),
    (
        "Website: Seite zum Sozialticket aktualisieren (Demo)",
        "",
        "done",
        "low",
        "karim-kaya",
        -40,
        ("Social Media",),
        (),
        (),
    ),
    (
        "Termin mit dem Jugendring vereinbaren (Demo)",
        "Thema Jugendzentrum und Jugendbeirat.",
        "todo",
        "medium",
        "tim-tessmer",
        10,
        ("Bürgeranliegen",),
        (),
        (),
    ),
    ("Reisekostenabrechnung Fraktionsklausur (Demo)", "", "done", "low", "bernd-birkholz", -55, ("Haushalt",), (), ()),
    (
        "Kleine Anfrage zur Kita-Warteliste vorbereiten (Demo)",
        "Zahlen aus dem Jugendhilfeausschuss nutzen.",
        "todo",
        "medium",
        "jana-jacobs",
        7,
        ("Ausschuss",),
        (),
        (("mitglied", "Ich schicke dir die Zahlen von letzter Woche."),),
    ),
    (
        "Infostand Stadtfest betreuen (Demo)",
        "Schichtplan im Kalender.",
        "done",
        "medium",
        "petra-pohl",
        -100,
        ("Veranstaltung",),
        ("Material", "Schichtplan"),
        (),
    ),
    (
        "Rückmeldungen aus dem Wahlkreis Nord sammeln (Demo)",
        "Für die nächste Fraktionssitzung.",
        "in_progress",
        "low",
        "unvereidigt",
        11,
        ("Wahlkreis",),
        (),
        (),
    ),
    (
        "Einarbeitung neuer Mitglieder: Leitfaden aktualisieren (Demo)",
        "",
        "todo",
        "low",
        "jana-jacobs",
        25,
        (),
        ("Gliederung", "Kapitel RIS und Work"),
        (),
    ),
    (
        "Instagram-Beitrag zur Ratssitzung (Demo)",
        "Kurzvideo mit Zitat.",
        "todo",
        "medium",
        "moritz-meyer",
        15,
        ("Social Media", "Ratssitzung"),
        (),
        (),
    ),
    ("Protokoll der Fraktionssitzung gegenlesen (Demo)", "", "done", "medium", "mitglied", -6, (), (), ()),
    (
        "Ortstermin Brücke Mühlenweg mit dem Tiefbauamt (Demo)",
        "Zustand vor der Sanierung ansehen.",
        "todo",
        "medium",
        "clara-cornelsen",
        8,
        ("Ausschuss",),
        ("Termin bestätigen", "Fotos machen"),
        (),
    ),
    ("Leserbrief zum Parkraumkonzept beantworten (Demo)", "", "done", "low", "lea-lorenz", -18, ("Presse",), (), ()),
    (
        "Einladung Neujahrsempfang verschicken (Demo)",
        "Gästeliste im Ordner Veranstaltungen.",
        "todo",
        "medium",
        "petra-pohl",
        35,
        ("Veranstaltung",),
        ("Gästeliste", "Einladung gestalten", "Versand"),
        (),
    ),
    (
        "Kita-Eltern im Musterviertel zurückrufen (Demo)",
        "Thema Wartelisten, Rückruf zugesagt.",
        "todo",
        "high",
        "nadine-novak",
        -1,
        ("Bürgeranliegen", "Wahlkreis"),
        (),
        (("vorsitz", "Bitte vor der JHA-Sitzung."),),
    ),
    (
        "Fragen für den Rechnungsprüfungsausschuss sammeln (Demo)",
        "",
        "todo",
        "medium",
        "bernd-birkholz",
        16,
        ("Ausschuss", "Haushalt"),
        (),
        (),
    ),
    (
        "Antrag Schultoiletten: Sachstand bei der Verwaltung erfragen (Demo)",
        "Beschluss ist acht Monate alt.",
        "in_progress",
        "medium",
        "jana-jacobs",
        4,
        ("Ausschuss",),
        ("Mail an Amt 40", "Antwort auswerten"),
        (("mitglied", "Ich habe eine Teilantwort bekommen, schicke ich dir."),),
    ),
    (
        "Fahrradtour durch den Wahlkreis Süd planen (Demo)",
        "Route, Treffpunkt, Ankündigung.",
        "todo",
        "low",
        "ruben-roth",
        40,
        ("Wahlkreis", "Veranstaltung"),
        ("Route", "Ankündigung Social Media"),
        (),
    ),
    (
        "Fraktionsbeitrag für das Amtsblatt schreiben (Demo)",
        "Maximal 2.000 Zeichen.",
        "in_progress",
        "medium",
        "karim-kaya",
        5,
        ("Presse",),
        (),
        (),
    ),
    (
        "Gespräch mit dem Fahrradclub zur Lindenstraße (Demo)",
        "",
        "done",
        "low",
        "sachkundig",
        -25,
        ("Bürgeranliegen",),
        (),
        (("sachkundig", "Ergebnisse im AK-Ordner."),),
    ),
    (
        "Haushaltsrede: Zahlen aktualisieren (Demo)",
        "Ergebnis der Steuerschätzung einarbeiten.",
        "todo",
        "high",
        "vorsitz",
        12,
        ("Haushalt",),
        ("Steuerschätzung", "Investitionsliste"),
        (),
    ),
    (
        "Neue Mitglieder in Work einladen (Demo)",
        "Zwei Nachrücker aus dem Ortsverband.",
        "done",
        "low",
        "jana-jacobs",
        -9,
        (),
        (),
        (),
    ),
    (
        "Spendenquittungen für das Sommerfest ausstellen (Demo)",
        "",
        "todo",
        "low",
        "petra-pohl",
        20,
        ("Veranstaltung",),
        (),
        (),
    ),
)

#: Präfixe der Aufgaben zu kommenden Tagesordnungspunkten (Titel: „<Präfix>: <Thema> (Demo)“)
TOP_AUFGABEN = ("Redebeitrag vorbereiten", "Position abstimmen")


def _thema(vorgang: Vorgang) -> str:
    """„Antrag der Fraktion Bunte Liste: Livestream …“ → „Livestream …“."""
    return vorgang.titel.split(": ", 1)[1] if ": " in vorgang.titel else vorgang.titel


def _dokumenttitel(vorgang: Vorgang) -> str:
    art = {"antrag": "Antrag", "anfrage": "Anfrage"}.get(vorgang.art, "Dokument")
    return f"{art}: {_thema(vorgang)} (Demo)"


def _aenderungstitel(vorgang: Vorgang) -> str:
    return f"Änderungsantrag: {_thema(vorgang)} (Demo)"


def _html(titel: str, abschnitte: list[tuple[str, list[str]]]) -> str:
    teile = [f"<h2>{titel}</h2>"]
    for ueberschrift, absaetze in abschnitte:
        if ueberschrift:
            teile.append(f"<h3>{ueberschrift}</h3>")
        teile.extend(f"<p>{absatz}</p>" for absatz in absaetze)
    return "".join(teile)


@dataclass
class Welt:
    """Was der Lauf braucht: Organisation, Mitglieder, RIS-Objekte, Ordner und Typen."""

    org: Organization
    plan: Stadtplan
    m: dict[str, Membership] = field(default_factory=dict)  # Personenschlüssel und Demo-Rollen → Mitgliedschaft
    papiere: dict[str, OParlPaper] = field(default_factory=dict)  # Vorgangsschlüssel → Vorlage
    sitzungen: dict[str, OParlMeeting] = field(default_factory=dict)  # Sitzungsschlüssel → Sitzung
    tops: dict[str, OParlAgendaItem] = field(default_factory=dict)  # top_kennung → TOP
    ordner: dict[str, DocumentFolder] = field(default_factory=dict)
    typen: dict[str, MotionType] = field(default_factory=dict)
    dokumente: dict[str, Motion] = field(default_factory=dict)  # Vorgangsschlüssel → Dokument
    aufgaben: list[Task] = field(default_factory=list)


class Command(BaseCommand):
    help = "Ergänzt die Demo-Umgebung um den Bestand der Musterstadt: Work (Musterfraktion)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--aufbautag", type=date.fromisoformat, default=None, help="Bezugstag (JJJJ-MM-TT)")
        parser.add_argument("--umfang", choices=UMFAENGE, default=None, help="voll (Demo) oder klein (Tests)")
        parser.add_argument(
            "--entfernen",
            action="store_true",
            help="Nur den Work-Bestand entfernen (vor dem Neuanlegen des Insight-Bestands)",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        org = Organization.objects.filter(slug=DEMO_ORG_SLUG).first()
        if org is None:
            if options["entfernen"]:
                return
            raise CommandError(
                f"Demo-Organisation {DEMO_ORG_SLUG} fehlt – zuerst python manage.py setup_demo_environment ausführen."
            )
        self.aufbautag: date = options["aufbautag"] or timezone.localdate()
        self.zaehler: dict[str, int] = {}
        # Alle Titel, die der Bestand je anlegen kann (unabhängig von Umfang und Tag): Grundlage des Entfernens
        self.voll = stadtplan(self.aufbautag, "voll")
        with transaction.atomic():
            entfernt = self._entfernen(org)
            if options["entfernen"]:
                self.stdout.write(f"  - Work-Bestand entfernt: {entfernt} Objekte")
                return
            welt = Welt(org=org, plan=stadtplan(self.aufbautag, options["umfang"] or demo_umfang()))
            self._mitglieder(welt)
            self._ris(welt)
            self._typen_und_ordner(welt)
            self._katalogdokumente(welt)
            self._eigene_dokumente(welt)
            self._vorbereitung(welt)
            self._aufgaben(welt)
            self._abwesenheiten(welt)
            self._benachrichtigungen(welt)
        self.stdout.write(self.style.SUCCESS("Musterstadt-Bestand in Work:"))
        for name, anzahl in self.zaehler.items():
            self.stdout.write(f"  - Work: {name}: {anzahl}")

    # ------------------------------------------------------------------
    # Hilfen
    # ------------------------------------------------------------------

    def _zaehlen(self, name: str, anzahl: int = 1) -> None:
        self.zaehler[name] = self.zaehler.get(name, 0) + anzahl

    def _zeit(self, tag: int, stunde: int = 10, minute: int = 0) -> datetime:
        return timezone.make_aware(datetime.combine(self.aufbautag + timedelta(days=tag), dt_time(stunde, minute)))

    def _datum(self, tag: int) -> date:
        return self.aufbautag + timedelta(days=tag)

    @staticmethod
    def _zurueckdatieren(modell: Any, zeiten: dict[Any, tuple[datetime, datetime | None]]) -> None:
        """``auto_now``-Felder überschreiben: Der Bestand soll ein Jahr Arbeit zeigen, nicht einen Aufbautag."""
        for pk, (angelegt, geaendert) in zeiten.items():
            felder = {"created_at": angelegt}
            if geaendert is not None:
                felder["updated_at"] = geaendert
            modell.objects.filter(pk=pk).update(**felder)

    def _alle_dokumenttitel(self) -> set[str]:
        titel = {d.titel for d in EIGENE_DOKUMENTE}
        for vorgang in self.voll.vorgaenge:
            if vorgang.urheber == FRAKTION:
                titel.add(_dokumenttitel(vorgang))
            titel.add(_aenderungstitel(vorgang))
        return titel

    def _alle_aufgabentitel(self) -> set[str]:
        titel = {a[0] for a in AUFGABEN}
        for vorgang in self.voll.vorgaenge:
            titel.update(f"{praefix}: {_thema(vorgang)} (Demo)" for praefix in TOP_AUFGABEN)
        return titel

    # ------------------------------------------------------------------
    # Entfernen
    # ------------------------------------------------------------------

    def _entfernen(self, org: Organization) -> int:
        """
        Den Work-Bestand entfernen: alles, was auf Bestands-Objekte im RIS zeigt (auch von Besuchern angelegt), und
        die Dokumente, Aufgaben, Abwesenheiten und Benachrichtigungen mit Bestandstiteln bzw. Marke.
        """
        top = demo_ext("agendaitem", BESTAND)
        sitzung = demo_ext("meeting", BESTAND)
        papier = demo_ext("paper", BESTAND)
        datei = demo_ext("file", BESTAND)
        anzahl = 0
        for queryset in (
            AgendaItemPosition.objects.filter(organization=org, agenda_item__external_id__startswith=top),
            AgendaItemNote.objects.filter(organization=org, agenda_item__external_id__startswith=top),
            AgendaPrivateNote.objects.filter(organization=org, agenda_item__external_id__startswith=top),
            AgendaSpeechNote.objects.filter(organization=org, agenda_item__external_id__startswith=top),
            AgendaSupplementaryDocument.objects.filter(organization=org, agenda_item__external_id__startswith=top),
            FileAnnotation.objects.filter(organization=org, oparl_file__external_id__startswith=datei),
            PaperComment.objects.filter(organization=org, paper__external_id__startswith=papier),
            MeetingPreparation.objects.filter(organization=org, meeting__external_id__startswith=sitzung),
            Task.objects.filter(organization=org, title__in=self._alle_aufgabentitel()),
            Motion.objects.filter(organization=org, title__in=self._alle_dokumenttitel()),
            MemberAbsence.objects.filter(organization=org, reason__in=[a[3] for a in ABWESENHEITEN]),
            Notification.objects.filter(recipient__organization=org, **{f"metadata__{MARKE}": True}),
        ):
            geloescht, _ = queryset.delete()
            anzahl += geloescht
        return anzahl

    # ------------------------------------------------------------------
    # Mitglieder
    # ------------------------------------------------------------------

    def _mitglieder(self, welt: Welt) -> None:
        org = welt.org
        for person_key, rolle in BASIS_KONTEN.items():
            ms = Membership.objects.filter(organization=org, user__email=DEMO_USERS[rolle]["email"]).first()
            if ms is None:
                raise CommandError(f"Demo-Mitgliedschaft {rolle} fehlt – setup_demo_environment ausführen.")
            welt.m[person_key] = welt.m[rolle] = ms
        gast = Membership.objects.filter(organization=org, user__email=DEMO_USERS["gast"]["email"]).first()
        if gast is not None:
            welt.m["gast"] = gast
        # Die Demo-Nutzerin „sachkundig“ ist Sofia Sachkundig, deren Person im RIS erst der Bestand anlegt
        sofia = ris.persons_by_external_id([demo_ext("person", "sofia-sachkundig")]).first()
        ms_sofia = welt.m.get("sofia-sachkundig")
        if sofia is not None and ms_sofia is not None and ms_sofia.oparl_person_id != sofia.pk:
            ms_sofia.oparl_person = sofia
            ms_sofia.save(update_fields=["oparl_person"])

        rollen = {r.name: r for r in Role.objects.filter(organization=org)}
        personen = [p for p in PERSONEN if p.fraktion == FRAKTION and p.key not in BASIS_KONTEN]
        ris_personen = {
            p.external_id: p for p in ris.persons_by_external_id([demo_ext("person", p.key) for p in personen])
        }
        gremien_keys = {"rat"} | {b.gremium for b in welt.plan.besetzung}
        gremien = {
            o.external_id: o
            for o in ris.organizations_by_external_id([demo_ext("organization", g) for g in gremien_keys])
        }
        neue_einstellungen = []
        for person in personen:
            user, angelegt = User.objects.get_or_create(
                email=person.email,
                defaults={"first_name": person.vorname, "last_name": person.nachname, "email_verified": True},
            )
            if angelegt or user.has_usable_password():
                # Konten des Bestands melden sich nie an (das Passwort der Demo gilt nur für die Demo-Konten)
                user.set_unusable_password()
                user.first_name, user.last_name, user.is_active = person.vorname, person.nachname, True
                user.save(update_fields=["password", "first_name", "last_name", "is_active"])
            ms, _ = Membership.objects.update_or_create(
                user=user,
                organization=org,
                defaults={"is_active": True, "is_guest": False, "is_sworn_in": True},
            )
            if person.key == STELLV_VORSITZ:
                rolle = "Stellv. Vorsitz"
            elif person.rat:
                rolle = "Fraktionsmitglied"
            else:
                rolle = "Sachkundige/r Bürger/in"
            ms.roles.set([rollen[rolle]] if rolle in rollen else [])
            ms.oparl_person = ris_personen.get(demo_ext("person", person.key))
            ms.save(update_fields=["oparl_person"])
            ausschuesse = [b.gremium for b in welt.plan.besetzung if b.person == person.key]
            if person.rat:
                ausschuesse.insert(0, "rat")
            ms.oparl_committees.set(
                [gremien[e] for e in (demo_ext("organization", g) for g in ausschuesse) if e in gremien]
            )
            if not NotificationPreference.objects.filter(membership=ms).exists():
                neue_einstellungen.append(
                    NotificationPreference(
                        membership=ms,
                        email_enabled=False,
                        type_settings={NotificationType.ANNOUNCEMENT.value: {"in_app": False, "email": False}},
                    )
                )
            welt.m[person.key] = ms
        NotificationPreference.objects.bulk_create(neue_einstellungen)
        self._zaehlen("Mitglieder ohne Anmeldung", len(personen))

    def _wer(self, welt: Welt, key: str) -> Membership:
        """Mitgliedschaft zu Personen- oder Rollenschlüssel; Unbekanntes fällt auf den Vorsitz zurück."""
        return welt.m.get(key) or welt.m["vorsitz"]

    def _mitglied_fuer(self, welt: Welt, *teile: object, nur_vereidigt: bool = False) -> Membership:
        """Ein Mitglied der Fraktion, deterministisch aus den Teilen gewählt (Personenschlüssel, nicht Demo-Rollen)."""
        kandidaten = [
            ms for key, ms in sorted(welt.m.items()) if key in PERSON and (ms.is_sworn_in or not nur_vereidigt)
        ]
        return kandidaten[stabil(*teile) % len(kandidaten)]

    # ------------------------------------------------------------------
    # RIS-Objekte des Plans (Insight-Bestand), soweit vorhanden
    # ------------------------------------------------------------------

    def _ris(self, welt: Welt) -> None:
        plan = welt.plan
        papiere = {demo_ext("paper", v.key): v.key for v in plan.vorgaenge}
        for papier in ris.papers_by_external_id(papiere):
            welt.papiere[papiere[papier.external_id]] = papier
        sitzungen = {demo_ext("meeting", s.key): s.key for s in plan.sitzungen}
        for sitzung in ris.meetings_by_external_id(sitzungen):
            welt.sitzungen[sitzungen[sitzung.external_id]] = sitzung
        tops = {demo_ext("agendaitem", top_kennung(s, t)): top_kennung(s, t) for s in plan.sitzungen for t in s.tops}
        for top in ris.agenda_items_by_external_id(tops):
            welt.tops[tops[top.external_id]] = top
        self._zaehlen("RIS-Verknüpfungen gefunden (Vorlagen/Sitzungen/TOPs)", len(welt.papiere) + len(welt.sitzungen))

    # ------------------------------------------------------------------
    # Dokumenttypen, Ordner, Labels
    # ------------------------------------------------------------------

    def _typen_und_ordner(self, welt: Welt) -> None:
        org = welt.org
        for reihenfolge, (slug, name, icon, farbe, freigabe, einreichbar, checkliste) in enumerate(DOKUMENTTYPEN):
            welt.typen[slug], _ = MotionType.objects.update_or_create(
                organization=org,
                slug=slug,
                defaults={
                    "name": name,
                    "icon": icon,
                    "color": farbe,
                    "requires_approval": freigabe,
                    "is_submittable": einreichbar,
                    "default_checklist": checkliste,
                    "is_active": True,
                    "sort_order": reihenfolge,
                },
            )
        for position, (key, name, farbe, eltern) in enumerate(ORDNER):
            welt.ordner[key], _ = DocumentFolder.objects.update_or_create(
                organization=org,
                parent=welt.ordner[eltern] if eltern else None,
                name=name,
                defaults={"color": farbe, "position": position, "created_by": welt.m["vorsitz"]},
            )
        welt.ordner["noe"] = DocumentFolder.non_public_folder(org, created_by=welt.m["vorsitz"])
        for name, farbe in LABELS:
            TaskLabel.objects.update_or_create(organization=org, name=name, defaults={"color": farbe})
        self._zaehlen("Dokumenttypen", len(DOKUMENTTYPEN))
        self._zaehlen("Ordner", len(ORDNER))

    # ------------------------------------------------------------------
    # Dokumente
    # ------------------------------------------------------------------

    def _beratungen(self, plan: Stadtplan, vorgang: Vorgang) -> list[tuple[Sitzung, Top]]:
        return sorted(
            ((s, t) for s in plan.sitzungen for t in s.tops if t.vorgang == vorgang.key), key=lambda st: st[0].tag
        )

    def _stand(self, plan: Stadtplan, vorgang: Vorgang) -> tuple[str, Sitzung | None]:
        """Work-Status des Fraktionsdokuments aus dem Stand der Beratung im Plan."""
        if vorgang.tag > 0:
            return ("approved" if vorgang.tag <= 14 else "internal_review"), None
        beratungen = self._beratungen(plan, vorgang)
        if not beratungen:
            return "at_admin", None
        kommend = [st for st in beratungen if st[0].tag > 0]
        if kommend:
            return "on_agenda", kommend[0][0]
        sitzung, top = beratungen[-1]
        if top.rolle == "Vorberatung":
            return "at_admin", sitzung
        stand = {"approved": "adopted", "rejected": "rejected", "withdrawn": "withdrawn", "noted": "completed"}
        return stand.get(top.ergebnis, "on_agenda"), sitzung

    def _katalogdokumente(self, welt: Welt) -> None:
        """Anträge und Anfragen der Bunten Liste aus dem Plan, Stand passend zur Beratung."""
        org, plan = welt.org, welt.plan
        neue: list[Motion] = []
        zeiten: dict[Any, tuple[datetime, datetime | None]] = {}
        for vorgang in plan.vorgaenge:
            if vorgang.urheber != FRAKTION:
                continue
            status, sitzung = self._stand(plan, vorgang)
            antrag = vorgang.art == "antrag"
            if antrag:
                begruendung = [z.removeprefix("Begründung: ") for z in vorgang.sachverhalt]
                abschnitte = [("Beschlussvorschlag", [vorgang.beschluss]), ("Begründung", begruendung)]
            else:
                abschnitte = [("", list(vorgang.sachverhalt))]
            autor = self._mitglied_fuer(welt, "autor", vorgang.key)
            eingereicht = vorgang.tag <= 0
            motion = Motion(
                organization=org,
                title=_dokumenttitel(vorgang),
                motion_type="motion" if antrag else "inquiry",
                document_type=welt.typen["antrag" if antrag else "anfrage"],
                status=status,
                visibility="organization",
                author=autor,
                responsible=autor,
                summary=f"{vorgang.oparl_art} {vorgang.nummer}: {_thema(vorgang)}" if eingereicht else _thema(vorgang),
                submitted_at=self._zeit(vorgang.tag, 9) if eingereicht else None,
                related_paper=welt.papiere.get(vorgang.key) if eingereicht else None,
                related_meeting=welt.sitzungen.get(sitzung.key) if sitzung else None,
                folder=self._ordner_fuer(welt, antrag, status),
                due_date=self._datum(vorgang.tag) if not eingereicht else None,
            )
            cast(Any, motion).set_content_encrypted(_html(_dokumenttitel(vorgang).removesuffix(" (Demo)"), abschnitte))
            neue.append(motion)
        Motion.objects.bulk_create(neue)
        for motion, vorgang in zip(neue, [v for v in plan.vorgaenge if v.urheber == FRAKTION], strict=True):
            welt.dokumente[vorgang.key] = motion
            zeiten[motion.pk] = (self._zeit(vorgang.tag - 14), self._zeit(min(vorgang.tag + 30, 0), 16))
            self._dokumentdetails(welt, motion, vorgang.key, kommentare=())
        self._zurueckdatieren(Motion, zeiten)
        self._zaehlen("Anträge und Anfragen aus dem RIS-Bestand", len(neue))

    @staticmethod
    def _ordner_fuer(welt: Welt, antrag: bool, status: str) -> DocumentFolder:
        if not antrag:
            return welt.ordner["anfragen"]
        if status == "adopted":
            return welt.ordner["beschlossen"]
        if status in ("rejected", "withdrawn"):
            return welt.ordner["erledigt"]
        return welt.ordner["antraege"]

    def _dokumentdetails(
        self,
        welt: Welt,
        motion: Motion,
        schluessel: str,
        *,
        kommentare: tuple[tuple[str, str, bool], ...],
        mitarbeit: tuple[str, ...] = (),
        fassungen: int = 0,
        gast: bool = False,
        freigabe_offen: bool = False,
    ) -> None:
        """Checkliste nach Dokumenttyp, Kommentare, Fassungen, Mitarbeit, Gastfreigabe und Freigabeanfrage."""
        typ = motion.document_type
        offen = motion.status in Motion.EDITABLE_STATUSES
        if typ is not None and typ.default_checklist:
            MotionChecklistItem.objects.bulk_create(
                MotionChecklistItem(
                    motion=motion,
                    title=titel,
                    position=i,
                    is_completed=not offen or i < stabil("checkliste", schluessel) % (len(typ.default_checklist) + 1),
                    completed_by=motion.author if not offen else None,
                )
                for i, titel in enumerate(typ.default_checklist)
            )
        if not kommentare and stabil("kommentar", schluessel) % 3 == 0:
            kommentare = (
                ("mitglied", "Danke, aus meiner Sicht kann das so raus.", True),
                ("sachkundig", "Im Ausschuss wurde nach den Folgekosten gefragt – bitte ergänzen.", False),
            )
        jetzt = timezone.now()
        for wer, text, erledigt in kommentare:
            MotionComment.objects.create(
                motion=motion,
                content=text,
                author=self._wer(welt, wer),
                is_resolved=erledigt,
                resolved_by=welt.m["vorsitz"] if erledigt else None,
                resolved_at=jetzt if erledigt else None,
            )
        self._zaehlen("Kommentare an Dokumenten", len(kommentare))
        inhalt = cast(Any, motion).get_content_decrypted() or ""
        for version in range(1, fassungen + 1):
            revision = MotionRevision(
                motion=motion,
                version=version,
                changed_by=motion.author,
                change_summary=("Erster Entwurf", "Anmerkungen der Fraktion eingearbeitet", "Endfassung")[
                    min(version - 1, 2)
                ],
            )
            cast(Any, revision).set_content_encrypted(inhalt)
            revision.save()
        if mitarbeit:
            motion.contributors.set([self._wer(welt, k) for k in mitarbeit])
        if gast and "gast" in welt.m and motion.folder is not None and not motion.folder.is_sworn_in_only():
            MotionShare.objects.create(
                motion=motion,
                scope="user",
                level="view",
                user=welt.m["gast"].user,
                created_by=welt.m["vorsitz"].user,
                message="Zur Information (Demo).",
            )
            self._zaehlen("Freigaben an den Gast")
        if freigabe_offen:
            MotionApproval.objects.create(motion=motion, approver=welt.m["vorsitz"], approval_type="chair")
            self._zaehlen("Offene Freigabeanfragen")

    def _eigene_dokumente(self, welt: Welt) -> None:
        org = welt.org
        neue: list[Motion] = []
        for dok in EIGENE_DOKUMENTE:
            typ = welt.typen[dok.typ]
            motion = Motion(
                organization=org,
                title=dok.titel,
                motion_type={"anfrage": "inquiry", "aenderungsantrag": "amendment"}.get(
                    dok.typ, "motion" if typ.is_submittable else "statement"
                ),
                document_type=typ,
                status=dok.status,
                visibility="organization",
                author=self._wer(welt, dok.autor),
                responsible=self._wer(welt, dok.autor),
                summary=dok.zusammenfassung,
                folder=welt.ordner[dok.ordner],
                due_date=self._datum(dok.frist) if dok.frist is not None else None,
                submitted_at=self._zeit(dok.tag + 7) if dok.status in ("submitted", "adopted", "rejected") else None,
            )
            cast(Any, motion).set_content_encrypted(
                _html(dok.titel.removesuffix(" (Demo)"), [("", list(dok.absaetze))])
            )
            neue.append(motion)

        # Änderungsanträge zu kommenden Vorlagen der Verwaltung (Position „Mit Änderungsantrag“)
        aenderungen = [v for v in self._kommende_vorlagen(welt) if self._position(v)[0] == "amended"][:6]
        for vorgang in aenderungen:
            motion = Motion(
                organization=org,
                title=_aenderungstitel(vorgang),
                motion_type="amendment",
                document_type=welt.typen["aenderungsantrag"],
                status="internal_review" if stabil("aenderung", vorgang.key) % 2 else "draft",
                visibility="organization",
                author=self._mitglied_fuer(welt, "aenderung", vorgang.key),
                summary=f"Änderungsantrag zur Vorlage {vorgang.nummer}.",
                parent_paper=welt.papiere.get(vorgang.key),
                folder=welt.ordner["antraege"],
                due_date=self._datum(max(min(vorgang.tag + 20, 30), 2)),
            )
            cast(Any, motion).set_content_encrypted(
                _html(
                    f"Änderungsantrag zu „{_thema(vorgang)}“",
                    [
                        ("Änderung", [f"Der Beschlussvorschlag wird ergänzt: {self._ergaenzung(vorgang)}"]),
                        ("Begründung", ["Die Vorlage greift zu kurz; mit der Ergänzung kann die Fraktion zustimmen."]),
                    ],
                )
            )
            neue.append(motion)
        Motion.objects.bulk_create(neue)

        zeiten: dict[Any, tuple[datetime, datetime | None]] = {}
        for motion, dok in zip(neue, EIGENE_DOKUMENTE, strict=False):
            zeiten[motion.pk] = (self._zeit(dok.tag), self._zeit(min(dok.tag + 3, 0), 15))
            self._dokumentdetails(
                welt,
                motion,
                dok.titel,
                kommentare=dok.kommentare,
                mitarbeit=dok.mitarbeit,
                fassungen=dok.fassungen,
                gast=dok.gast,
                freigabe_offen=dok.freigabe_offen,
            )
        for motion, vorgang in zip(neue[len(EIGENE_DOKUMENTE) :], aenderungen, strict=True):
            zeiten[motion.pk] = (self._zeit(-4), self._zeit(-1, 21))
            self._dokumentdetails(welt, motion, vorgang.key, kommentare=(), mitarbeit=("vorsitz",))
            welt.dokumente[f"aenderung:{vorgang.key}"] = motion
        self._zurueckdatieren(Motion, zeiten)
        self._zaehlen("Eigene Dokumente (Presse, Reden, Haushalt, Strategie, AK, Vermerke)", len(EIGENE_DOKUMENTE))
        self._zaehlen("Änderungsanträge zu kommenden Vorlagen", len(aenderungen))

    @staticmethod
    def _ergaenzung(vorgang: Vorgang) -> str:
        vorschlaege = (
            "Die Umsetzung wird dem Fachausschuss halbjährlich berichtet.",
            "Die Bürgerinnen und Bürger werden vor der Ausführungsplanung beteiligt.",
            "Die Maßnahme wird barrierefrei geplant.",
            "Die Klimafolgen werden vor dem Baubeschluss dargestellt.",
            "Die Folgekosten werden im nächsten Haushalt gesondert ausgewiesen.",
        )
        return vorschlaege[stabil("ergaenzung", vorgang.key) % len(vorschlaege)]

    # ------------------------------------------------------------------
    # Sitzungsvorbereitung
    # ------------------------------------------------------------------

    def _kommende_vorlagen(self, welt: Welt) -> list[Vorgang]:
        """Vorlagen der Verwaltung auf kommenden Tagesordnungen, nächste zuerst (ohne Doppelte)."""
        gesehen: dict[str, Vorgang] = {}
        for sitzung in sorted(welt.plan.sitzungen, key=lambda s: s.tag):
            if sitzung.tag <= 0:
                continue
            for top in sitzung.tops:
                vorgang = welt.plan.vorgang.get(top.vorgang or "")
                if vorgang and vorgang.art in ("vorlage", "satzung") and vorgang.key not in gesehen:
                    gesehen[vorgang.key] = vorgang
        return list(gesehen.values())

    @staticmethod
    def _position(vorgang: Vorgang) -> tuple[str, str]:
        """Position der Fraktion zu einem Vorgang und Begründung (deterministisch)."""
        thema = _thema(vorgang)
        if vorgang.art in ("anfrage", "mitteilung"):
            return "info", f"Kenntnisnahme; Nachfragen zu „{thema}“ im Ausschuss stellen."
        if vorgang.urheber == FRAKTION:
            return "for", "Eigener Antrag – geschlossene Zustimmung der Fraktion."
        zufall = stabil("position", vorgang.key) % 100
        if vorgang.von_fraktion:
            if vorgang.ergebnis == "rejected":
                return "against", "Ablehnung: Der Vorschlag ist nicht gegenfinanziert und sozial unausgewogen."
            if zufall < 60:
                return "for", "Zustimmung: Das Anliegen teilen wir, auch wenn der Antrag nicht von uns kommt."
            if zufall < 80:
                return "abstain", "Enthaltung: Wir wollen erst die Stellungnahme der Verwaltung sehen."
            return "amended", "Zustimmung nur mit Ergänzung: Bericht an den Fachausschuss."
        gruende_fuer = (
            "Die Vorlage entspricht unserem Programm.",
            "Sinnvolle Investition, die Fördermittel sichert.",
            "Die Verwaltung hat unsere Hinweise aus dem Ausschuss aufgenommen.",
            "Notwendig und gut begründet; Kosten im Rahmen.",
        )
        if zufall < 68:
            return "for", "Zustimmung: " + gruende_fuer[zufall % len(gruende_fuer)]
        if zufall < 84:
            return "amended", "Zustimmung nur mit unserem Änderungsantrag."
        if zufall < 92:
            return "abstain", "Enthaltung: Offene Fragen zu Folgekosten und Zeitplan."
        if zufall < 96:
            return "refer", "In den Fachausschuss verweisen, dort fehlt die Vorberatung."
        return "against", "Ablehnung: Die Kosten stehen in keinem Verhältnis zum Nutzen."

    @staticmethod
    def _ausgang(position: str, ergebnis: str) -> str:
        if ergebnis in ("approved", "recommended"):
            return "as_position" if position in ("for", "amended") else "accepted"
        return {"rejected": "rejected", "deferred": "deferred", "noted": "noted"}.get(ergebnis, "")

    def _vorbereitung(self, welt: Welt) -> None:
        org, plan = welt.org, welt.plan
        heute = 0
        kommend = sorted((s for s in plan.sitzungen if s.tag > heute), key=lambda s: s.tag)
        vergangen = sorted((s for s in plan.sitzungen if s.tag < heute), key=lambda s: -s.tag)
        # Vorbereitet werden die nächsten Sitzungen und die jüngst vergangenen; weiter entfernte bleiben offen
        auswahl = kommend[:10] + vergangen[:10]
        bestehend = {
            p.meeting_id: p
            for p in MeetingPreparation.objects.filter(
                organization=org, meeting__in=[welt.sitzungen[s.key] for s in auswahl if s.key in welt.sitzungen]
            )
        }
        positionen: list[AgendaItemPosition] = []
        notizen: list[AgendaItemNote] = []
        anzahl_vorbereitungen = 0
        for rang, sitzung in enumerate(auswahl):
            ris_sitzung = welt.sitzungen.get(sitzung.key)
            if ris_sitzung is None:
                continue
            vergangen_ = sitzung.tag < 0
            vorbereitung = bestehend.get(ris_sitzung.pk)
            if vorbereitung is None:
                if sitzung.basis:
                    continue  # Basissitzungen ohne Vorbereitung bleiben, wie sie sind
                wer = self._mitglied_fuer(welt, "vorbereitung", sitzung.key, nur_vereidigt=True)
                fertig = vergangen_ or rang < 3
                vorbereitung = MeetingPreparation(
                    organization=org,
                    meeting=ris_sitzung,
                    membership=wer,
                    is_prepared=fertig,
                    prepared_at=self._zeit(sitzung.tag - 2, 20) if fertig else None,
                    prepared_by=wer if fertig else None,
                )
                cast(Any, vorbereitung).set_notes_encrypted(
                    "Schwerpunkte vorab in der Fraktionssitzung besprechen; Redebeiträge verteilen."
                    if not vergangen_
                    else "Sitzung nachbereitet, Ergebnisse im Protokoll der Fraktionssitzung."
                )
                vorbereitung.save()
                anzahl_vorbereitungen += 1
            for top in sitzung.tops:
                ris_top = welt.tops.get(top_kennung(sitzung, top))
                vorgang = plan.vorgang.get(top.vorgang or "")
                if ris_top is None or vorgang is None or not top.oeffentlich:
                    continue
                position, begruendung = self._position(vorgang)
                offen = not vergangen_ and rang >= 6 and stabil("offen", top.vorgang) % 2 == 0
                pos = AgendaItemPosition(
                    preparation=vorbereitung,
                    organization=org,
                    agenda_item=ris_top,
                    position="open" if offen else position,
                    is_final=vergangen_ or (not offen and stabil("final", top.vorgang) % 4 != 0),
                    outcome=self._ausgang(position, top.ergebnis) if vergangen_ else "",
                    set_by=vorbereitung.membership,
                )
                if not offen:
                    cast(Any, pos).set_reasoning_encrypted(begruendung)
                positionen.append(pos)
                if not vergangen_ and stabil("notiz", sitzung.key, top.vorgang) % 4 == 0:
                    notiz = AgendaItemNote(
                        organization=org,
                        agenda_item=ris_top,
                        visibility="organization",
                        author=self._mitglied_fuer(welt, "notizautor", top.vorgang),
                        is_pinned=stabil("angeheftet", top.vorgang) % 3 == 0,
                    )
                    cast(Any, notiz).set_content_encrypted(self._notiztext(vorgang))
                    notizen.append(notiz)
        AgendaItemPosition.objects.bulk_create(positionen)
        AgendaItemNote.objects.bulk_create(notizen)
        self._zaehlen("Sitzungsvorbereitungen (neu)", anzahl_vorbereitungen)
        self._zaehlen("Positionen zu Tagesordnungspunkten", len(positionen))
        self._zaehlen("Notizen zu Tagesordnungspunkten", len(notizen))
        self._persoenliches(welt, kommend[:6])
        self._vorlagenkommentare(welt)

    @staticmethod
    def _notiztext(vorgang: Vorgang) -> str:
        texte = (
            "Redebeitrag übernimmt die Sprecherin des Arbeitskreises.",
            "Vorab mit der Verwaltung klären, ob Fördermittel bewilligt sind.",
            "Bürgerinitiative hat sich gemeldet – Schreiben liegt im Ordner.",
            "Abstimmung mit der Fraktion Zukunft läuft, Ergebnis in der Fraktionssitzung.",
            "Kostenschätzung erscheint niedrig, im Ausschuss nachfragen.",
        )
        return texte[stabil("notiztext", vorgang.key) % len(texte)]

    def _persoenliches(self, welt: Welt, sitzungen: list[Sitzung]) -> None:
        """Private Notizen und Redebeiträge der Demo-Konten zu den nächsten Sitzungen."""
        privat: list[AgendaPrivateNote] = []
        reden: list[AgendaSpeechNote] = []
        belegt: set[tuple[Any, Any]] = set()
        for sitzung in sitzungen:
            for top in sitzung.tops:
                ris_top = welt.tops.get(top_kennung(sitzung, top))
                vorgang = welt.plan.vorgang.get(top.vorgang or "")
                if ris_top is None or vorgang is None:
                    continue
                if stabil("privat", vorgang.key) % 3 == 0 and (welt.m["vorsitz"].pk, ris_top.pk) not in belegt:
                    notiz = AgendaPrivateNote(organization=welt.org, author=welt.m["vorsitz"], agenda_item=ris_top)
                    cast(Any, notiz).set_content_encrypted(f"Persönlich: Zu „{_thema(vorgang)}“ kurz melden.")
                    privat.append(notiz)
                    belegt.add((welt.m["vorsitz"].pk, ris_top.pk))
                eigenes = welt.dokumente.get(vorgang.key)
                if eigenes is not None or stabil("rede", vorgang.key) % 5 == 0:
                    redner = welt.m["mitglied"] if eigenes is None else welt.m["vorsitz"]
                    if (redner.pk, ris_top.pk) in belegt:
                        continue
                    rede = AgendaSpeechNote(
                        organization=welt.org,
                        author=redner,
                        meeting=welt.sitzungen.get(sitzung.key),
                        agenda_item=ris_top,
                        title=f"Redebeitrag: {_thema(vorgang)}",
                        estimated_duration=180,
                        linked_document=eigenes,
                        is_shared=True,
                    )
                    cast(Any, rede).set_content_encrypted(
                        f"Sehr geehrte Frau Bürgermeisterin, liebe Kolleginnen und Kollegen, zu „{_thema(vorgang)}“ "
                        "drei Punkte: Was wir wollen, warum es jetzt nötig ist und was es kostet."
                    )
                    reden.append(rede)
                    belegt.add((redner.pk, ris_top.pk))
        AgendaPrivateNote.objects.bulk_create(privat)
        AgendaSpeechNote.objects.bulk_create(reden)
        self._zaehlen("Private Notizen", len(privat))
        self._zaehlen("Redebeiträge", len(reden))

    def _vorlagenkommentare(self, welt: Welt) -> None:
        kommentare: list[PaperComment] = []
        texte = (
            ("Folgekosten fehlen in der Vorlage – im Ausschuss nachfragen.", False),
            ("Empfehlung des AK: zustimmen.", True),
            ("Vergleich mit der Lösung in der Nachbarstadt wäre hilfreich.", False),
            ("Empfehlung des AK: nur mit Änderungsantrag zustimmen.", True),
        )
        for i, vorgang in enumerate(self._kommende_vorlagen(welt)[:12]):
            papier = welt.papiere.get(vorgang.key)
            if papier is None:
                continue
            text, empfehlung = texte[i % len(texte)]
            kommentar = PaperComment(
                paper=papier,
                organization=welt.org,
                author=self._mitglied_fuer(welt, "papierkommentar", vorgang.key),
                visibility="organization",
                is_recommendation=empfehlung,
            )
            cast(Any, kommentar).set_content_encrypted(text)
            kommentare.append(kommentar)
        PaperComment.objects.bulk_create(kommentare)
        self._zaehlen("Kommentare zu Vorlagen", len(kommentare))

    # ------------------------------------------------------------------
    # Aufgaben
    # ------------------------------------------------------------------

    def _aufgaben(self, welt: Welt) -> None:
        org = welt.org
        labels = {label.name: label for label in TaskLabel.objects.filter(organization=org)}
        eintraege: list[tuple[Task, tuple[str, ...], tuple[str, ...], tuple[tuple[str, str], ...], int]] = []
        for titel, beschreibung, status, prioritaet, wer, faellig, label, checkliste, kommentare in AUFGABEN:
            erledigt = status == "done"
            eintraege.append(
                (
                    Task(
                        organization=org,
                        title=titel,
                        description=beschreibung,
                        visibility="organization",
                        status=status,
                        priority=prioritaet,
                        is_completed=erledigt,
                        completed_at=self._zeit(faellig if faellig is not None else -1, 17) if erledigt else None,
                        due_date=self._datum(faellig) if faellig is not None else None,
                        created_by=welt.m["vorsitz"],
                        assigned_to=self._wer(welt, wer),
                    ),
                    label,
                    checkliste,
                    kommentare,
                    (faellig or 0) - 14,
                )
            )
        # Aufgaben zu den nächsten Tagesordnungspunkten: Redebeiträge zu eigenen Anträgen, Positionen zu Vorlagen
        kommende = sorted((s for s in welt.plan.sitzungen if s.tag > 0), key=lambda s: s.tag)[:8]
        for sitzung in kommende:
            for top in sitzung.tops:
                vorgang = welt.plan.vorgang.get(top.vorgang or "")
                if vorgang is None or vorgang.art in ("anfrage", "mitteilung"):
                    continue
                eigen = vorgang.urheber == FRAKTION
                if not eigen and stabil("aufgabe", vorgang.key) % 3:
                    continue
                titel = f"{TOP_AUFGABEN[0] if eigen else TOP_AUFGABEN[1]}: {_thema(vorgang)} (Demo)"
                if any(t.title == titel for t, *_ in eintraege):
                    continue
                eintraege.append(
                    (
                        Task(
                            organization=org,
                            title=titel,
                            description=f"Für die Sitzung am {self._datum(sitzung.tag):%d.%m.%Y}.",
                            visibility="organization",
                            status="in_progress" if eigen else "todo",
                            priority="high" if sitzung.tag <= 14 else "medium",
                            due_date=self._datum(max(sitzung.tag - 2, 0)),
                            created_by=welt.m["vorsitz"],
                            assigned_to=self._mitglied_fuer(welt, "aufgabe", vorgang.key),
                            related_meeting=welt.sitzungen.get(sitzung.key),
                            related_agenda_item=welt.tops.get(top_kennung(sitzung, top)),
                            related_motion=welt.dokumente.get(vorgang.key),
                        ),
                        ("Ratssitzung",) if sitzung.gremium == "rat" else ("Ausschuss",),
                        ("Unterlagen lesen", "Mit dem AK abstimmen") if not eigen else ("Redetext", "Zahlen prüfen"),
                        (),
                        -7,
                    )
                )
        aufgaben = Task.objects.bulk_create([e[0] for e in eintraege])
        listen, kommentare_, aktivitaeten = [], [], []
        zeiten: dict[Any, tuple[datetime, datetime | None]] = {}
        for aufgabe, (_t, label, checkliste, kommentare, angelegt) in zip(aufgaben, eintraege, strict=True):
            aufgabe.labels.set([labels[name] for name in label if name in labels])
            for i, punkt in enumerate(checkliste):
                listen.append(
                    TaskChecklistItem(
                        task=aufgabe,
                        title=punkt,
                        position=i,
                        is_completed=aufgabe.is_completed or (aufgabe.status == "in_progress" and i == 0),
                    )
                )
            for wer, text in kommentare:
                kommentare_.append(TaskComment(task=aufgabe, content=text, author=self._wer(welt, wer)))
            aktivitaeten.append(TaskActivity(task=aufgabe, actor=welt.m["vorsitz"], activity_type="created"))
            if aufgabe.is_completed:
                aktivitaeten.append(TaskActivity(task=aufgabe, actor=aufgabe.assigned_to, activity_type="completed"))
            zeiten[aufgabe.pk] = (self._zeit(min(angelegt, -1), 9), None)
        TaskChecklistItem.objects.bulk_create(listen)
        TaskComment.objects.bulk_create(kommentare_)
        TaskActivity.objects.bulk_create(aktivitaeten)
        self._zurueckdatieren(Task, zeiten)
        self._zaehlen("Aufgaben", len(aufgaben))
        self._zaehlen("Checklistenpunkte an Aufgaben", len(listen))
        self._zaehlen("Kommentare an Aufgaben", len(kommentare_))
        welt.aufgaben = aufgaben

    # ------------------------------------------------------------------
    # Abwesenheiten, Benachrichtigungen
    # ------------------------------------------------------------------

    def _abwesenheiten(self, welt: Welt) -> None:
        neue = [
            MemberAbsence(
                organization=welt.org,
                membership=welt.m[wer],
                start_date=self._datum(von),
                end_date=self._datum(bis),
                reason=grund,
                deputy=welt.m.get(vertretung),
                auto_decline_meetings=False,
                notify_deputy=False,
                is_active=True,
            )
            for wer, von, bis, grund, vertretung in ABWESENHEITEN
            if wer in welt.m
        ]
        MemberAbsence.objects.bulk_create(neue)
        self._zaehlen("Abwesenheiten", len(neue))

    def _benachrichtigungen(self, welt: Welt) -> None:
        org = welt.org
        slug = org.slug
        aufgaben = welt.aufgaben
        neue: list[tuple[Notification, int]] = []

        def neu(empfaenger: str, art: str, titel: str, text: str, link: str, tag: int, gelesen: bool) -> None:
            ms = welt.m.get(empfaenger)
            if ms is None:
                return
            neue.append(
                (
                    Notification(
                        recipient=ms,
                        notification_type=art,
                        title=titel,
                        message=text,
                        link=link,
                        actor=welt.m["vorsitz"] if empfaenger != "vorsitz" else welt.m.get("mitglied"),
                        is_read=gelesen,
                        read_at=self._zeit(tag, 18) if gelesen else None,
                        metadata={MARKE: True},
                    ),
                    tag,
                )
            )

        for empfaenger in ("vorsitz", "mitglied", "sachkundig", "unvereidigt"):
            eigene = [a for a in aufgaben if a.assigned_to_id == welt.m[empfaenger].pk and not a.is_completed][:2]
            for i, aufgabe in enumerate(eigene):
                neu(
                    empfaenger,
                    NotificationType.TASK_ASSIGNED,
                    "Neue Aufgabe zugewiesen",
                    f"Ihnen wurde die Aufgabe „{aufgabe.title}“ zugewiesen.",
                    f"/work/{slug}/tasks/?open={aufgabe.id}",
                    -1 - i,
                    gelesen=i > 0,
                )
        dokumente = [m for m in welt.dokumente.values() if m.status in ("adopted", "on_agenda")]
        for i, motion in enumerate(dokumente[:3]):
            neu(
                "vorsitz" if i % 2 == 0 else "mitglied",
                NotificationType.MOTION_STATUS,
                "Antragsstatus geändert",
                f"„{motion.title}“: {motion.get_status_display()}.",
                f"/work/{slug}/documents/{motion.id}/",
                -3 - i * 4,
                gelesen=i > 0,
            )
        kommentiert = Motion.objects.filter(
            organization=org, title__in=[d.titel for d in EIGENE_DOKUMENTE if d.kommentare]
        )
        for i, motion in enumerate(kommentiert.order_by("title")[:3]):
            neu(
                "vorsitz",
                NotificationType.MOTION_COMMENT,
                "Neuer Kommentar",
                f"Neuer Kommentar zu „{motion.title}“.",
                f"/work/{slug}/documents/{motion.id}/",
                -i,
                gelesen=i == 2,
            )
        kommende = sorted(
            (s for s in welt.plan.sitzungen if s.tag > 0 and s.key in welt.sitzungen), key=lambda s: s.tag
        )
        for empfaenger, sitzung in zip(("vorsitz", "mitglied", "sachkundig"), kommende[:3], strict=False):
            ris_sitzung = welt.sitzungen[sitzung.key]
            neu(
                empfaenger,
                NotificationType.MEETING_REMINDER,
                "Sitzung in Kürze",
                f"{ris_sitzung.name} am {self._datum(sitzung.tag):%d.%m.%Y}.",
                f"/insight/termine/{ris_sitzung.id}/",
                0,
                gelesen=False,
            )
        Notification.objects.bulk_create([n for n, _ in neue])
        self._zurueckdatieren(Notification, {n.pk: (self._zeit(tag, 8), None) for n, tag in neue})
        self._zaehlen("Benachrichtigungen", len(neue))
