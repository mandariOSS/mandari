# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Management-Command: Demo- und Musterumgebung aufbauen.

Erstellt eine vollständige, klar als Demo erkennbare Musterumgebung:

1. Insight:  Fiktive Kommune "Musterstadt (Demo)" als OParlBody mit Gremien,
   Fraktionen, Personen, Sitzungen, Tagesordnungspunkten, Vorlagen,
   Beratungen und kleinen PDF-Dateien (synthetisch, ohne OCR).
2. Work:     Organisation "Musterfraktion (Demo)" mit Standard-Rollen,
   drei Demo-Nutzern (Vorsitz, Mitglied, Gast mit Ordner-Freigabe),
   Dokumenten, Sitzungsvorbereitung, Aufgaben und einer Fraktionssitzung.
3. Session:  SessionTenant "Stadtverwaltung Musterstadt (Demo)" mit Gremien,
   Personen (verschlüsselte Kontaktdaten), Sitzungen, Vorlagen, Anträgen
   und einem Demo-Verwaltungsnutzer.

Idempotenz: Alle Objekte werden über feste Demo-Kennungen (Slugs, external_ids
mit Präfix https://demo.mandari.invalid/oparl/...) per update_or_create
angelegt. Ein wiederholter Lauf aktualisiert statt zu duplizieren.

Die Passwörter der Demo-Nutzer werden bei JEDEM Lauf neu generiert und
ausschließlich auf stdout ausgegeben (niemals gespeichert).

Aufräumen: --reset entfernt sämtliche Demo-Daten (und nur diese) wieder.

Verwendung:
    python manage.py setup_demo_environment
    python manage.py setup_demo_environment --reset
"""

import secrets
from datetime import date, timedelta
from datetime import time as dt_time
from io import BytesIO
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

# ---------------------------------------------------------------------------
# Feste Demo-Kennungen (Grundlage der Idempotenz und des --reset)
# ---------------------------------------------------------------------------

DEMO_OPARL_PREFIX = "https://demo.mandari.invalid/oparl"
DEMO_SOURCE_URL = f"{DEMO_OPARL_PREFIX}/system"
DEMO_BODY_SLUG = "musterstadt-demo"
DEMO_ORG_SLUG = "musterfraktion-demo"
DEMO_PARTY_SLUG = "musterpartei-demo"
DEMO_SESSION_SLUG = "stadtverwaltung-musterstadt-demo"
DEMO_EMAIL_DOMAIN = "demo.mandari.de"
DEMO_MEDIA_SUBDIR = "demo"
# Fester Mandatsbeginn: Teil natürlicher Schlüssel (Idempotenz bei Wiederholung)
DEMO_START_DATE = date(2024, 7, 1)

DEMO_USERS = {
    "vorsitz": {
        "email": f"demo-vorsitz@{DEMO_EMAIL_DOMAIN}",
        "first_name": "Vera",
        "last_name": "Beispiel",
    },
    "mitglied": {
        "email": f"demo-mitglied@{DEMO_EMAIL_DOMAIN}",
        "first_name": "Martin",
        "last_name": "Muster",
    },
    "gast": {
        "email": f"demo-gast@{DEMO_EMAIL_DOMAIN}",
        "first_name": "Greta",
        "last_name": "Gast",
    },
    "verwaltung": {
        "email": f"demo-verwaltung@{DEMO_EMAIL_DOMAIN}",
        "first_name": "Victor",
        "last_name": "Verwaltung",
    },
}


def _ext(kind: str, key: str) -> str:
    """Deterministische Demo-external_id (kollidiert nie mit echten Quellen)."""
    return f"{DEMO_OPARL_PREFIX}/{kind}/{key}"


def _minimal_pdf(title: str, lines: list[str]) -> bytes:
    """Kleines, valides PDF erzeugen (reportlab, Fallback: Minimal-Bytes)."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas

        buffer = BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=A4)
        width, height = A4
        pdf.setTitle(title)
        pdf.setFont("Helvetica-Bold", 14)
        pdf.drawString(50, height - 60, title)
        pdf.setFont("Helvetica", 10)
        y = height - 100
        for line in lines:
            pdf.drawString(50, y, line)
            y -= 16
        pdf.setFont("Helvetica-Oblique", 8)
        pdf.drawString(50, 40, "Mandari Demo-Umgebung - synthetisches Musterdokument, kein amtliches Dokument.")
        pdf.showPage()
        pdf.save()
        return buffer.getvalue()
    except Exception:
        # Fallback: minimal gültiges leeres PDF
        return (
            b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
            b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]>>endobj\n"
            b"xref\n0 4\n0000000000 65535 f \ntrailer<</Size 4/Root 1 0 R>>\nstartxref\n0\n%%EOF\n"
        )


class Command(BaseCommand):
    help = "Erstellt (oder aktualisiert) die Demo-/Musterumgebung: Kommune, Work-Organisation und Session-Mandant."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Entfernt alle Demo-Daten wieder (nur die über Demo-Kennungen angelegten Objekte).",
        )

    # ------------------------------------------------------------------
    # Einstieg
    # ------------------------------------------------------------------

    def handle(self, *args, **options):
        if options["reset"]:
            with transaction.atomic():
                self._reset()
            return

        self.counters: dict[str, int] = {}
        self.passwords: dict[str, str] = {}

        with transaction.atomic():
            body = self._setup_insight()
            org = self._setup_work(body)
            self._setup_session(body, org)

        self._print_summary()

    def _count(self, key: str, n: int = 1):
        self.counters[key] = self.counters.get(key, 0) + n

    # ------------------------------------------------------------------
    # 1. Insight: Musterstadt (Demo)
    # ------------------------------------------------------------------

    def _setup_insight(self):
        from insight_core.models import (
            OParlAgendaItem,
            OParlBody,
            OParlConsultation,
            OParlFile,
            OParlLegislativeTerm,
            OParlMeeting,
            OParlMembership,
            OParlOrganization,
            OParlPaper,
            OParlPerson,
            OParlSource,
        )

        now = timezone.now()
        today = now.date()

        source, _ = OParlSource.objects.update_or_create(
            url=DEMO_SOURCE_URL,
            defaults={
                "name": "Mandari Demo-Quelle (synthetische Daten)",
                # Bewusst inaktiv: Der Sync-Daemon darf diese Quelle nie abrufen.
                "is_active": False,
                "sync_config": {"demo": True},
            },
        )

        body, _ = OParlBody.objects.update_or_create(
            external_id=_ext("body", "musterstadt"),
            defaults={
                "source": source,
                "name": "Stadt Musterstadt (Demo)",
                "short_name": "Musterstadt (Demo)",
                "display_name": "Musterstadt (Demo)",
                "slug": DEMO_BODY_SLUG,
                "description": (
                    "Willkommen im Demo-Ratsinformationssystem der fiktiven Stadt Musterstadt. "
                    "Alle Daten sind synthetisch und dienen ausschließlich der Demonstration von mandari."
                ),
                "classification": "Kreisangehörige Stadt (Demo)",
                "deleted": False,
                "oparl_created": now,
                "oparl_modified": now,
            },
        )
        self._count("Insight: Kommune")

        OParlLegislativeTerm.objects.update_or_create(
            external_id=_ext("term", "2024-2029"),
            defaults={
                "body": body,
                "name": "Wahlperiode 2024-2029 (Demo)",
                "start_date": today.replace(year=today.year - 2),
                "end_date": today.replace(year=today.year + 3),
                "deleted": False,
            },
        )

        # --- Gremien und Fraktionen -----------------------------------
        gremien_def = [
            ("rat", "Rat der Stadt Musterstadt", "Rat", "Gremium"),
            ("hauptausschuss", "Hauptausschuss", "Ausschuss", "Gremium"),
            ("bauausschuss", "Ausschuss für Bauen und Verkehr", "Ausschuss", "Gremium"),
            ("fraktion-bunte-liste", "Fraktion Bunte Liste (Demo)", "Fraktion", "Fraktion"),
            ("fraktion-zukunft", "Fraktion Zukunft (Demo)", "Fraktion", "Fraktion"),
        ]
        orgs: dict[str, OParlOrganization] = {}
        for key, name, classification, org_type in gremien_def:
            orgs[key], _ = OParlOrganization.objects.update_or_create(
                external_id=_ext("organization", key),
                defaults={
                    "body": body,
                    "name": name,
                    "short_name": name if len(name) <= 40 else classification,
                    "organization_type": org_type,
                    "classification": classification,
                    "deleted": False,
                    "oparl_created": now,
                    "oparl_modified": now,
                },
            )
        self._count("Insight: Gremien/Fraktionen", len(gremien_def))

        # --- Personen -------------------------------------------------
        personen_def = [
            ("anna-amberg", "Anna", "Amberg", "fraktion-bunte-liste"),
            ("bernd-birkholz", "Bernd", "Birkholz", "fraktion-bunte-liste"),
            ("clara-cornelsen", "Clara", "Cornelsen", "fraktion-bunte-liste"),
            ("dieter-dahl", "Dieter", "Dahl", "fraktion-bunte-liste"),
            ("elif-erden", "Elif", "Erden", "fraktion-zukunft"),
            ("frank-feldmann", "Frank", "Feldmann", "fraktion-zukunft"),
            ("gisela-grote", "Gisela", "Grote", "fraktion-zukunft"),
            ("hakan-heller", "Hakan", "Heller", "fraktion-zukunft"),
        ]
        persons: dict[str, OParlPerson] = {}
        for key, given, family, _faction in personen_def:
            persons[key], _ = OParlPerson.objects.update_or_create(
                external_id=_ext("person", key),
                defaults={
                    "body": body,
                    "name": f"{given} {family}",
                    "given_name": given,
                    "family_name": family,
                    "deleted": False,
                    "oparl_created": now,
                    "oparl_modified": now,
                },
            )
        self._count("Insight: Personen", len(personen_def))

        # --- Mitgliedschaften -----------------------------------------
        membership_count = 0
        for key, _given, _family, faction in personen_def:
            person = persons[key]
            # Fraktion
            OParlMembership.objects.update_or_create(
                external_id=_ext("membership", f"{key}-{faction}"),
                defaults={
                    "person": person,
                    "organization": orgs[faction],
                    "role": "Mitglied",
                    "voting_right": True,
                    "start_date": DEMO_START_DATE,
                    "deleted": False,
                },
            )
            # Rat
            OParlMembership.objects.update_or_create(
                external_id=_ext("membership", f"{key}-rat"),
                defaults={
                    "person": person,
                    "organization": orgs["rat"],
                    "role": "Ratsmitglied",
                    "voting_right": True,
                    "start_date": DEMO_START_DATE,
                    "deleted": False,
                },
            )
            membership_count += 2
        # Ausschussbesetzung + Vorsitz
        ausschuss_zuordnung = [
            ("anna-amberg", "hauptausschuss", "Vorsitzende"),
            ("elif-erden", "hauptausschuss", "Stellv. Vorsitzende"),
            ("bernd-birkholz", "hauptausschuss", "Mitglied"),
            ("frank-feldmann", "bauausschuss", "Vorsitzender"),
            ("clara-cornelsen", "bauausschuss", "Stellv. Vorsitzende"),
            ("hakan-heller", "bauausschuss", "Mitglied"),
        ]
        for person_key, org_key, role in ausschuss_zuordnung:
            OParlMembership.objects.update_or_create(
                external_id=_ext("membership", f"{person_key}-{org_key}"),
                defaults={
                    "person": persons[person_key],
                    "organization": orgs[org_key],
                    "role": role,
                    "voting_right": True,
                    "start_date": DEMO_START_DATE,
                    "deleted": False,
                },
            )
            membership_count += 1
        self._count("Insight: Mitgliedschaften", membership_count)

        # --- Sitzungen ------------------------------------------------
        def meeting_dt(days: int, hour: int = 17):
            return (now + timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)

        sitzungen_def = [
            ("rat-1", "rat", "Sitzung des Rates der Stadt Musterstadt", -84, "Ratssaal"),
            ("rat-2", "rat", "Sitzung des Rates der Stadt Musterstadt", -28, "Ratssaal"),
            ("rat-3", "rat", "Sitzung des Rates der Stadt Musterstadt", 14, "Ratssaal"),
            ("haupt-1", "hauptausschuss", "Sitzung des Hauptausschusses", -42, "Sitzungssaal 1"),
            ("haupt-2", "hauptausschuss", "Sitzung des Hauptausschusses", 7, "Sitzungssaal 1"),
            ("bau-1", "bauausschuss", "Sitzung des Ausschusses für Bauen und Verkehr", -35, "Sitzungssaal 2"),
        ]
        meetings: dict[str, OParlMeeting] = {}
        for key, org_key, name, day_offset, room in sitzungen_def:
            start = meeting_dt(day_offset)
            meeting, _ = OParlMeeting.objects.update_or_create(
                external_id=_ext("meeting", key),
                defaults={
                    "body": body,
                    "name": name,
                    "meeting_state": "terminiert" if day_offset > 0 else "durchgeführt",
                    "start": start,
                    "end": start + timedelta(hours=3),
                    "location_name": f"Rathaus Musterstadt, {room}",
                    "location_address": "Rathausplatz 1, 12345 Musterstadt",
                    "cancelled": False,
                    "deleted": False,
                    "oparl_created": now,
                    "oparl_modified": now,
                },
            )
            meeting.organizations.set([orgs[org_key]])
            meetings[key] = meeting
        self._count("Insight: Sitzungen", len(sitzungen_def))

        # --- Vorlagen -------------------------------------------------
        papers_def = [
            ("spielplatz", "Sanierung des Spielplatzes am Stadtpark", "Beschlussvorlage", "V/2026/D-001", -90),
            ("radweg", "Lückenschluss Radweg Bahnhofstraße", "Beschlussvorlage", "V/2026/D-002", -80),
            ("haushalt", "Haushaltssatzung und Haushaltsplan 2026", "Beschlussvorlage", "V/2026/D-003", -70),
            ("baumschutz", "Neufassung der Baumschutzsatzung", "Beschlussvorlage", "V/2026/D-004", -60),
            ("grundschule", "Energetische Sanierung der Grundschule Am Anger", "Beschlussvorlage", "V/2026/D-005", -55),
            ("ladesaeulen", "Errichtung von Ladesäulen im Stadtgebiet", "Beschlussvorlage", "V/2026/D-006", -50),
            (
                "bplan",
                "Bebauungsplan Nr. 12 'Am Stadtpark' - Aufstellungsbeschluss",
                "Beschlussvorlage",
                "V/2026/D-007",
                -45,
            ),
            ("beleuchtung", "Anfrage: Ausfall der Straßenbeleuchtung im Musterviertel", "Anfrage", "A/2026/D-008", -40),
            ("trinkbrunnen", "Antrag: Öffentliche Trinkwasserbrunnen in der Innenstadt", "Antrag", "A/2026/D-009", -38),
            ("laerm", "Mitteilung: Fortschreibung des Lärmaktionsplans", "Mitteilungsvorlage", "M/2026/D-010", -30),
            ("jugendbeirat", "Antrag: Einrichtung eines Jugendbeirats", "Antrag", "A/2026/D-011", -21),
            ("feuerwehr", "Feuerwehrbedarfsplan 2026-2031", "Beschlussvorlage", "V/2026/D-012", -14),
        ]
        papers: dict[str, OParlPaper] = {}
        for key, name, paper_type, reference, day_offset in papers_def:
            papers[key], _ = OParlPaper.objects.update_or_create(
                external_id=_ext("paper", key),
                defaults={
                    "body": body,
                    "name": name,
                    "reference": reference,
                    "paper_type": paper_type,
                    "date": today + timedelta(days=day_offset),
                    "georef_status": "no_locations",
                    "deleted": False,
                    "oparl_created": now,
                    "oparl_modified": now,
                },
            )
        self._count("Insight: Vorlagen", len(papers_def))

        # --- Tagesordnungen + Beratungen ------------------------------
        # (meeting_key, [(TOP-Nr, Titel, paper_key|None, result)])
        agenda_def = [
            (
                "rat-1",
                [
                    ("1", "Eröffnung und Feststellung der Tagesordnung", None, ""),
                    ("2", "Sanierung des Spielplatzes am Stadtpark", "spielplatz", "Einstimmig beschlossen"),
                    ("3", "Lückenschluss Radweg Bahnhofstraße", "radweg", "Mehrheitlich beschlossen"),
                    ("4", "Anfragen und Mitteilungen", None, ""),
                ],
            ),
            (
                "rat-2",
                [
                    ("1", "Eröffnung und Feststellung der Tagesordnung", None, ""),
                    ("2", "Haushaltssatzung und Haushaltsplan 2026", "haushalt", "Mehrheitlich beschlossen"),
                    ("3", "Neufassung der Baumschutzsatzung", "baumschutz", "Verwiesen in den Bauausschuss"),
                    ("4", "Anfrage: Ausfall der Straßenbeleuchtung im Musterviertel", "beleuchtung", "Beantwortet"),
                ],
            ),
            (
                "rat-3",
                [
                    ("1", "Eröffnung und Feststellung der Tagesordnung", None, ""),
                    ("2", "Antrag: Öffentliche Trinkwasserbrunnen in der Innenstadt", "trinkbrunnen", ""),
                    ("3", "Antrag: Einrichtung eines Jugendbeirats", "jugendbeirat", ""),
                    ("4", "Feuerwehrbedarfsplan 2026-2031", "feuerwehr", ""),
                ],
            ),
            (
                "haupt-1",
                [
                    ("1", "Eröffnung", None, ""),
                    ("2", "Energetische Sanierung der Grundschule Am Anger", "grundschule", "Empfehlung an den Rat"),
                    ("3", "Mitteilung: Fortschreibung des Lärmaktionsplans", "laerm", "Zur Kenntnis genommen"),
                ],
            ),
            (
                "haupt-2",
                [
                    ("1", "Eröffnung", None, ""),
                    ("2", "Feuerwehrbedarfsplan 2026-2031 (Vorberatung)", "feuerwehr", ""),
                    ("3", "Antrag: Einrichtung eines Jugendbeirats (Vorberatung)", "jugendbeirat", ""),
                ],
            ),
            (
                "bau-1",
                [
                    ("1", "Eröffnung", None, ""),
                    ("2", "Errichtung von Ladesäulen im Stadtgebiet", "ladesaeulen", "Einstimmig beschlossen"),
                    (
                        "3",
                        "Bebauungsplan Nr. 12 'Am Stadtpark' - Aufstellungsbeschluss",
                        "bplan",
                        "Mehrheitlich beschlossen",
                    ),
                ],
            ),
        ]
        top_count = 0
        consultation_count = 0
        for meeting_key, tops in agenda_def:
            meeting = meetings[meeting_key]
            for order, (number, title, paper_key, result) in enumerate(tops, start=1):
                item_ext = _ext("agendaitem", f"{meeting_key}-{number}")
                OParlAgendaItem.objects.update_or_create(
                    external_id=item_ext,
                    defaults={
                        "meeting": meeting,
                        "number": number,
                        "order": order,
                        "name": title,
                        "public": True,
                        "result": result,
                        "deleted": False,
                        "oparl_created": now,
                        "oparl_modified": now,
                    },
                )
                top_count += 1
                if paper_key:
                    is_council = meeting_key.startswith("rat")
                    OParlConsultation.objects.update_or_create(
                        external_id=_ext("consultation", f"{meeting_key}-{number}-{paper_key}"),
                        defaults={
                            "body": body,
                            "paper": papers[paper_key],
                            "paper_external_id": papers[paper_key].external_id,
                            "meeting_external_id": meeting.external_id,
                            "agenda_item_external_id": item_ext,
                            "role": "Entscheidung" if is_council else "Vorberatung",
                            "authoritative": is_council,
                            "deleted": False,
                        },
                    )
                    consultation_count += 1
        self._count("Insight: Tagesordnungspunkte", top_count)
        self._count("Insight: Beratungen", consultation_count)

        # --- Dateien (kleine generierte PDFs mit Text) ----------------
        files_def = [
            (
                "spielplatz-vorlage",
                "spielplatz",
                "Beschlussvorlage Spielplatz-Sanierung (Demo)",
                "demo-vorlage-spielplatz.pdf",
                [
                    "Beschlussvorlage V/2026/D-001 - Sanierung des Spielplatzes am Stadtpark",
                    "",
                    "Sachverhalt: Der Spielplatz am Stadtpark ist in die Jahre gekommen.",
                    "Mehrere Spielgeräte entsprechen nicht mehr den aktuellen Sicherheitsnormen.",
                    "Die Verwaltung schlägt eine vollständige Sanierung mit neuen, inklusiven",
                    "Spielgeräten vor. Die Kosten betragen voraussichtlich 180.000 Euro brutto.",
                    "",
                    "Beschlussvorschlag: Der Rat beauftragt die Verwaltung, die Sanierung des",
                    "Spielplatzes am Stadtpark im Haushaltsjahr 2026 umzusetzen.",
                ],
            ),
            (
                "radweg-vorlage",
                "radweg",
                "Beschlussvorlage Radweg Bahnhofstraße (Demo)",
                "demo-vorlage-radweg.pdf",
                [
                    "Beschlussvorlage V/2026/D-002 - Lückenschluss Radweg Bahnhofstraße",
                    "",
                    "Sachverhalt: Zwischen Bahnhof und Stadtpark fehlt auf 400 Metern ein",
                    "durchgängiger Radweg. Der Lückenschluss erhöht die Verkehrssicherheit",
                    "insbesondere für den Schulverkehr. Die Kosten betragen voraussichtlich",
                    "95.000 Euro brutto, eine Landesförderung von 60 Prozent ist beantragt.",
                    "",
                    "Beschlussvorschlag: Der Rat beschließt den Lückenschluss des Radwegs",
                    "in der Bahnhofstraße gemäß Anlage 1.",
                ],
            ),
        ]
        media_dir = Path(settings.MEDIA_ROOT) / DEMO_MEDIA_SUBDIR
        media_dir.mkdir(parents=True, exist_ok=True)
        site_url = getattr(settings, "SITE_URL", "").rstrip("/")
        media_url = settings.MEDIA_URL if settings.MEDIA_URL.startswith("/") else f"/{settings.MEDIA_URL}"
        for key, paper_key, name, filename, lines in files_def:
            pdf_bytes = _minimal_pdf(name, lines)
            (media_dir / filename).write_bytes(pdf_bytes)
            file_url = f"{site_url}{media_url}{DEMO_MEDIA_SUBDIR}/{filename}"
            OParlFile.objects.update_or_create(
                external_id=_ext("file", key),
                defaults={
                    "body": body,
                    "paper": papers[paper_key],
                    "name": name,
                    "file_name": filename,
                    "mime_type": "application/pdf",
                    "size": len(pdf_bytes),
                    "access_url": file_url,
                    "download_url": file_url,
                    "text_content": "\n".join(lines),
                    "text_extraction_status": "completed",
                    "text_extraction_method": "none",
                    "text_extracted_at": now,
                    "page_count": 1,
                    "deleted": False,
                    "oparl_created": now,
                    "oparl_modified": now,
                },
            )
        self._count("Insight: Dateien (PDF)", len(files_def))

        self._insight_refs = {"orgs": orgs, "persons": persons, "meetings": meetings, "papers": papers}
        return body

    # ------------------------------------------------------------------
    # 2. Work: Musterfraktion (Demo)
    # ------------------------------------------------------------------

    def _make_user(self, key: str):
        from apps.accounts.models import User

        info = DEMO_USERS[key]
        password = secrets.token_urlsafe(14)
        user, _ = User.objects.update_or_create(
            email=info["email"],
            defaults={
                "first_name": info["first_name"],
                "last_name": info["last_name"],
                "is_active": True,
                "email_verified": True,
            },
        )
        user.set_password(password)
        user.save(update_fields=["password"])
        self.passwords[info["email"]] = password
        return user

    def _setup_work(self, body):
        from apps.tenants.models import Membership, Organization, PartyGroup, Role
        from apps.work.meetings.models import AgendaItemNote, AgendaItemPosition
        from apps.work.models import FactionAgendaItem, FactionMeeting, MeetingPreparation, Motion, Task
        from apps.work.motions.models import DocumentFolder, FolderGuestShare
        from insight_core.models import OParlAgendaItem

        now = timezone.now()
        today = now.date()

        party, _ = PartyGroup.objects.update_or_create(
            slug=DEMO_PARTY_SLUG,
            defaults={
                "name": "Musterpartei (Demo)",
                "abbreviation": "MUSTER",
                "description": "Fiktive Partei für die Mandari-Demo-Umgebung.",
                "is_active": True,
            },
        )

        org, _ = Organization.objects.update_or_create(
            slug=DEMO_ORG_SLUG,
            defaults={
                "name": "Musterfraktion (Demo)",
                "description": (
                    "Demo-Arbeitsumgebung einer fiktiven Fraktion in der Stadt Musterstadt. "
                    "Alle Inhalte sind synthetisch."
                ),
                "body": body,
                "party_group": party,
                "is_active": True,
                "plan": "community",
            },
        )
        org.oparl_organizations.set([self._insight_refs["orgs"]["fraktion-bunte-liste"]])
        self._count("Work: Organisation")

        # Standard-Rollen (setup_roles-Mechanik)
        roles = Role.create_default_roles(org)
        self._count("Work: Rollen", len(roles))
        role_by_name = {r.name: r for r in roles}

        # --- Nutzer + Mitgliedschaften --------------------------------
        user_vorsitz = self._make_user("vorsitz")
        user_mitglied = self._make_user("mitglied")
        user_gast = self._make_user("gast")

        ms_vorsitz, _ = Membership.objects.update_or_create(
            user=user_vorsitz,
            organization=org,
            defaults={"is_active": True, "is_guest": False},
        )
        ms_vorsitz.roles.set([role_by_name["Fraktionsvorsitz"]])
        ms_vorsitz.oparl_person = self._insight_refs["persons"]["anna-amberg"]
        ms_vorsitz.save(update_fields=["oparl_person"])
        ms_vorsitz.oparl_committees.set(
            [self._insight_refs["orgs"]["rat"], self._insight_refs["orgs"]["hauptausschuss"]]
        )

        ms_mitglied, _ = Membership.objects.update_or_create(
            user=user_mitglied,
            organization=org,
            defaults={"is_active": True, "is_guest": False},
        )
        ms_mitglied.roles.set([role_by_name["Fraktionsmitglied"]])
        ms_mitglied.oparl_person = self._insight_refs["persons"]["bernd-birkholz"]
        ms_mitglied.save(update_fields=["oparl_person"])
        ms_mitglied.oparl_committees.set([self._insight_refs["orgs"]["rat"]])

        ms_gast, _ = Membership.objects.update_or_create(
            user=user_gast,
            organization=org,
            defaults={"is_active": True, "is_guest": True},
        )
        ms_gast.roles.clear()
        self._count("Work: Nutzer/Mitgliedschaften", 3)

        if not org.owner_id:
            org.owner = user_vorsitz
            org.save(update_fields=["owner"])

        # --- Ordner + Gast-Freigabe -----------------------------------
        folder, _ = DocumentFolder.objects.update_or_create(
            organization=org,
            parent=None,
            name="Öffentliche Unterlagen (Demo)",
            defaults={"color": "blue", "created_by": ms_vorsitz},
        )
        FolderGuestShare.objects.update_or_create(
            folder=folder,
            user=user_gast,
            defaults={"level": "view", "created_by": user_vorsitz},
        )
        self._count("Work: Ordner mit Gast-Freigabe")

        # --- Dokumente / Anträge --------------------------------------
        motion_draft, _ = Motion.objects.update_or_create(
            organization=org,
            title="Antrag: Mehr Sitzgelegenheiten in der Innenstadt (Demo)",
            defaults={
                "motion_type": "motion",
                "status": "draft",
                "visibility": "organization",
                "author": ms_vorsitz,
                "summary": "Entwurf: Aufstellung von zehn zusätzlichen Bänken entlang der Fußgängerzone.",
                "folder": None,
            },
        )
        motion_draft.set_content_encrypted(
            "<h2>Antrag: Mehr Sitzgelegenheiten in der Innenstadt</h2>"
            "<p>Die Verwaltung wird beauftragt, entlang der Fußgängerzone zehn zusätzliche "
            "Sitzbänke aufzustellen. Die Kosten in Höhe von ca. 12.000 Euro brutto werden aus "
            "dem Budget für Stadtmobiliar gedeckt.</p>"
            "<p><em>Begründung:</em> Insbesondere ältere Menschen wünschen sich mehr "
            "Verweilmöglichkeiten in der Innenstadt.</p>"
        )
        motion_draft.save()

        motion_submitted, _ = Motion.objects.update_or_create(
            organization=org,
            title="Antrag: Öffentliche Trinkwasserbrunnen in der Innenstadt (Demo)",
            defaults={
                "motion_type": "motion",
                "status": "submitted",
                "visibility": "organization",
                "author": ms_vorsitz,
                "responsible": ms_mitglied,
                "summary": "Eingereicht: Errichtung von drei Trinkwasserbrunnen bis Sommer 2026.",
                "submitted_at": now - timedelta(days=14),
                "folder": folder,
                "related_paper": self._insight_refs["papers"]["trinkbrunnen"],
                "related_meeting": self._insight_refs["meetings"]["rat-3"],
            },
        )
        motion_submitted.set_content_encrypted(
            "<h2>Antrag: Öffentliche Trinkwasserbrunnen in der Innenstadt</h2>"
            "<p>Der Rat möge beschließen: Die Verwaltung errichtet bis Sommer 2026 drei "
            "öffentliche Trinkwasserbrunnen in der Innenstadt (Marktplatz, Stadtpark, Bahnhofsvorplatz). "
            "Die Kosten betragen ca. 45.000 Euro brutto.</p>"
            "<p><em>Begründung:</em> Angesichts zunehmender Hitzetage verbessert kostenloses "
            "Trinkwasser die Aufenthaltsqualität und die Gesundheitsvorsorge.</p>"
        )
        motion_submitted.save()
        self._count("Work: Dokumente/Anträge", 2)

        # --- Sitzungsvorbereitung (kommende Ratssitzung) --------------
        rat3 = self._insight_refs["meetings"]["rat-3"]
        prep, _ = MeetingPreparation.objects.update_or_create(
            organization=org,
            meeting=rat3,
            defaults={
                "membership": ms_vorsitz,
                "is_prepared": True,
                "prepared_at": now,
                "prepared_by": ms_vorsitz,
            },
        )
        prep.set_notes_encrypted(
            "Demo-Notiz: Schwerpunkt der Sitzung sind unsere beiden Anträge "
            "(Trinkwasserbrunnen, Jugendbeirat). Redebeitrag übernimmt Vera."
        )
        prep.save()

        top_trinkbrunnen = OParlAgendaItem.objects.get(external_id=_ext("agendaitem", "rat-3-2"))
        top_jugendbeirat = OParlAgendaItem.objects.get(external_id=_ext("agendaitem", "rat-3-3"))
        top_eroeffnung = OParlAgendaItem.objects.get(external_id=_ext("agendaitem", "rat-3-1"))

        pos1, _ = AgendaItemPosition.objects.update_or_create(
            organization=org,
            agenda_item=top_trinkbrunnen,
            defaults={"preparation": prep, "position": "for", "is_final": True, "set_by": ms_vorsitz},
        )
        pos1.set_reasoning_encrypted("Eigener Antrag - geschlossene Zustimmung der Fraktion.")
        pos1.save()

        pos2, _ = AgendaItemPosition.objects.update_or_create(
            organization=org,
            agenda_item=top_jugendbeirat,
            defaults={"preparation": prep, "position": "for", "is_final": False, "set_by": ms_mitglied},
        )
        pos2.set_reasoning_encrypted("Zustimmung mit Prüfauftrag zur Geschäftsordnung des Beirats.")
        pos2.save()

        note, _ = AgendaItemNote.objects.update_or_create(
            organization=org,
            agenda_item=top_eroeffnung,
            author=ms_vorsitz,
            defaults={"visibility": "organization"},
        )
        note.set_content_encrypted("Demo-Notiz: Zu Beginn Dringlichkeitsantrag zur Tagesordnung ankündigen.")
        note.save()
        self._count("Work: Sitzungsvorbereitung (Positionen/Notizen)", 1)

        # --- Aufgaben -------------------------------------------------
        tasks_def = [
            (
                "Redebeitrag Trinkwasserbrunnen vorbereiten (Demo)",
                "Kernpunkte: Kosten, Standorte, Hitzevorsorge. Maximal 3 Minuten.",
                "in_progress",
                "high",
                ms_vorsitz,
                ms_vorsitz,
                10,
            ),
            (
                "Pressemitteilung zum Jugendbeirat entwerfen (Demo)",
                "Entwurf bis zwei Tage vor der Ratssitzung an alle Mitglieder schicken.",
                "todo",
                "medium",
                ms_vorsitz,
                ms_mitglied,
                7,
            ),
            (
                "Rückmeldungen zur Baumschutzsatzung sammeln (Demo)",
                "Hinweise aus der Bürgersprechstunde zusammentragen und im Wiki ablegen.",
                "todo",
                "low",
                ms_mitglied,
                ms_mitglied,
                21,
            ),
        ]
        for title, description, status, priority, created_by, assigned_to, due_in in tasks_def:
            Task.objects.update_or_create(
                organization=org,
                title=title,
                defaults={
                    "description": description,
                    "visibility": "organization",
                    "status": status,
                    "priority": priority,
                    "created_by": created_by,
                    "assigned_to": assigned_to,
                    "due_date": today + timedelta(days=due_in),
                },
            )
        self._count("Work: Aufgaben", len(tasks_def))

        # --- Fraktionssitzung -----------------------------------------
        fm_start = (now + timedelta(days=10)).replace(hour=19, minute=0, second=0, microsecond=0)
        faction_meeting, _ = FactionMeeting.objects.update_or_create(
            organization=org,
            title="Fraktionssitzung zur Vorbereitung der Ratssitzung (Demo)",
            defaults={
                "description": "Interne Demo-Sitzung: Beratung der Anträge für die kommende Ratssitzung.",
                "start": fm_start,
                "end": fm_start + timedelta(hours=2),
                "location": "Fraktionsbüro, Rathausplatz 1, Musterstadt",
                "status": "planned",
                "created_by": ms_vorsitz,
                "related_meeting": rat3,
            },
        )
        for number, title in [("1", "Begrüßung und Protokoll"), ("2", "Beratung: Anträge zur Ratssitzung")]:
            FactionAgendaItem.objects.update_or_create(
                meeting=faction_meeting,
                number=number,
                title=title,
                defaults={"visibility": "internal"},
            )
        self._count("Work: Fraktionssitzung")

        return org

    # ------------------------------------------------------------------
    # 3. Session: Stadtverwaltung Musterstadt (Demo)
    # ------------------------------------------------------------------

    def _setup_session(self, body, work_org):
        from apps.session.models import (
            SessionAgendaItem,
            SessionApplication,
            SessionMeeting,
            SessionOrganization,
            SessionOrganizationMembership,
            SessionPaper,
            SessionPerson,
            SessionRole,
            SessionTenant,
            SessionUser,
        )

        now = timezone.now()
        today = now.date()

        tenant, _ = SessionTenant.objects.update_or_create(
            slug=DEMO_SESSION_SLUG,
            defaults={
                "name": "Stadtverwaltung Musterstadt (Demo)",
                "short_name": "Musterstadt (Demo)",
                "description": "Demo-Mandant des Verwaltungs-RIS. Alle Daten sind synthetisch.",
                "oparl_body": body,
                "is_active": True,
            },
        )
        self._count("Session: Mandant")

        if not tenant.roles.exists():
            SessionRole.create_default_roles(tenant)
        admin_role = tenant.roles.filter(is_admin=True).first()
        self._count("Session: Rollen", tenant.roles.count())

        # --- Demo-Verwaltungsnutzer -----------------------------------
        user_verwaltung = self._make_user("verwaltung")
        session_user, _ = SessionUser.objects.update_or_create(
            user=user_verwaltung,
            tenant=tenant,
            defaults={"is_active": True},
        )
        if admin_role:
            session_user.roles.set([admin_role])
        self._count("Session: Verwaltungsnutzer")

        # --- Gremien --------------------------------------------------
        s_orgs: dict[str, SessionOrganization] = {}
        session_org_def = [
            ("rat", "Rat der Stadt Musterstadt", "council"),
            ("hauptausschuss", "Hauptausschuss", "committee"),
            ("bauausschuss", "Ausschuss für Bauen und Verkehr", "committee"),
        ]
        for key, name, org_type in session_org_def:
            s_orgs[key], _ = SessionOrganization.objects.update_or_create(
                tenant=tenant,
                name=name,
                defaults={
                    "organization_type": org_type,
                    "oparl_organization": self._insight_refs["orgs"][key],
                    "default_meeting_location": "Rathaus Musterstadt",
                    "default_meeting_start_time": dt_time(17, 0),
                    "is_active": True,
                },
            )
        self._count("Session: Gremien", len(session_org_def))

        # --- Personen (verschlüsselte Kontaktdaten über Accessoren) ---
        s_persons: dict[str, SessionPerson] = {}
        person_keys = list(self._insight_refs["persons"].keys())
        for idx, key in enumerate(person_keys):
            oparl_person = self._insight_refs["persons"][key]
            person, _ = SessionPerson.objects.update_or_create(
                tenant=tenant,
                given_name=oparl_person.given_name,
                family_name=oparl_person.family_name,
                defaults={
                    "oparl_person": oparl_person,
                    "email": f"{key}@{DEMO_EMAIL_DOMAIN}",
                    "is_active": True,
                    "start_date": DEMO_START_DATE,
                },
            )
            # Verschlüsselte Felder ausschließlich über die Accessoren setzen
            person.set_phone_encrypted(f"+49 1234 5678-{idx + 10:02d}")
            person.set_address_encrypted(f"Musterweg {idx + 1}, 12345 Musterstadt")
            person.save()
            s_persons[key] = person
        self._count("Session: Personen", len(person_keys))

        # --- Besetzung passend zur Kommune ----------------------------
        membership_count = 0
        for key in person_keys:
            _, created = SessionOrganizationMembership.objects.update_or_create(
                organization=s_orgs["rat"],
                person=s_persons[key],
                start_date=DEMO_START_DATE,
                defaults={"role": "member", "has_voting_rights": True},
            )
            membership_count += 1
        for person_key, org_key, role in [
            ("anna-amberg", "hauptausschuss", "chair"),
            ("elif-erden", "hauptausschuss", "deputy_chair"),
            ("bernd-birkholz", "hauptausschuss", "member"),
            ("frank-feldmann", "bauausschuss", "chair"),
            ("clara-cornelsen", "bauausschuss", "deputy_chair"),
            ("hakan-heller", "bauausschuss", "member"),
        ]:
            SessionOrganizationMembership.objects.update_or_create(
                organization=s_orgs[org_key],
                person=s_persons[person_key],
                start_date=DEMO_START_DATE,
                defaults={"role": role, "has_voting_rights": True},
            )
            membership_count += 1
        self._count("Session: Gremienbesetzungen", membership_count)

        # --- Vorlagen -------------------------------------------------
        s_papers = {}
        session_papers_def = [
            (
                "SV/2026/D-001",
                "Sanierung des Spielplatzes am Stadtpark",
                "proposal",
                "approved",
                "rat",
                "Der Spielplatz am Stadtpark wird vollständig saniert (Kosten: 180.000 Euro brutto).",
                "Der Rat beauftragt die Verwaltung mit der Sanierung im Haushaltsjahr 2026.",
            ),
            (
                "SV/2026/D-002",
                "Feuerwehrbedarfsplan 2026-2031",
                "proposal",
                "review",
                "hauptausschuss",
                "Fortschreibung des Feuerwehrbedarfsplans für die Jahre 2026 bis 2031.",
                "Der Rat beschließt den Feuerwehrbedarfsplan 2026-2031.",
            ),
            (
                "SV/2026/D-003",
                "Mitteilung: Fortschreibung des Lärmaktionsplans",
                "report",
                "completed",
                "hauptausschuss",
                "Die Verwaltung informiert über den Stand der Fortschreibung des Lärmaktionsplans.",
                "Die Mitteilung wird zur Kenntnis genommen.",
            ),
            (
                "SV/2026/D-004",
                "Neufassung der Straßenreinigungssatzung",
                "bylaw",
                "draft",
                "rat",
                "Die Straßenreinigungssatzung wird redaktionell und gebührenseitig angepasst.",
                "Der Rat beschließt die Neufassung der Straßenreinigungssatzung.",
            ),
        ]
        for reference, name, ptype, status, org_key, main_text, resolution in session_papers_def:
            paper, _ = SessionPaper.objects.update_or_create(
                tenant=tenant,
                reference=reference,
                defaults={
                    "name": name,
                    "paper_type": ptype,
                    "status": status,
                    "main_text": main_text,
                    "resolution_text": resolution,
                    "is_public": True,
                    "date": today - timedelta(days=20),
                    "main_organization": s_orgs[org_key],
                    "created_by": session_user,
                },
            )
            s_papers[reference] = paper
        # Ein Beispiel für vertraulichen (verschlüsselten) Inhalt
        vertraulich = s_papers["SV/2026/D-004"]
        vertraulich.set_confidential_text_encrypted(
            "Demo: Interner Vermerk zur Gebührenkalkulation (nicht öffentlich)."
        )
        vertraulich.save()
        self._count("Session: Vorlagen", len(session_papers_def))

        # --- Sitzungen mit Tagesordnung -------------------------------
        def s_meeting_dt(days: int):
            return (now + timedelta(days=days)).replace(hour=17, minute=0, second=0, microsecond=0)

        # Namen sind der natürliche Schlüssel je (tenant, organization) — daher eindeutig
        session_meetings_def = [
            (
                "Ratssitzung (Demo, kommend)",
                "rat",
                14,
                "scheduled",
                [
                    ("1", "Eröffnung und Feststellung der Tagesordnung", None),
                    ("2", "Sanierung des Spielplatzes am Stadtpark", "SV/2026/D-001"),
                    ("3", "Neufassung der Straßenreinigungssatzung", "SV/2026/D-004"),
                ],
            ),
            (
                "Hauptausschuss (Demo, kommend)",
                "hauptausschuss",
                7,
                "invitation_sent",
                [("1", "Eröffnung", None), ("2", "Feuerwehrbedarfsplan 2026-2031 (Vorberatung)", "SV/2026/D-002")],
            ),
            (
                "Hauptausschuss (Demo, vergangen)",
                "hauptausschuss",
                -28,
                "completed",
                [("1", "Eröffnung", None), ("2", "Mitteilung: Fortschreibung des Lärmaktionsplans", "SV/2026/D-003")],
            ),
        ]
        top_count = 0
        for name, org_key, day_offset, state, tops in session_meetings_def:
            start = s_meeting_dt(day_offset)
            meeting, _ = SessionMeeting.objects.update_or_create(
                tenant=tenant,
                organization=s_orgs[org_key],
                name=name,
                defaults={
                    "start": start,
                    "end": start + timedelta(hours=3),
                    "location": "Rathaus Musterstadt",
                    "room": "Ratssaal" if org_key == "rat" else "Sitzungssaal 1",
                    "street_address": "Rathausplatz 1",
                    "postal_code": "12345",
                    "locality": "Musterstadt",
                    "meeting_state": state,
                    "is_public": True,
                    "created_by": session_user,
                },
            )
            for order, (number, title, paper_ref) in enumerate(tops, start=1):
                SessionAgendaItem.objects.update_or_create(
                    meeting=meeting,
                    number=number,
                    defaults={
                        "name": title,
                        "order": order,
                        "is_public": True,
                        "paper": s_papers.get(paper_ref) if paper_ref else None,
                        "vote_result": "approved" if state == "completed" and paper_ref else "pending",
                    },
                )
                top_count += 1
        self._count("Session: Sitzungen", len(session_meetings_def))
        self._count("Session: Tagesordnungspunkte", top_count)

        # --- Anträge von Fraktionen -----------------------------------
        applications_def = [
            (
                "Antrag: Öffentliche Trinkwasserbrunnen in der Innenstadt (Demo)",
                "motion",
                "in_review",
                "Angesichts zunehmender Hitzetage verbessert kostenloses Trinkwasser die "
                "Aufenthaltsqualität und die Gesundheitsvorsorge in der Innenstadt.",
                "Die Verwaltung errichtet bis Sommer 2026 drei öffentliche Trinkwasserbrunnen "
                "(Marktplatz, Stadtpark, Bahnhofsvorplatz).",
                "Kosten: ca. 45.000 Euro brutto, Deckung aus dem Klimafolgenanpassungs-Budget.",
            ),
            (
                "Antrag: Einrichtung eines Jugendbeirats (Demo)",
                "motion",
                "submitted",
                "Junge Menschen sollen frühzeitig und verbindlich an kommunalen Entscheidungen beteiligt werden.",
                "Der Rat beschließt die Einrichtung eines Jugendbeirats zum Schuljahr 2026/27.",
                "",
            ),
        ]
        for title, app_type, status, justification, resolution, financial in applications_def:
            application, _ = SessionApplication.objects.update_or_create(
                tenant=tenant,
                title=title,
                defaults={
                    "application_type": app_type,
                    "status": status,
                    "justification": justification,
                    "resolution_proposal": resolution,
                    "financial_impact": financial,
                    "submitting_organization": work_org,
                    "submitter_name": "Vera Beispiel (Demo)",
                    "submitter_email": DEMO_USERS["vorsitz"]["email"],
                    "target_organization": s_orgs["rat"],
                    "received_at": now - timedelta(days=10) if status != "submitted" else None,
                    "received_by": session_user if status != "submitted" else None,
                },
            )
            if status == "in_review":
                application.set_additional_info_encrypted(
                    "Demo: Vertrauliche Anmerkung der Fraktion zur Standortabstimmung."
                )
                application.save()
        self._count("Session: Anträge", len(applications_def))

        return tenant

    # ------------------------------------------------------------------
    # Reset
    # ------------------------------------------------------------------

    def _reset(self):
        from apps.accounts.models import User
        from apps.session.models import SessionTenant
        from apps.tenants.models import Organization, PartyGroup
        from insight_core.models import OParlSource

        deleted = []

        # Die Audit-Receiver erkennen die Mandanten-Kaskadenlöschung selbst
        # und überspringen das Protokollieren (Issue #56) — keine lokale
        # Umgehung mehr nötig.
        count, _ = SessionTenant.objects.filter(slug=DEMO_SESSION_SLUG).delete()
        deleted.append(f"Session-Mandant (+abhängige Objekte): {count}")

        count, _ = Organization.objects.filter(slug=DEMO_ORG_SLUG).delete()
        deleted.append(f"Work-Organisation (+abhängige Objekte): {count}")

        count, _ = PartyGroup.objects.filter(slug=DEMO_PARTY_SLUG).delete()
        deleted.append(f"Parteigruppe: {count}")

        demo_emails = [info["email"] for info in DEMO_USERS.values()]
        count, _ = User.objects.filter(email__in=demo_emails).delete()
        deleted.append(f"Demo-Nutzer (+abhängige Objekte): {count}")

        count, _ = OParlSource.objects.filter(url=DEMO_SOURCE_URL).delete()
        deleted.append(f"OParl-Demo-Quelle inkl. Kommune (+abhängige Objekte): {count}")

        media_dir = Path(settings.MEDIA_ROOT) / DEMO_MEDIA_SUBDIR
        removed_files = 0
        if media_dir.is_dir():
            for pdf in media_dir.glob("demo-*.pdf"):
                pdf.unlink(missing_ok=True)
                removed_files += 1
        deleted.append(f"Demo-PDF-Dateien im Media-Verzeichnis: {removed_files}")

        self.stdout.write(self.style.SUCCESS("Demo-Umgebung entfernt:"))
        for line in deleted:
            self.stdout.write(f"  - {line}")

    # ------------------------------------------------------------------
    # Ausgabe
    # ------------------------------------------------------------------

    def _print_summary(self):
        self.stdout.write(self.style.SUCCESS("\nDemo-Umgebung erfolgreich angelegt/aktualisiert:"))
        for key, value in self.counters.items():
            self.stdout.write(f"  - {key}: {value}")

        self.stdout.write(self.style.WARNING("\nDemo-Zugangsdaten (werden NICHT gespeichert - jetzt notieren!):"))
        for email, password in self.passwords.items():
            self.stdout.write(f"  {email}  ->  {password}")

        self.stdout.write("\nEinstiegspunkte:")
        self.stdout.write(f"  Insight (öffentlich): /  (Kommune 'Musterstadt (Demo)', Slug {DEMO_BODY_SLUG})")
        self.stdout.write(f"  Work:                 /work/{DEMO_ORG_SLUG}/")
        self.stdout.write(f"  Session:              /session/{DEMO_SESSION_SLUG}/")
        self.stdout.write("\nHinweis: Wiederholter Lauf aktualisiert die Daten und rotiert die Passwörter.")
        self.stdout.write("Entfernen mit: python manage.py setup_demo_environment --reset\n")
