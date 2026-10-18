# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Demo-Inhalte für das neue Design in Work (Issue #884).

Ergänzt die Musterfraktion der Demo-Umgebung so, dass das neue Design jeden Punkt des Work-Updates zeigen kann.
Wird von ``setup_demo_environment`` am Ende des Work-Teils aufgerufen (auch im nächtlichen Neuaufbau der öffentlichen
Demo) und arbeitet ausschließlich in der Demo-Organisation (``DEMO_ORG_SLUG``):

- Standard-Tagesordnung (Beschlüsse, Politische Arbeit, Termine, Presse und Social Media, Sonstiges, ein
  nicht-öffentlicher Punkt)
- vergangene Fraktionssitzung mit Anwesenheit (vor Ort und online), Protokolleinträgen, Beschluss und Aufgabe;
  ihr Protokoll wartet auf die Genehmigung in der nächsten Sitzung
- kommende Fraktionssitzung mit Ort und Videolink: Genehmigungs-TOP, Standard-Tagesordnung, Unterpunkte mit
  verknüpftem Antrag und verknüpfter Vorlage, nicht-öffentliche TOPs und ein TOP-Vorschlag der Sachkundigen
- Sitzungsreihe (wöchentlich montags 18 Uhr, Zu- und Absagen und automatische Einladung aus) mit Ferienpause;
  die Termine erzeugt dieselbe Funktion wie im Betrieb
- Positionen mit Beratungsverlauf: Dieselbe Vorlage steht im Hauptausschuss und im Rat; die Position aus dem
  Hauptausschuss („Mit Änderungsantrag“) erscheint in der Vorbereitung der Ratssitzung. Dazu vergangene
  Positionen mit Ergebnis
- Antrag mit Kommentaren an Textstellen (offen, beantwortet, erledigt) und zwei Änderungsanträgen
- gibt am Ende den Stand des Schalters aus; eingeschaltet ist das neue Design für die Demo schon in
  ``setup_demo_environment`` (#852)

Idempotent wie die Basisdemo: feste Titel sind natürliche Schlüssel, ein zweiter Lauf legt nichts doppelt an.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from typing import TYPE_CHECKING, Any, cast

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction
from django.utils import timezone

from apps.common.demo_daten import (
    DEMO_ANTRAG_SITZBAENKE,
    DEMO_ANTRAG_TRINKBRUNNEN,
    DEMO_ORG_SLUG,
    DEMO_USERS,
    demo_ext,
    ist_sitzungstag,
    termin_am_sitzungstag,
)
from apps.tenants.models import Membership, Organization
from apps.work.faction import agenda
from apps.work.faction.generation import generate_meetings_for_schedule
from apps.work.faction.models import (
    FactionAgendaItem,
    FactionAttendance,
    FactionDecision,
    FactionMeeting,
    FactionMeetingException,
    FactionMeetingSchedule,
    FactionProtocolEntry,
    FactionStandardAgendaItem,
)
from apps.work.meetings.models import AgendaItemPosition, MeetingPreparation
from apps.work.motions.models import Motion, MotionComment
from apps.work.rahmen import neues_design
from hub.ris import selectors as ris

if TYPE_CHECKING:
    from insight_core.models import OParlAgendaItem, OParlMeeting, OParlPaper

#: Work-Konten der Demo (Schlüssel aus ``DEMO_USERS``) ohne den Gast
MITGLIEDER = ("vorsitz", "mitglied", "sachkundig", "unvereidigt")

ORT = "Fraktionsbüro, Rathausplatz 1, Musterstadt"
VIDEOLINK = "https://video.demo.mandari.invalid/musterfraktion"

STANDARD_TAGESORDNUNG = [
    ("Beschlüsse", "public"),
    ("Politische Arbeit", "public"),
    ("Termine", "public"),
    ("Presse und Social Media", "public"),
    ("Sonstiges", "public"),
    ("Personal- und Finanzangelegenheiten der Fraktion", "internal"),
]

VERGANGENE_SITZUNG = "Fraktionssitzung (Demo, vergangen)"
KOMMENDE_SITZUNG = "Fraktionssitzung zur Vorbereitung der Ratssitzung (Demo)"
REIHE = "Wöchentliche Fraktionssitzung (Demo)"
PAUSE = "Herbstferien (Demo)"
#: So weit voraus trägt die Demo Feiertage als Ausnahme der Reihe ein (mehr als der Horizont der Erzeugung)
HORIZONT_TAGE = 120

ANTRAG_ENTWURF = DEMO_ANTRAG_SITZBAENKE
ANTRAG_EINGEREICHT = DEMO_ANTRAG_TRINKBRUNNEN
AENDERUNG_ANTRAG = "Änderungsantrag: Trinkwasserbrunnen auch am Spielplatz Stadtpark (Demo)"
AENDERUNG_VORLAGE = "Änderungsantrag zum Feuerwehrbedarfsplan: Gerätehaus Nord vorziehen (Demo)"


def _marke(name: str) -> uuid.UUID:
    """Feste Kennung einer Kommentarmarke im Antragstext (Text und Kommentar müssen dieselbe tragen)."""
    return uuid.uuid5(uuid.NAMESPACE_URL, demo_ext("work/kommentar", name))


MARKE_BAENKE = _marke("baenke")
MARKE_KOSTEN = _marke("kosten")

ENTWURF_INHALT = (
    "<h2>Antrag: Mehr Sitzgelegenheiten in der Innenstadt</h2>"
    "<p>Die Verwaltung wird beauftragt, entlang der Fußgängerzone "
    f'<span data-comment-id="{MARKE_BAENKE}">zehn zusätzliche Sitzbänke</span> aufzustellen. Die Kosten in Höhe von '
    f'<span data-comment-id="{MARKE_KOSTEN}" data-resolved="true">ca. 12.000 Euro brutto</span> werden aus dem '
    "Budget für Stadtmobiliar gedeckt.</p>"
    "<p><em>Begründung:</em> Insbesondere ältere Menschen wünschen sich mehr Verweilmöglichkeiten in der "
    "Innenstadt.</p>"
)


def _naechster_montag(tag: date) -> date:
    return tag + timedelta(days=(7 - tag.weekday()) % 7)


class Command(BaseCommand):
    help = "Ergänzt die Demo-Organisation um Inhalte, die das neue Work-Design vollständig zeigen (nur Demo)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--aufbautag",
            type=date.fromisoformat,
            default=None,
            help="Bezugstag der Termine (JJJJ-MM-TT, Standard heute; setup_demo_environment gibt seinen weiter)",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        org = Organization.objects.filter(slug=DEMO_ORG_SLUG).first()
        if org is None:
            raise CommandError(
                f"Demo-Organisation {DEMO_ORG_SLUG} fehlt – zuerst python manage.py setup_demo_environment ausführen."
            )
        self.zaehler: dict[str, int] = {}
        self.aufbautag: date = options["aufbautag"] or timezone.localdate()
        with transaction.atomic():
            m = self._mitglieder(org)
            rat3, haupt2, rat2, bau1 = self._sitzungen()
            standard = self._standard_tagesordnung(org)
            vorige = self._vergangene_sitzung(org, m)
            antraege = self._antraege(org, m, rat3, haupt2)
            self._kommende_sitzung(org, m, vorige, rat3, antraege)
            self._reihe(org)
            self._positionen(org, m, haupt2, rat2, bau1)
        self._zaehlen("Standard-TOPs", len(standard))
        self.stdout.write(self.style.SUCCESS("Demo-Inhalte für das neue Work-Design:"))
        for name, anzahl in self.zaehler.items():
            self.stdout.write(f"  - Work: {name}: {anzahl}")
        stand = "an" if neues_design(org) else "aus"
        self.stdout.write(f"  - Work: neues Design für {org.slug}: {stand}")

    def _termin(self, tage: int, stunde: int) -> datetime:
        """Termin relativ zum Aufbautag an einem Sitzungstag (wie die Termine der Basisdemo)."""
        return termin_am_sitzungstag(self.aufbautag, tage, stunde)

    def _zaehlen(self, name: str, anzahl: int = 1) -> None:
        self.zaehler[name] = self.zaehler.get(name, 0) + anzahl

    # ------------------------------------------------------------------
    # Bestand der Basisdemo
    # ------------------------------------------------------------------

    def _mitglieder(self, org: Organization) -> dict[str, Membership]:
        mitglieder = {
            key: Membership.objects.filter(organization=org, user__email=DEMO_USERS[key]["email"]).first()
            for key in MITGLIEDER
        }
        fehlend = [key for key, ms in mitglieder.items() if ms is None]
        if fehlend:
            raise CommandError(
                f"Demo-Mitgliedschaften fehlen: {', '.join(fehlend)} – setup_demo_environment ausführen."
            )
        return {key: ms for key, ms in mitglieder.items() if ms is not None}

    def _sitzungen(self) -> tuple[OParlMeeting, OParlMeeting, OParlMeeting, OParlMeeting]:
        schluessel = ("rat-3", "haupt-2", "rat-2", "bau-1")
        gefunden = {m.external_id: m for m in ris.meetings_by_external_id(demo_ext("meeting", k) for k in schluessel)}
        try:
            rat3, haupt2, rat2, bau1 = (gefunden[demo_ext("meeting", k)] for k in schluessel)
        except KeyError as fehler:
            raise CommandError("RIS-Sitzungen der Demo fehlen – setup_demo_environment ausführen.") from fehler
        return rat3, haupt2, rat2, bau1

    @staticmethod
    def _top(sitzung: str, nummer: str) -> OParlAgendaItem:
        top = ris.agenda_items_by_external_id([demo_ext("agendaitem", f"{sitzung}-{nummer}")]).first()
        if top is None:
            raise CommandError(f"Tagesordnungspunkt {sitzung}-{nummer} der Demo fehlt.")
        return top

    @classmethod
    def _vorlage(cls, sitzung: str, nummer: str) -> OParlPaper:
        vorlage = ris.papers_of_agenda_item(cls._top(sitzung, nummer)).first()
        if vorlage is None:
            raise CommandError(f"Vorlage zu {sitzung}-{nummer} der Demo fehlt.")
        return vorlage

    # ------------------------------------------------------------------
    # Fraktionssitzungen
    # ------------------------------------------------------------------

    def _standard_tagesordnung(self, org: Organization) -> list[FactionStandardAgendaItem]:
        punkte = []
        for reihenfolge, (titel, sichtbarkeit) in enumerate(STANDARD_TAGESORDNUNG, start=1):
            punkt, _ = FactionStandardAgendaItem.objects.update_or_create(
                organization=org, title=titel, defaults={"visibility": sichtbarkeit, "order": reihenfolge}
            )
            punkte.append(punkt)
        return punkte

    def _anwesenheiten(self, sitzung: FactionMeeting, m: dict[str, Membership], **status: tuple[str, str]) -> None:
        for key, ms in m.items():
            stand, art = status.get(key, ("invited", "onsite"))
            FactionAttendance.objects.update_or_create(
                meeting=sitzung, membership=ms, defaults={"status": stand, "participation_type": art}
            )

    def _vergangene_sitzung(self, org: Organization, m: dict[str, Membership]) -> FactionMeeting:
        beginn = self._termin(-7, stunde=18)
        sitzung, angelegt = FactionMeeting.objects.update_or_create(
            organization=org,
            title=VERGANGENE_SITZUNG,
            defaults={
                "description": "Reguläre Fraktionssitzung mit Protokoll, das in der nächsten Sitzung genehmigt wird.",
                "start": beginn,
                "end": beginn + timedelta(hours=2),
                "location": ORT,
                "video_link": VIDEOLINK,
                "status": "completed",
                "protocol_status": "pending",
                "attendance_confirmed_at": beginn + timedelta(hours=2),
                "attendance_confirmed_by": m["vorsitz"],
                "created_by": m["vorsitz"],
            },
        )
        if angelegt or not sitzung.meeting_number:
            sitzung.meeting_number = cast(Any, FactionMeeting).get_next_meeting_number(org)
            sitzung.save(update_fields=["meeting_number"])
        sitzung.create_approval_agenda_item()
        agenda.apply_standard_agenda(sitzung)
        self._anwesenheiten(
            sitzung,
            m,
            vorsitz=("present", "onsite"),
            mitglied=("present", "online"),
            sachkundig=("present", "onsite"),
            unvereidigt=("excused", "onsite"),
        )

        tops = {item.title: item for item in sitzung.agenda_items.all()}
        beschluesse = tops["Beschlüsse"]
        beschluesse.has_decision = True
        beschluesse.save(update_fields=["has_decision"])
        FactionDecision.objects.update_or_create(
            agenda_item=beschluesse,
            defaults={
                "votes_yes": 3,
                "votes_no": 0,
                "votes_abstain": 0,
                "result": "accepted",
                "decision_text": "Die Fraktion bringt den Antrag zu Trinkwasserbrunnen in die Ratssitzung ein.",
                "recorded_by": m["vorsitz"],
            },
        )
        eintraege = [
            (
                beschluesse,
                "decision",
                "Antrag Trinkwasserbrunnen: einstimmig beschlossen, Einreichung durch den Vorsitz.",
            ),
            (tops["Termine"], "note", "Bürgersprechstunde am Samstag, 10 bis 12 Uhr, Marktplatz."),
            (
                tops["Presse und Social Media"],
                "action",
                "Beitrag zum Jugendbeirat für die sozialen Medien vorbereiten.",
            ),
            (
                tops["Personal- und Finanzangelegenheiten der Fraktion"],
                "note",
                "Vertraulich: Haushalt der Fraktionsgeschäftsstelle für das kommende Jahr besprochen.",
            ),
        ]
        for reihenfolge, (top, art, text) in enumerate(eintraege, start=1):
            eintrag, _ = FactionProtocolEntry.objects.update_or_create(
                meeting=sitzung,
                agenda_item=top,
                entry_type=art,
                defaults={
                    "order": reihenfolge,
                    "created_by": m["vorsitz"],
                    "action_assignee": m["mitglied"] if art == "action" else None,
                    "action_due_date": timezone.localdate() + timedelta(days=5) if art == "action" else None,
                },
            )
            cast(Any, eintrag).set_content_encrypted(text)
            cast(Any, eintrag).save()
        self._zaehlen("Fraktionssitzungen")
        return sitzung

    def _kommende_sitzung(
        self,
        org: Organization,
        m: dict[str, Membership],
        vorige: FactionMeeting,
        rat3: OParlMeeting,
        antraege: dict[str, Motion],
    ) -> FactionMeeting:
        beginn = self._termin(10, stunde=19)
        if timezone.localtime(beginn).weekday() == 0:
            # Montags tagt die Sitzungsreihe; die Sondersitzung zur Ratssitzung liegt auf einem anderen Tag
            beginn = self._termin(11, stunde=19)
        sitzung, angelegt = FactionMeeting.objects.update_or_create(
            organization=org,
            title=KOMMENDE_SITZUNG,
            defaults={
                "description": "Beratung der Anträge und Vorlagen für die kommende Ratssitzung.",
                "start": beginn,
                "end": beginn + timedelta(hours=2),
                "location": ORT,
                "video_link": VIDEOLINK,
                "status": "planned",
                "created_by": m["vorsitz"],
                "related_meeting": rat3,
            },
        )
        if angelegt or not sitzung.meeting_number:
            sitzung.meeting_number = cast(Any, FactionMeeting).get_next_meeting_number(org)
            sitzung.save(update_fields=["meeting_number"])
        if sitzung.previous_meeting_id is None and not FactionMeeting.objects.filter(previous_meeting=vorige).exists():
            sitzung.previous_meeting = vorige
            sitzung.save(update_fields=["previous_meeting"])
        sitzung.create_approval_agenda_item()
        agenda.apply_standard_agenda(sitzung)
        self._anwesenheiten(sitzung, m)

        politik = sitzung.agenda_items.get(title="Politische Arbeit", parent__isnull=True)
        unterpunkte = [
            (
                "Ratssitzung: Antrag Trinkwasserbrunnen",
                [antraege["eingereicht"], antraege["aenderung_antrag"]],
                "2",
                "Eigener Antrag und Änderungsantrag aus der Fraktion; Redebeitrag im Rat abstimmen.",
            ),
            (
                "Ratssitzung: Feuerwehrbedarfsplan 2026–2031",
                [antraege["aenderung_vorlage"]],
                "4",
                "Im Hauptausschuss mit Änderungsantrag zugestimmt – Linie für den Rat festlegen.",
            ),
        ]
        for nummer, (titel, dokumente, ris_top, beschreibung) in enumerate(unterpunkte, start=1):
            top = self._top("rat-3", ris_top)
            punkt, _ = FactionAgendaItem.objects.update_or_create(
                meeting=sitzung,
                title=titel,
                defaults={
                    "parent": politik,
                    "visibility": "public",
                    "number": f"{politik.number}.{nummer}",
                    "order": politik.order,
                    "related_agenda_item": top,
                },
            )
            cast(Any, punkt).set_description_encrypted(beschreibung)
            punkt.save()
            punkt.related_motions.set(dokumente)
            punkt.related_papers.set(ris.papers_of_agenda_item(top))

        vertraulich, angelegt = FactionAgendaItem.objects.get_or_create(
            meeting=sitzung,
            title="Grundstücksangelegenheit Am Stadtpark",
            defaults={"visibility": "internal", "order": agenda.next_order(sitzung)},
        )
        if angelegt:
            cast(Any, vertraulich).set_description_encrypted(
                "Vertrauliche Vorlage der Verwaltung, nur für vereidigte Mitglieder."
            )
            vertraulich.save()
            agenda.renumber(sitzung, "internal")

        FactionAgendaItem.objects.update_or_create(
            meeting=sitzung,
            title="Bericht aus dem Ausschuss für Bauen und Verkehr",
            defaults={
                "visibility": "public",
                "proposal_status": "proposed",
                "proposed_by": m["sachkundig"],
                "proposed_at": timezone.now() - timedelta(days=1),
                "number": "",
                "order": 0,
            },
        )
        self._zaehlen("Fraktionssitzungen")
        return sitzung

    def _reihe(self, org: Organization) -> FactionMeetingSchedule:
        reihe, _ = FactionMeetingSchedule.objects.update_or_create(
            organization=org,
            name=REIHE,
            defaults={
                "recurrence": "weekly",
                "weekday": 0,
                "time": dt_time(18, 0),
                "duration_minutes": 120,
                "default_location": ORT,
                "default_video_link": VIDEOLINK,
                "rsvp_enabled": False,
                "auto_invite": False,
                "is_active": True,
            },
        )
        # Wie im Betrieb: Feiertage trägt die Fraktion als Ausnahme ein, die Reihe zeigt den Termin als „entfällt“
        heute = timezone.localdate()
        for tag in (heute + timedelta(days=n) for n in range(HORIZONT_TAGE)):
            if tag.weekday() == 0 and not ist_sitzungstag(tag):
                FactionMeetingException.objects.update_or_create(
                    schedule=reihe,
                    original_date=tag,
                    defaults={"exception_type": "cancelled", "reason": "Feiertag (Demo)", "end_date": None},
                )
        pause_beginn = _naechster_montag(heute + timedelta(days=21))
        while (
            FactionMeetingException.objects.filter(schedule=reihe, original_date=pause_beginn)
            .exclude(reason=PAUSE)
            .exists()
        ):
            pause_beginn += timedelta(days=7)
        FactionMeetingException.objects.update_or_create(
            schedule=reihe,
            reason=PAUSE,
            defaults={
                "exception_type": "cancelled",
                "original_date": pause_beginn,
                "end_date": pause_beginn + timedelta(days=7),
            },
        )
        ergebnis = generate_meetings_for_schedule(reihe)
        self._zaehlen("Sitzungsreihe")
        self._zaehlen("Termine der Reihe (neu erzeugt)", ergebnis["created"] + ergebnis["cancelled"])
        return reihe

    # ------------------------------------------------------------------
    # Vorbereitung: Positionen mit Beratungsverlauf
    # ------------------------------------------------------------------

    def _positionen(
        self,
        org: Organization,
        m: dict[str, Membership],
        haupt2: OParlMeeting,
        rat2: OParlMeeting,
        bau1: OParlMeeting,
    ) -> None:
        jetzt = timezone.now()
        vorbereitungen = {}
        for sitzung, wer in ((haupt2, m["vorsitz"]), (rat2, m["vorsitz"]), (bau1, m["sachkundig"])):
            vorbereitungen[sitzung.pk], _ = MeetingPreparation.objects.update_or_create(
                organization=org,
                meeting=sitzung,
                defaults={"membership": wer, "is_prepared": True, "prepared_at": jetzt, "prepared_by": wer},
            )
        positionen = [
            # Vorberatung im Hauptausschuss: Die Ratssitzung zeigt sie im Beratungsverlauf derselben Vorlage
            (
                haupt2,
                "haupt-2",
                "2",
                "amended",
                "",
                "Zustimmung nur mit unserem Änderungsantrag: Gerätehaus Nord vorziehen.",
                m["vorsitz"],
            ),
            (
                haupt2,
                "haupt-2",
                "3",
                "for",
                "",
                "Zustimmung; Geschäftsordnung des Beirats im Rat klären.",
                m["vorsitz"],
            ),
            # Vergangene Sitzungen mit Ergebnis der Beratung
            (rat2, "rat-2", "3", "refer", "referred", "Erst im Bauausschuss beraten.", m["vorsitz"]),
            (bau1, "bau-1", "2", "for", "accepted", "Ladesäulen an allen Parkhäusern.", m["sachkundig"]),
        ]
        for sitzung, schluessel, nummer, position, ergebnis, begruendung, wer in positionen:
            pos, _ = AgendaItemPosition.objects.update_or_create(
                organization=org,
                agenda_item=self._top(schluessel, nummer),
                defaults={
                    "preparation": vorbereitungen[sitzung.pk],
                    "position": position,
                    "outcome": ergebnis,
                    "is_final": True,
                    "set_by": wer,
                },
            )
            cast(Any, pos).set_reasoning_encrypted(begruendung)
            pos.save()
        self._zaehlen("Positionen mit Beratungsverlauf", len(positionen))

    # ------------------------------------------------------------------
    # Antrag mit Kommentaren und Änderungsanträgen
    # ------------------------------------------------------------------

    def _antraege(
        self, org: Organization, m: dict[str, Membership], rat3: OParlMeeting, haupt2: OParlMeeting
    ) -> dict[str, Motion]:
        entwurf = Motion.objects.filter(organization=org, title=ANTRAG_ENTWURF).first()
        eingereicht = Motion.objects.filter(organization=org, title=ANTRAG_EINGEREICHT).first()
        if entwurf is None or eingereicht is None:
            raise CommandError("Anträge der Basisdemo fehlen – setup_demo_environment ausführen.")

        cast(Any, entwurf).set_content_encrypted(ENTWURF_INHALT)
        entwurf.save()
        kommentare = [
            (
                MARKE_BAENKE,
                "zehn zusätzliche Sitzbänke",
                m["mitglied"],
                "Vorschlag: zwölf Bänke, davon zwei mit Armlehnen für ältere Menschen.",
                False,
            ),
            (
                MARKE_KOSTEN,
                "ca. 12.000 Euro brutto",
                m["sachkundig"],
                "Bitte die Kosten vor der Einreichung mit der Verwaltung abstimmen.",
                True,
            ),
        ]
        for marke, stelle, wer, text, erledigt in kommentare:
            kommentar, _ = MotionComment.objects.update_or_create(
                motion=entwurf,
                mark_id=marke,
                defaults={
                    "selected_text": stelle,
                    "content": text,
                    "author": wer,
                    "is_resolved": erledigt,
                    "resolved_by": m["vorsitz"] if erledigt else None,
                    "resolved_at": timezone.now() if erledigt else None,
                },
            )
            if marke == MARKE_BAENKE:
                MotionComment.objects.update_or_create(
                    motion=entwurf,
                    parent=kommentar,
                    author=m["vorsitz"],
                    defaults={"content": "Guter Punkt – ich übernehme das in die nächste Fassung."},
                )
        MotionComment.objects.update_or_create(
            motion=entwurf,
            author=m["unvereidigt"],
            parent=None,
            mark_id=None,
            defaults={"content": "Können wir Standorte mit Schatten bevorzugen?"},
        )
        self._zaehlen("Kommentare am Antrag", 4)

        aenderung_antrag, _ = Motion.objects.update_or_create(
            organization=org,
            title=AENDERUNG_ANTRAG,
            defaults={
                "motion_type": "amendment",
                "status": "draft",
                "visibility": "organization",
                "author": m["mitglied"],
                "summary": "Änderungsvorschlag zum eigenen Antrag: ein vierter Brunnen am Spielplatz Stadtpark.",
                "parent_motion": eingereicht,
                "parent_paper": None,
                "related_meeting": rat3,
            },
        )
        cast(Any, aenderung_antrag).set_content_encrypted(
            "<h2>Änderungsantrag</h2>"
            "<p>Im Beschlussvorschlag wird nach „Bahnhofsvorplatz“ ergänzt: „sowie am Spielplatz im Stadtpark“.</p>"
            "<p><em>Begründung:</em> Der Spielplatz wird im Sommer stark genutzt; dort fehlt Trinkwasser.</p>"
        )
        aenderung_antrag.save()

        aenderung_vorlage, _ = Motion.objects.update_or_create(
            organization=org,
            title=AENDERUNG_VORLAGE,
            defaults={
                "motion_type": "amendment",
                "status": "internal_review",
                "visibility": "organization",
                "author": m["vorsitz"],
                "summary": "Änderungsantrag zur Vorlage des Feuerwehrbedarfsplans, eingebracht im Hauptausschuss.",
                "parent_motion": None,
                "parent_paper": self._vorlage("haupt-2", "2"),
                "related_meeting": haupt2,
            },
        )
        cast(Any, aenderung_vorlage).set_content_encrypted(
            "<h2>Änderungsantrag zum Feuerwehrbedarfsplan 2026–2031</h2>"
            "<p>Der Neubau des Gerätehauses Nord wird von 2029 auf 2027 vorgezogen.</p>"
            "<p><em>Begründung:</em> Die Hilfsfrist wird im Norden heute am häufigsten überschritten.</p>"
        )
        aenderung_vorlage.save()
        self._zaehlen("Änderungsanträge", 2)
        return {
            "entwurf": entwurf,
            "eingereicht": eingereicht,
            "aenderung_antrag": aenderung_antrag,
            "aenderung_vorlage": aenderung_vorlage,
        }
