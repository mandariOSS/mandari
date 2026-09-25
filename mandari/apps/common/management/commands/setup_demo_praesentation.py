# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Management-Command: Präsentationsumgebung „Demo-Drehbuch“ aufsetzen.

Baut auf der Demo-Umgebung (``setup_demo_environment``) eine Präsentation auf, in der eine einzige
Drucksache ihren Weg nimmt – von der Fraktion (Work) über den Sitzungsdienst (Session) bis ins
Bürgerportal (Insight) und in die OParl-Schnittstelle:

1. Mandant A (der Demo-Mandant) bekommt Wahlperiode und Nummernkreis des Profils, veröffentlicht im
   Bürgerportal und zeigt dort den Umsetzungsstand seiner Beschlüsse.
2. Mandant B „Bezirksamt Musterstadt-Süd (Demo)“ zählt seine Drucksachen selbst (eigener Zähler).
3. Die Leitstelle ist Administrator in A und B – daran lässt sich der Mandantenwechsel zeigen.
4. Die Musterfraktion ist über einen Einreichungs-Token mit Mandant A verbunden.
5. Drehbuch-Daten in A: Antrag im Entwurf (Work), kommende Sitzung mit Ö- und NÖ-Teil,
   Beratungsfolge, vergangene Sitzung mit Anwesenheit, namentlicher Abstimmung, Protokoll und
   Beschlusskontrolle sowie offene Sitzungsgeld-Positionen für das Vier-Augen-Prinzip.
6. Mandant A wird synchron im Prozess ins Bürgerportal gespiegelt; die Kommune bleibt ungelistet.

Idempotenz: Feste Titel und Slugs sind natürliche Schlüssel, wiederholte Läufe aktualisieren statt zu
verdoppeln. Ein erneuter Lauf setzt zugleich die live gespielten Schritte zurück (Antrag wieder im
Entwurf, TOP wieder öffentlich, Sitzungsgeld wieder offen) – so lässt sich das Drehbuch proben.
Vergebene Vorlagennummern bleiben vergeben, Nummernkreise zählen nur aufwärts.

Die Basisdemo wird nur angelegt, wenn der Demo-Mandant fehlt; ihre Passwörter rotieren sonst nicht.
Das Passwort der Leitstelle wird beim Anlegen erzeugt und nur dann ausgegeben (niemals gespeichert).

Verwendung:
    python manage.py setup_demo_praesentation                    # Profil nrw
    python manage.py setup_demo_praesentation --profil hamburg
    python manage.py setup_demo_praesentation --reset
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from apps.common.management.commands.setup_demo_environment import (
    DEMO_EMAIL_DOMAIN,
    DEMO_ORG_SLUG,
    DEMO_SESSION_SLUG,
    DEMO_START_DATE,
    DEMO_USERS,
)

if TYPE_CHECKING:
    from apps.accounts.models import User
    from apps.session.models import (
        SessionAgendaItem,
        SessionMeeting,
        SessionOrganization,
        SessionPaper,
        SessionPerson,
        SessionTenant,
        SessionUser,
    )
    from apps.tenants.models import Membership, Organization
    from apps.work.motions.models import Motion
    from insight_core.models import OParlBody

# ---------------------------------------------------------------------------
# Profile und feste Kennungen (Grundlage der Idempotenz und des --reset)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Profil:
    """Wahlperiode und Nummernkreis-Preset eines Profils (siehe docs/SESSION_NUMMERNKREISE.md)."""

    preset: str
    wahlperiode: str
    nummer: int
    beginn: date


PROFILE: dict[str, Profil] = {
    "hamburg": Profil("hamburg_bezirk", "22. Wahlperiode", 22, date(2024, 6, 9)),
    "nrw": Profil("nrw_verwaltung_politik", "Wahlperiode 2025–2030", 1, date(2025, 11, 1)),
}

MANDANT_B_SLUG = "bezirksamt-musterstadt-sued-demo"
MANDANT_B_NAME = "Bezirksamt Musterstadt-Süd (Demo)"
LEITSTELLE = {
    "email": f"demo-leitstelle@{DEMO_EMAIL_DOMAIN}",
    "first_name": "Leo",
    "last_name": "Leitstelle",
}
TOKEN_NAME = "Demo-Drehbuch: Einreichung der Musterfraktion"

ANTRAG_TITEL = "Antrag: Tempo 30 vor der Grundschule am Musterweg (Demo)"
ANTRAG_INHALT = (
    "<h2>Antrag: Tempo 30 vor der Grundschule am Musterweg</h2>"
    "<h3>Beschlussvorschlag</h3>"
    "<p>Die Verwaltung wird beauftragt, auf dem Musterweg im Abschnitt vor der Grundschule eine "
    "Geschwindigkeitsbegrenzung auf 30 km/h anzuordnen und den Schulweg mit einer zusätzlichen "
    "Querungshilfe zu sichern.</p>"
    "<h3>Begründung</h3>"
    "<p>Der Musterweg ist der Hauptzugang zur Grundschule. Zu Schulbeginn und Schulschluss queren dort "
    "täglich rund 250 Kinder die Fahrbahn. Eine Zählung der Elternvertretung zeigt, dass die zulässige "
    "Geschwindigkeit von 50 km/h regelmäßig überschritten wird. Tempo 30 verkürzt den Anhalteweg und "
    "mindert die Unfallschwere deutlich.</p>"
    "<h3>Finanzielle Auswirkungen</h3>"
    "<p>Beschilderung und Markierung ca. 4.500 Euro brutto, Querungshilfe ca. 18.000 Euro brutto aus dem "
    "Budget für Verkehrssicherheit.</p>"
)

SITZUNG_KOMMEND = "Hauptausschuss (Demo-Drehbuch)"
SITZUNG_VERGANGEN = "Hauptausschuss (Demo-Drehbuch, vergangen)"
SITZUNG_VORBERATUNG = "Bauausschuss (Demo-Drehbuch, Vorberatung)"
SITZUNG_B = "Regionalausschuss (Demo-Drehbuch)"
DREHBUCH_SITZUNGEN = (SITZUNG_KOMMEND, SITZUNG_VERGANGEN, SITZUNG_VORBERATUNG)

TOP_EROEFFNUNG = "Eröffnung"
TOP_JUGENDZENTRUM = "Anmietung von Räumen für das Jugendzentrum"
TOP_SPIELPLATZ = "Sanierung Spielplatz Stadtpark"
TOP_GRUNDSTUECK = "Grundstücksangelegenheit Flurstück 12/3"
BESCHLUSS_LASTENRAD = "Beschaffung von Lastenrädern für den Bauhof"
BESCHLUSS_HALTESTELLE = "Barrierefreier Umbau der Bushaltestelle Marktplatz"
TOP_REINIGUNG = "Vergabe der Reinigungsleistungen im Rathaus"

#: Gremien der Basisdemo in Mandant A (natürlicher Schlüssel: Name im Mandanten)
GREMIUM_HA = "Hauptausschuss"
GREMIUM_BAU = "Ausschuss für Bauen und Verkehr"
GREMIUM_REGIONAL = "Regionalausschuss"

#: Personen der Basisdemo (Schlüssel wie in setup_demo_environment)
PERSONEN = {
    "anna-amberg": ("Anna", "Amberg"),
    "bernd-birkholz": ("Bernd", "Birkholz"),
    "dieter-dahl": ("Dieter", "Dahl"),
    "elif-erden": ("Elif", "Erden"),
    "gisela-grote": ("Gisela", "Grote"),
    "hakan-heller": ("Hakan", "Heller"),
}
#: Zusätzliche Sitze im Hauptausschuss – die Basisdemo besetzt nur drei, eine Abstimmung mit
#: Ja, Nein und Enthaltung braucht mehr Stimmen
HA_ZUSATZ = ("dieter-dahl", "gisela-grote", "hakan-heller")

#: Sitzungsgeld-Sätze des Hauptausschusses je Funktion
SAETZE_HA = {"chair": Decimal("60.00"), "deputy_chair": Decimal("45.00"), "member": Decimal("35.00")}


@dataclass(frozen=True)
class Vorlage:
    """Drehbuch-Vorlage; der Betreff ist der natürliche Schlüssel im Mandanten, die Nummer vergibt der Kreis."""

    name: str
    status: str
    gremium: str
    sachverhalt: str
    beschlussvorschlag: str
    kosten: str = ""  # leer = keine finanziellen Auswirkungen
    oeffentlich: bool = True
    alter_tage: int = 30


VORLAGEN_A = (
    Vorlage(
        TOP_JUGENDZENTRUM,
        "scheduled",
        GREMIUM_HA,
        "Die offene Jugendarbeit zählt inzwischen über 80 Besuche am Tag, Gruppenräume fehlen. Im Erdgeschoss "
        "des Gebäudes Musterweg 12 stehen rund 240 m² leer. Die Verwaltung schlägt vor, die Räume für zunächst "
        "fünf Jahre anzumieten.",
        "Der Hauptausschuss beschließt die Anmietung der Räume im Gebäude Musterweg 12 für das Jugendzentrum "
        "zu den in der Anlage genannten Bedingungen.",
        "Jährliche Miete 28.800 € brutto zuzüglich Nebenkosten, Deckung im Teilhaushalt Jugend.",
        alter_tage=28,
    ),
    Vorlage(
        TOP_SPIELPLATZ,
        "scheduled",
        GREMIUM_HA,
        "Für die Sanierung des Spielplatzes am Stadtpark liegt die Ausführungsplanung vor: inklusive "
        "Spielgeräte, neue Fallschutzflächen und eine barrierefreie Wegeführung.",
        "Der Hauptausschuss stimmt der Ausführungsplanung zu und beauftragt die Verwaltung mit der Ausschreibung.",
        "Gesamtkosten 180.000 € brutto aus dem Budget für Spiel- und Freiflächen.",
        alter_tage=21,
    ),
    Vorlage(
        TOP_GRUNDSTUECK,
        "scheduled",
        GREMIUM_HA,
        "Die Stadt kann das Flurstück 12/3 für die Erweiterung des Bauhofs erwerben. Kaufpreis und "
        "Verhandlungsspielraum stehen im vertraulichen Vermerk.",
        "Der Hauptausschuss ermächtigt die Verwaltung, die Kaufverhandlungen im Rahmen des vertraulichen "
        "Vermerks zu führen.",
        "Kaufpreis gemäß vertraulichem Vermerk.",
        oeffentlich=False,
        alter_tage=14,
    ),
    Vorlage(
        BESCHLUSS_LASTENRAD,
        "completed",
        GREMIUM_HA,
        "Der Bauhof erledigt viele kurze Wege in der Innenstadt mit Kleintransportern. Drei E-Lastenräder "
        "können einen Teil dieser Fahrten ersetzen.",
        "Der Hauptausschuss beschließt die Beschaffung von drei E-Lastenrädern für den Bauhof.",
        "ca. 24.000 € brutto, Förderquote 40 Prozent.",
        alter_tage=35,
    ),
    Vorlage(
        BESCHLUSS_HALTESTELLE,
        "completed",
        GREMIUM_HA,
        "Die Bushaltestelle Marktplatz ist nicht barrierefrei: Die Bordhöhe beträgt 12 cm, ein Leitsystem für "
        "blinde und sehbehinderte Menschen fehlt.",
        "Der Hauptausschuss beschließt den barrierefreien Umbau der Bushaltestelle Marktplatz.",
        "ca. 65.000 € brutto, Landesförderung ist beantragt.",
        alter_tage=35,
    ),
)

VORLAGEN_B = (
    Vorlage(
        "Neugestaltung des Marktplatzes Süd",
        "approved",
        GREMIUM_REGIONAL,
        "Der Marktplatz im Stadtteil Süd soll mehr Schatten, Sitzgelegenheiten und einen Trinkbrunnen bekommen.",
        "Der Regionalausschuss beschließt die Vorplanung zur Neugestaltung des Marktplatzes Süd.",
        "Planungskosten 35.000 € brutto.",
        alter_tage=12,
    ),
    Vorlage(
        "Radverkehrskonzept für den Stadtteil Süd",
        "approved",
        GREMIUM_REGIONAL,
        "Das Radverkehrskonzept bündelt Lückenschlüsse, Abstellanlagen und sichere Schulwege im Stadtteil Süd.",
        "Der Regionalausschuss nimmt das Radverkehrskonzept zustimmend zur Kenntnis.",
        alter_tage=8,
    ),
)


@dataclass
class Top:
    """Tagesordnungspunkt; der Betreff ist der natürliche Schlüssel in der Sitzung."""

    name: str
    vorlage: SessionPaper | None = None
    oeffentlich: bool = True
    felder: dict[str, Any] = field(default_factory=dict)


@dataclass
class Welt:
    """Was die Ausgabe (Links, Spickzettel) und die Tests brauchen."""

    mandant_a: SessionTenant
    mandant_b: SessionTenant
    antrag: Motion
    sitzung_kommend: SessionMeeting
    sitzung_vergangen: SessionMeeting
    top_jugendzentrum: SessionAgendaItem
    beschluss: SessionAgendaItem


def inprozess_abruf(basis_url: str) -> Callable[[str], dict[str, Any]]:
    """
    ``fetch`` für den Bürgerportal-Spiegel: OParl-URLs im eigenen Prozess abrufen.

    Der Django-Test-Client ruft die Session-OParl-API ohne HTTP und ohne laufenden Server ab. Als
    Host geht der Host der Quell-URL (``SITE_URL``) mit – die gespiegelten Kennungen sind damit
    dieselben, die auch der Ingestor beim Abruf von außen erzeugt.
    """
    from django.test import Client

    basis = urlsplit(basis_url)
    client = Client()

    def abruf(url: str) -> dict[str, Any]:
        ziel = urlsplit(url)
        pfad = f"{ziel.path}?{ziel.query}" if ziel.query else ziel.path
        antwort = client.get(
            pfad,
            headers={"host": basis.netloc or "localhost", "accept": "application/json"},
            secure=basis.scheme == "https",
        )
        if antwort.status_code != 200:
            raise CommandError(f"Spiegel: {url} lieferte HTTP {antwort.status_code}.")
        return cast(dict[str, Any], antwort.json())

    return abruf


class Command(BaseCommand):
    help = (
        "Setzt auf der Demo-Umgebung die Präsentation „Demo-Drehbuch“ auf: eine Drucksache von der "
        "Fraktion über den Sitzungsdienst bis ins Bürgerportal und die OParl-Schnittstelle."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--profil",
            choices=sorted(PROFILE),
            default="nrw",
            help="Nummernkreis und Wahlperiode: hamburg (Drucksache 22-0001) oder nrw (0001/2026, AN/0001/2026).",
        )
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Entfernt Mandant B, Leitstelle, Drehbuch-Daten, Verbindung, Token und Spiegel; die Basisdemo bleibt.",
        )

    # ------------------------------------------------------------------
    # Einstieg
    # ------------------------------------------------------------------

    def handle(self, *args: Any, **options: Any) -> None:
        from apps.session.models import SessionTenant

        if options["reset"]:
            with transaction.atomic():
                self._reset()
            return

        profil_name = str(options["profil"])
        profil = PROFILE[profil_name]
        self.passwoerter: dict[str, str] = {}
        self.hinweise: list[str] = []

        if not SessionTenant.objects.filter(slug=DEMO_SESSION_SLUG).exists():
            self.stdout.write("Demo-Mandant fehlt – die Basisdemo wird zuerst angelegt.")
            call_command("setup_demo_environment", stdout=self.stdout, stderr=self.stderr)

        with transaction.atomic():
            welt = self._aufbauen(profil)
        # Erst nach dem Commit spiegeln: Der Spiegel liest die OParl-API wie ein externer Client
        kommune = self._spiegeln(welt.mandant_a)
        self._ausgeben(welt, kommune, profil_name)

    # ------------------------------------------------------------------
    # Aufbau
    # ------------------------------------------------------------------

    def _aufbauen(self, profil: Profil) -> Welt:
        from apps.session.models import SessionTenant
        from apps.tenants.models import Membership, Organization

        mandant_a = SessionTenant.objects.filter(slug=DEMO_SESSION_SLUG).first()
        fraktion = Organization.objects.filter(slug=DEMO_ORG_SLUG).first()
        vorsitz = Membership.objects.filter(
            organization__slug=DEMO_ORG_SLUG, user__email=DEMO_USERS["vorsitz"]["email"]
        ).first()
        if mandant_a is None or fraktion is None or vorsitz is None:
            raise CommandError(
                "Die Basisdemo ist unvollständig – bitte zuerst `python manage.py setup_demo_environment` ausführen."
            )
        verwaltung = self._session_user(mandant_a, "verwaltung")

        self._profil_anwenden(mandant_a, profil)
        mandant_a.insight_publish = True
        mandant_a.implementation_publish = True
        # Der Veröffentlichungs-Schalter registriert die OParl-Quelle (apps/session/signals.py)
        cast(Any, mandant_a).save(update_fields=["insight_publish", "implementation_publish", "updated_at"])

        mandant_b = self._mandant_b(profil)
        zugaenge = self._leitstelle([mandant_a, mandant_b])
        self._inhalte_b(mandant_b, zugaenge[mandant_b.pk])
        self._verbindung(mandant_a, fraktion, vorsitz, verwaltung)
        antrag = self._antrag(fraktion, vorsitz)
        return self._drehbuch(mandant_a, antrag, mandant_b)

    def _session_user(self, mandant: SessionTenant, key: str) -> SessionUser:
        from apps.session.models import SessionUser

        zugang = SessionUser.objects.filter(tenant=mandant, user__email=DEMO_USERS[key]["email"]).first()
        if zugang is None:
            raise CommandError(
                f"Demo-Nutzer {DEMO_USERS[key]['email']} fehlt im Mandanten {mandant.slug} – "
                "bitte `python manage.py setup_demo_environment` ausführen."
            )
        return zugang

    def _profil_anwenden(self, mandant: SessionTenant, profil: Profil) -> None:
        """Wahlperiode des Profils anlegen, die des anderen Profils entfernen, Preset übernehmen."""
        from apps.session.models import SessionLegislativeTerm
        from apps.session.services import numbering_service

        andere = [p.wahlperiode for p in PROFILE.values() if p.wahlperiode != profil.wahlperiode]
        SessionLegislativeTerm.objects.filter(tenant=mandant, name__in=andere).delete()
        SessionLegislativeTerm.objects.update_or_create(
            tenant=mandant,
            name=profil.wahlperiode,
            defaults={"number": profil.nummer, "start_date": profil.beginn},
        )
        vorher = sorted(mandant.number_ranges.filter(is_active=True).values_list("pattern", flat=True))
        numbering_service.apply_preset(mandant, profil.preset)
        nachher = sorted(mandant.number_ranges.filter(is_active=True).values_list("pattern", flat=True))
        drehbuch = [v.name for v in (*VORLAGEN_A, *VORLAGEN_B)]
        if vorher != nachher and mandant.papers.filter(name__in=drehbuch).exists():
            self.hinweise.append(
                f"{mandant.name}: Nummernkreis gewechselt – bereits vergebene Nummern bleiben. "
                "Für durchgängig neue Nummern vorher mit --reset aufräumen."
            )

    def _mandant_b(self, profil: Profil) -> SessionTenant:
        from apps.session.models import SessionRole, SessionTenant

        mandant_b, _ = SessionTenant.objects.update_or_create(
            slug=MANDANT_B_SLUG,
            defaults={
                "name": MANDANT_B_NAME,
                "short_name": "Musterstadt-Süd (Demo)",
                "description": "Zweiter Demo-Mandant mit eigener Drucksachen-Zählung. Alle Daten sind synthetisch.",
                "is_active": True,
            },
        )
        if not mandant_b.roles.exists():
            SessionRole.create_default_roles(mandant_b)
        self._profil_anwenden(mandant_b, profil)
        return mandant_b

    def _leitstelle(self, mandanten: list[SessionTenant]) -> dict[Any, SessionUser]:
        """Leitstellen-Nutzer mit Administratorrolle in allen genannten Mandanten."""
        from apps.accounts.models import User
        from apps.session.models import SessionUser

        nutzer, angelegt = User.objects.get_or_create(
            email=LEITSTELLE["email"],
            defaults={
                "first_name": LEITSTELLE["first_name"],
                "last_name": LEITSTELLE["last_name"],
                "is_active": True,
                "email_verified": True,
            },
        )
        from apps.common.demo import demo_passwort

        # Öffentliche Demo-Instanz: festes Passwort bei jedem Lauf (Issue #99); sonst nur bei Neuanlage
        fest = demo_passwort()
        if angelegt or fest:
            passwort = fest or secrets.token_urlsafe(14)
            nutzer.set_password(passwort)
            nutzer.save(update_fields=["password"])
            self.passwoerter[nutzer.email] = passwort
        self._zweiten_faktor_pruefen(nutzer)

        zugaenge: dict[Any, SessionUser] = {}
        for mandant in mandanten:
            zugang, _ = SessionUser.objects.update_or_create(user=nutzer, tenant=mandant, defaults={"is_active": True})
            admin = mandant.roles.filter(is_admin=True).order_by("-priority").first()
            if admin is not None:
                zugang.roles.set([admin])
            zugaenge[mandant.pk] = zugang
        return zugaenge

    def _zweiten_faktor_pruefen(self, nutzer: User) -> None:
        from apps.accounts.two_factor_policy import two_factor_required

        if two_factor_required(nutzer):
            self.hinweise.append(
                f"{nutzer.email} muss einen zweiten Faktor einrichten: Die Domain {DEMO_EMAIL_DOMAIN} fehlt in "
                "TWO_FACTOR_EXEMPT_EMAIL_DOMAINS."
            )

    def _gremien(self, mandant: SessionTenant, namen: dict[str, str]) -> dict[str, SessionOrganization]:
        from apps.session.models import SessionOrganization

        gremien: dict[str, SessionOrganization] = {}
        for name, kurz in namen.items():
            gremien[name], _ = SessionOrganization.objects.update_or_create(
                tenant=mandant,
                name=name,
                defaults={
                    "short_name": kurz,
                    "organization_type": "committee",
                    "default_meeting_location": "Bezirksamt Süd",
                    "is_active": True,
                },
            )
        return gremien

    def _inhalte_b(self, mandant_b: SessionTenant, leitstelle: SessionUser) -> None:
        """Mandant B: zwei Gremien, zwei Vorlagen mit eigener Nummernfolge, eine kommende Sitzung."""
        gremien = self._gremien(mandant_b, {"Hauptausschuss": "HA", GREMIUM_REGIONAL: "RegA"})
        vorlagen = [self._vorlage(mandant_b, v, gremien, leitstelle, leitstelle) for v in VORLAGEN_B]
        sitzung = self._sitzung(
            mandant_b, gremien[GREMIUM_REGIONAL], SITZUNG_B, self._termin(10), "scheduled", leitstelle
        )
        self._tagesordnung(sitzung, [Top(TOP_EROEFFNUNG), *(Top(v.name, v) for v in vorlagen)])

    def _verbindung(
        self, mandant: SessionTenant, fraktion: Organization, vorsitz: Membership, verwaltung: SessionUser
    ) -> None:
        """Einreichungs-Token für Mandant A ausstellen und die Musterfraktion damit verbinden."""
        from apps.session.models import SessionAPIToken
        from apps.work.motions import ris_submission
        from apps.work.motions.models import AdministrationConnection

        bestehend = AdministrationConnection.objects.filter(
            organization=fraktion, tenant=mandant, is_active=True
        ).first()
        if bestehend is not None and ris_submission.connection_state(bestehend)[0]:
            return  # bestehende, nutzbare Verbindung wiederverwenden
        SessionAPIToken.objects.filter(tenant=mandant, name=TOKEN_NAME).delete()
        _token, roh = SessionAPIToken.create_token(
            mandant,
            TOKEN_NAME,
            can_submit_applications=True,
            can_read_meetings=True,
            can_read_papers=True,
            description="Einreichungs-Token der Präsentationsumgebung (setup_demo_praesentation).",
            created_by=verwaltung,
        )
        ris_submission.connect_with_token(fraktion, roh, vorsitz)

    def _antrag(self, fraktion: Organization, vorsitz: Membership) -> Motion:
        """Work-Antrag im Entwurf – ein erneuter Lauf nimmt eine Einreichung aus der Probe zurück."""
        from apps.work.motions.models import Motion

        vorhanden = Motion.objects.filter(organization=fraktion, title=ANTRAG_TITEL).first()
        if vorhanden is not None:
            self._einreichung_zuruecknehmen(vorhanden)
        antrag, _ = Motion.objects.update_or_create(
            organization=fraktion,
            title=ANTRAG_TITEL,
            defaults={
                "motion_type": "motion",
                "status": "draft",
                "visibility": "organization",
                "author": vorsitz,
                "responsible": vorsitz,
                "summary": "Entwurf: Tempo 30 und eine Querungshilfe vor der Grundschule am Musterweg.",
                "folder": None,
                "session_application": None,
                "administration_status": "",
                "submitted_at": None,
            },
        )
        cast(Any, antrag).set_content_encrypted(ANTRAG_INHALT)
        antrag.save()
        return antrag

    def _einreichung_zuruecknehmen(self, antrag: Motion) -> None:
        """Eingang in Session samt daraus umgewandelter Vorlage löschen (Probe zurücksetzen)."""
        antrag.administration_events.all().delete()
        eingang = antrag.session_application
        if eingang is None:
            return
        for vorlage in eingang.created_papers.all():
            vorlage.delete()
        eingang.delete()

    # ------------------------------------------------------------------
    # Bausteine
    # ------------------------------------------------------------------

    @staticmethod
    def _termin(tage: int) -> datetime:
        return (timezone.localtime() + timedelta(days=tage)).replace(hour=17, minute=0, second=0, microsecond=0)

    def _vorlage(
        self,
        mandant: SessionTenant,
        vorlage: Vorlage,
        gremien: dict[str, SessionOrganization],
        ersteller: SessionUser,
        freigabe: SessionUser,
    ) -> SessionPaper:
        """Vorlage ohne Nummer anlegen – die Nummer vergibt der Nummernkreis beim ersten Speichern."""
        from apps.session.models import SessionPaper

        datum = timezone.localdate() - timedelta(days=vorlage.alter_tage)
        paper, _ = SessionPaper.objects.update_or_create(
            tenant=mandant,
            name=vorlage.name,
            defaults={
                "paper_type": "proposal",
                "status": vorlage.status,
                "is_public": vorlage.oeffentlich,
                "main_text": vorlage.sachverhalt,
                "resolution_text": vorlage.beschlussvorschlag,
                "date": datum,
                "main_organization": gremien[vorlage.gremium],
                "has_financial_impact": bool(vorlage.kosten),
                "financial_impact_note": vorlage.kosten,
                "created_by": ersteller,
                "approved_by": freigabe,
                "approved_at": timezone.make_aware(datetime.combine(datum, datetime.min.time())),
            },
        )
        return paper

    def _sitzung(
        self,
        mandant: SessionTenant,
        gremium: SessionOrganization,
        name: str,
        beginn: datetime,
        status: str,
        ersteller: SessionUser,
        **weitere: Any,
    ) -> SessionMeeting:
        from apps.session.models import SessionMeeting

        ort, strasse = ("Rathaus Musterstadt", "Rathausplatz 1")
        if mandant.slug != DEMO_SESSION_SLUG:
            ort, strasse = ("Bezirksamt Süd", "Marktplatz 5")
        sitzung, _ = SessionMeeting.objects.update_or_create(
            tenant=mandant,
            organization=gremium,
            name=name,
            defaults={
                "start": beginn,
                "end": beginn + timedelta(hours=3),
                "location": ort,
                "room": "Sitzungssaal 1",
                "street_address": strasse,
                "postal_code": "12345",
                "locality": "Musterstadt",
                "meeting_state": status,
                "is_public": True,
                "cancelled": False,
                "created_by": ersteller,
                **weitere,
            },
        )
        return sitzung

    def _tagesordnung(self, sitzung: SessionMeeting, tops: list[Top]) -> dict[str, SessionAgendaItem]:
        """TOPs in der angegebenen Reihenfolge; Nummern (Ö 1..n, NÖ N1..) vergibt renumber_agenda."""
        from apps.session.models import SessionAgendaItem
        from apps.session.services import agenda_service

        punkte: dict[str, SessionAgendaItem] = {}
        for reihenfolge, top in enumerate(tops, start=1):
            punkte[top.name], _ = SessionAgendaItem.objects.update_or_create(
                meeting=sitzung,
                name=top.name,
                parent=None,
                defaults={
                    "order": reihenfolge,
                    "number": str(reihenfolge) if top.oeffentlich else f"N{reihenfolge}",
                    "is_public": top.oeffentlich,
                    "is_withdrawn": False,
                    "paper": top.vorlage,
                    **top.felder,
                },
            )
        agenda_service.renumber_agenda(sitzung)
        for punkt in punkte.values():
            punkt.refresh_from_db()
        return punkte

    def _beratung(
        self,
        vorlage: SessionPaper,
        gremium: SessionOrganization,
        top: SessionAgendaItem,
        rolle: str,
        reihenfolge: int,
        ergebnis: str = "pending",
    ) -> None:
        """Station der Beratungsfolge, terminiert auf einen TOP (natürlicher Schlüssel: Vorlage + Gremium)."""
        from apps.session.models import SessionConsultation

        SessionConsultation.objects.update_or_create(
            paper=vorlage,
            organization=gremium,
            defaults={
                "role": rolle,
                "authoritative": rolle == "decision",
                "order": reihenfolge,
                "meeting": top.meeting,
                "agenda_item": top,
                "result": ergebnis,
            },
        )

    # ------------------------------------------------------------------
    # Drehbuch in Mandant A
    # ------------------------------------------------------------------

    def _drehbuch(self, mandant: SessionTenant, antrag: Motion, mandant_b: SessionTenant) -> Welt:
        from apps.session.models import SessionOrganization, SessionOrganizationMembership, SessionPerson

        verwaltung = self._session_user(mandant, "verwaltung")
        sachbearbeitung = self._session_user(mandant, "sachbearbeitung")
        protokoll = self._session_user(mandant, "protokoll")
        gremien = {
            g.name: g for g in SessionOrganization.objects.filter(tenant=mandant, name__in=[GREMIUM_HA, GREMIUM_BAU])
        }
        personen: dict[str, SessionPerson] = {}
        for key, (vorname, nachname) in PERSONEN.items():
            person = SessionPerson.objects.filter(tenant=mandant, given_name=vorname, family_name=nachname).first()
            if person is not None:
                personen[key] = person
        if len(gremien) != 2 or len(personen) != len(PERSONEN):
            raise CommandError(
                "Gremien oder Personen der Basisdemo fehlen – bitte `python manage.py setup_demo_environment` "
                "ausführen."
            )
        ha, bau = gremien[GREMIUM_HA], gremien[GREMIUM_BAU]

        for key in HA_ZUSATZ:
            SessionOrganizationMembership.objects.update_or_create(
                organization=ha,
                person=personen[key],
                start_date=DEMO_START_DATE,
                defaults={"role": "member", "has_voting_rights": True},
            )

        vorlagen = {v.name: self._vorlage(mandant, v, gremien, sachbearbeitung, verwaltung) for v in VORLAGEN_A}
        geheim = vorlagen[TOP_GRUNDSTUECK]
        cast(Any, geheim).set_confidential_text_encrypted(
            "Vertraulicher Vermerk (Demo): Verhandlungsspielraum bis 95 € je m², Gutachten liegt vor."
        )
        geheim.save()

        # --- Vorberatung im Bauausschuss (vergangen, Empfehlung) -------
        vorberatung = self._sitzung(mandant, bau, SITZUNG_VORBERATUNG, self._termin(-10), "completed", verwaltung)
        tops_bau = self._tagesordnung(
            vorberatung,
            [
                Top(TOP_EROEFFNUNG),
                Top(
                    TOP_JUGENDZENTRUM,
                    vorlagen[TOP_JUGENDZENTRUM],
                    felder={
                        "vote_result": "approved",
                        "votes_yes": 3,
                        "votes_no": 0,
                        "votes_abstain": 0,
                        "resolution_text": "Der Ausschuss für Bauen und Verkehr empfiehlt dem Hauptausschuss, "
                        "der Anmietung zuzustimmen.",
                    },
                ),
            ],
        )

        # --- Kommende Sitzung (Einladung fristgerecht versandt) --------
        beginn = self._termin(7)
        kommend = self._sitzung(
            mandant,
            ha,
            SITZUNG_KOMMEND,
            beginn,
            "invitation_sent",
            verwaltung,
            # Ladungsfrist des Gremiums (Geschäftsordnung) plus einen Tag Puffer
            invitation_sent_at=beginn - timedelta(days=(ha.invitation_period_days or 0) + 1),
            invitation_text="Hiermit lade ich zur Sitzung des Hauptausschusses ein. Die Unterlagen stehen im "
            "Ratsinformationssystem bereit.",
        )
        tops_kommend = self._tagesordnung(
            kommend,
            [
                Top(TOP_EROEFFNUNG),
                Top(TOP_JUGENDZENTRUM, vorlagen[TOP_JUGENDZENTRUM]),
                Top(TOP_SPIELPLATZ, vorlagen[TOP_SPIELPLATZ]),
                Top(TOP_GRUNDSTUECK, vorlagen[TOP_GRUNDSTUECK], oeffentlich=False),
            ],
        )

        # --- Beratungsfolge: Bauausschuss (Vorberatung) → Hauptausschuss (Entscheidung)
        jugendzentrum = vorlagen[TOP_JUGENDZENTRUM]
        self._beratung(jugendzentrum, bau, tops_bau[TOP_JUGENDZENTRUM], "preliminary", 1, "approved")
        self._beratung(jugendzentrum, ha, tops_kommend[TOP_JUGENDZENTRUM], "decision", 2)
        # Die übrigen Vorlagen haben eine Station – so verlinkt auch das Bürgerportal TOP und Vorgang
        for name in (TOP_SPIELPLATZ, TOP_GRUNDSTUECK):
            self._beratung(vorlagen[name], ha, tops_kommend[name], "decision", 1)

        vergangen, beschluss = self._vergangene_sitzung(mandant, ha, vorlagen, personen, verwaltung, protokoll)
        return Welt(
            mandant_a=mandant,
            mandant_b=mandant_b,
            antrag=antrag,
            sitzung_kommend=kommend,
            sitzung_vergangen=vergangen,
            top_jugendzentrum=tops_kommend[TOP_JUGENDZENTRUM],
            beschluss=beschluss,
        )

    def _vergangene_sitzung(
        self,
        mandant: SessionTenant,
        ha: SessionOrganization,
        vorlagen: dict[str, SessionPaper],
        personen: dict[str, SessionPerson],
        verwaltung: SessionUser,
        protokoll: SessionUser,
    ) -> tuple[SessionMeeting, SessionAgendaItem]:
        """Anwesenheit, Abstimmung, Protokoll, Beschlusskontrolle und Sitzungsgeld."""
        from apps.session.models import SessionAttendance, SessionProtocol
        from apps.session.services import resolution_service, voting_service

        heute = timezone.localdate()
        jetzt = timezone.now()
        vergangen = self._sitzung(mandant, ha, SITZUNG_VERGANGEN, self._termin(-21), "completed", verwaltung)
        tops = self._tagesordnung(
            vergangen,
            [
                Top(
                    TOP_EROEFFNUNG,
                    felder={
                        "protocol_note": "Der Vorsitz eröffnet die Sitzung und stellt die Beschlussfähigkeit fest."
                    },
                ),
                Top(
                    BESCHLUSS_LASTENRAD,
                    vorlagen[BESCHLUSS_LASTENRAD],
                    felder={
                        "voting_method": "roll_call",
                        "vote_result": "approved",
                        "resolution_text": vorlagen[BESCHLUSS_LASTENRAD].resolution_text,
                        "protocol_note": "Die Verwaltung erläutert Förderung und Einsatzplanung. Namentliche "
                        "Abstimmung auf Antrag eines Mitglieds.",
                        "implementation_status": "in_progress",
                        "implementation_recipient": "Bauhof (Demo)",
                        "implementation_deadline": heute + timedelta(days=14),
                        "implementation_note": "Ausschreibung veröffentlicht, Angebotsfrist läuft.",
                        "implementation_public": True,
                        "implementation_public_note": "Die Ausschreibung läuft, die Räder kommen voraussichtlich "
                        "im nächsten Quartal.",
                        "implementation_updated_at": jetzt - timedelta(days=3),
                        "implementation_updated_by": verwaltung,
                    },
                ),
                Top(
                    BESCHLUSS_HALTESTELLE,
                    vorlagen[BESCHLUSS_HALTESTELLE],
                    felder={
                        "voting_method": "summary",
                        "vote_result": "approved",
                        "votes_yes": 5,
                        "votes_no": 0,
                        "votes_abstain": 0,
                        "resolution_text": vorlagen[BESCHLUSS_HALTESTELLE].resolution_text,
                        "implementation_status": "open",
                        "implementation_recipient": "Tiefbauamt (Demo)",
                        # Frist bereits verstrichen: zeigt Überfällig-Filter und Erinnerung
                        "implementation_deadline": heute - timedelta(days=7),
                        "implementation_public": True,
                        "implementation_updated_at": None,
                        "implementation_updated_by": None,
                    },
                ),
                Top(
                    TOP_REINIGUNG,
                    oeffentlich=False,
                    felder={"vote_result": "approved", "votes_yes": 4, "votes_no": 0, "votes_abstain": 1},
                ),
            ],
        )
        for name in (BESCHLUSS_LASTENRAD, BESCHLUSS_HALTESTELLE):
            self._beratung(vorlagen[name], ha, tops[name], "decision", 1, "approved")
        reinigung = tops[TOP_REINIGUNG]
        cast(Any, reinigung).set_resolution_text_encrypted(
            "Der Auftrag wird an den wirtschaftlichsten Bieter (Angebot Nr. 2) vergeben."
        )
        cast(Any, reinigung).set_protocol_note_encrypted("Beratung der Angebote unter Ausschluss der Öffentlichkeit.")
        reinigung.save()

        # --- Anwesenheit und namentliche Abstimmung --------------------
        anwesenheit = [
            ("anna-amberg", "present", "chair"),
            ("elif-erden", "present", "deputy_chair"),
            ("bernd-birkholz", "present", "member"),
            ("dieter-dahl", "present", "member"),
            ("gisela-grote", "present", "member"),
            ("hakan-heller", "excused", "member"),
        ]
        for key, status, rolle in anwesenheit:
            SessionAttendance.objects.update_or_create(
                meeting=vergangen,
                person=personen[key],
                defaults={
                    "status": status,
                    "role": rolle,
                    "has_voting_rights": True,
                    "excuse_reason": "Terminüberschneidung (Demo)" if status == "excused" else "",
                },
            )
        lastenrad = tops[BESCHLUSS_LASTENRAD]
        voting_service.capture_votes(
            lastenrad,
            {
                personen["anna-amberg"]: "yes",
                personen["elif-erden"]: "yes",
                personen["bernd-birkholz"]: "no",
                personen["dieter-dahl"]: "yes",
                personen["gisela-grote"]: "abstain",
            },
            recorded_by=protokoll,
        )
        # Beschlussnummern (B/<Jahr>/<lfd>) – einmal vergeben, bleiben bei jedem weiteren Lauf
        resolution_service.ensure_numbers_for_meeting(vergangen)

        # --- Protokoll: Ö-Teil im Klartext, NÖ-Teil verschlüsselt ------
        niederschrift, _ = SessionProtocol.objects.update_or_create(
            meeting=vergangen,
            defaults={
                "content": (
                    "Sitzung des Hauptausschusses (Demo-Drehbuch).\n\n"
                    f"TOP 1 – {TOP_EROEFFNUNG}: Der Vorsitz stellt die ordnungsgemäße Ladung und die "
                    "Beschlussfähigkeit fest.\n\n"
                    f"TOP 2 – {BESCHLUSS_LASTENRAD}: Namentliche Abstimmung, angenommen mit 3 Ja-Stimmen, "
                    "1 Nein-Stimme und 1 Enthaltung.\n\n"
                    f"TOP 3 – {BESCHLUSS_HALTESTELLE}: Einstimmig angenommen."
                ),
                "status": "approved",
                "created_by": protokoll,
                "approved_by": verwaltung,
                "approved_at": jetzt - timedelta(days=14),
                "approval_note": "Genehmigt durch den Vorsitz (Demo).",
                "chair_name": personen["anna-amberg"].display_name,
                "recorder_name": f"{protokoll.user.first_name} {protokoll.user.last_name}".strip(),
            },
        )
        cast(Any, niederschrift).set_content_encrypted(
            f"Nichtöffentlicher Teil – TOP N1 {TOP_REINIGUNG}: Der Ausschuss vergibt den Auftrag an den "
            "wirtschaftlichsten Bieter (4 Ja-Stimmen, 1 Enthaltung)."
        )
        niederschrift.save()

        self._sitzungsgeld(mandant, ha, vergangen, verwaltung)
        return vergangen, lastenrad

    def _sitzungsgeld(
        self, mandant: SessionTenant, ha: SessionOrganization, vergangen: SessionMeeting, verwaltung: SessionUser
    ) -> None:
        """Sätze des Hauptausschusses und offene Positionen, erzeugt von der Verwaltung (Vier-Augen-Prinzip)."""
        from apps.session.models import SessionAllowance, SessionAllowanceRate
        from apps.session.services import allowance_service

        for rolle, betrag in SAETZE_HA.items():
            SessionAllowanceRate.objects.update_or_create(
                organization=ha, role=rolle, defaults={"amount": betrag, "currency": "EUR"}
            )
        # Probe zurücksetzen: Positionen wieder offen und von der Verwaltung erzeugt
        for position in SessionAllowance.objects.filter(attendance__meeting=vergangen):
            if position.status == "pending" and position.created_by_id == verwaltung.pk:
                continue
            position.status = "pending"
            position.created_by = verwaltung
            position.approved_by = None
            position.approved_at = None
            position.paid_at = None
            position.export_reference = ""
            position.export_date = None
            position.save()
        tag = timezone.localtime(vergangen.start).date()
        allowance_service.generate_allowances(mandant, tag, tag, organization=ha, created_by=verwaltung)

    # ------------------------------------------------------------------
    # Spiegel ins Bürgerportal
    # ------------------------------------------------------------------

    def _spiegeln(self, mandant: SessionTenant) -> OParlBody:
        from apps.session.services import insight_service
        from insight_core.models import OParlBody
        from insight_sync.session_mirror import SessionMirror

        quelle, _ = insight_service.register_source(mandant)
        spiegel = cast(Any, SessionMirror)(quelle, fetch=inprozess_abruf(quelle.url))
        statistik = spiegel.sync(full=True)
        kommune: OParlBody | None = OParlBody.objects.filter(source=quelle).first()
        if kommune is None:
            raise CommandError(f"Spiegel: Die Quelle {quelle.url} lieferte keine Kommune.")
        # Per direktem Link erreichbar, aber nicht in der öffentlichen Kommunenauswahl
        kommune.is_listed = False
        if not kommune.slug and not OParlBody.objects.filter(slug=mandant.slug).exclude(pk=kommune.pk).exists():
            kommune.slug = mandant.slug
        kommune.save(update_fields=["is_listed", "slug", "updated_at"])
        zusammenfassung = ", ".join(f"{k}={v}" for k, v in statistik.items() if v)
        self.stdout.write(f"Bürgerportal-Spiegel aktualisiert: {zusammenfassung}")
        return kommune

    # ------------------------------------------------------------------
    # Aufräumen
    # ------------------------------------------------------------------

    def _reset(self) -> None:
        from apps.accounts.models import User
        from apps.session.models import (
            SessionAllowanceRate,
            SessionAPIToken,
            SessionLegislativeTerm,
            SessionMeeting,
            SessionOrganizationMembership,
            SessionPaper,
            SessionTenant,
        )
        from apps.session.services import numbering_service
        from apps.tenants.models import Organization
        from apps.work.motions.models import AdministrationConnection, Motion
        from insight_core.models import OParlSource

        entfernt: list[str] = []
        mandant_a = SessionTenant.objects.filter(slug=DEMO_SESSION_SLUG).first()
        fraktion = Organization.objects.filter(slug=DEMO_ORG_SLUG).first()

        if fraktion is not None:
            antraege = list(Motion.objects.filter(organization=fraktion, title=ANTRAG_TITEL))
            for antrag in antraege:
                self._einreichung_zuruecknehmen(antrag)
                antrag.delete()
            entfernt.append(f"Work-Antrag (samt Einreichung und umgewandelter Vorlage): {len(antraege)}")
            anzahl, _ = AdministrationConnection.objects.filter(
                organization=fraktion, tenant__slug=DEMO_SESSION_SLUG
            ).delete()
            entfernt.append(f"Verbindung Work ↔ Session: {anzahl}")

        if mandant_a is not None:
            anzahl, _ = SessionAPIToken.objects.filter(tenant=mandant_a, name=TOKEN_NAME).delete()
            entfernt.append(f"Einreichungs-Token: {anzahl}")
            anzahl, _ = SessionMeeting.objects.filter(tenant=mandant_a, name__in=DREHBUCH_SITZUNGEN).delete()
            entfernt.append(f"Drehbuch-Sitzungen (+abhängige Objekte): {anzahl}")
            anzahl, _ = SessionPaper.objects.filter(tenant=mandant_a, name__in=[v.name for v in VORLAGEN_A]).delete()
            entfernt.append(f"Drehbuch-Vorlagen (+Beratungsfolge): {anzahl}")
            SessionAllowanceRate.objects.filter(
                organization__tenant=mandant_a, organization__name=GREMIUM_HA, role__in=list(SAETZE_HA)
            ).delete()
            zusatz = Q()
            for key in HA_ZUSATZ:
                zusatz |= Q(person__given_name=PERSONEN[key][0], person__family_name=PERSONEN[key][1])
            SessionOrganizationMembership.objects.filter(
                zusatz, organization__tenant=mandant_a, organization__name=GREMIUM_HA, start_date=DEMO_START_DATE
            ).delete()
            SessionLegislativeTerm.objects.filter(
                tenant=mandant_a, name__in=[p.wahlperiode for p in PROFILE.values()]
            ).delete()
            # Zurück auf den Stand der Basisdemo: Standard-Nummernkreis, keine Veröffentlichung
            numbering_service.apply_preset(mandant_a, "standard")
            mandant_a.insight_publish = False
            mandant_a.implementation_publish = False
            cast(Any, mandant_a).save(update_fields=["insight_publish", "implementation_publish", "updated_at"])

        anzahl, _ = OParlSource.objects.filter(url__contains=f"/session/{DEMO_SESSION_SLUG}/api/oparl/").delete()
        entfernt.append(f"Bürgerportal-Spiegel inkl. Kommune (+abhängige Objekte): {anzahl}")
        anzahl, _ = SessionTenant.objects.filter(slug=MANDANT_B_SLUG).delete()
        entfernt.append(f"Mandant B (+abhängige Objekte): {anzahl}")
        anzahl, _ = User.objects.filter(email=LEITSTELLE["email"]).delete()
        entfernt.append(f"Leitstellen-Nutzer (+Zugänge): {anzahl}")

        self.stdout.write(self.style.SUCCESS("Präsentationsumgebung entfernt (die Basisdemo bleibt):"))
        for zeile in entfernt:
            self.stdout.write(f"  - {zeile}")

    # ------------------------------------------------------------------
    # Ausgabe
    # ------------------------------------------------------------------

    def _ausgeben(self, welt: Welt, kommune: OParlBody, profil_name: str) -> None:
        from insight_core.models import OParlMeeting

        basis = str(getattr(settings, "SITE_URL", "http://localhost:8000")).rstrip("/")
        a, b = welt.mandant_a, welt.mandant_b
        oparl_sitzung = OParlMeeting.objects.filter(
            external_id__endswith=f"/session/{a.slug}/api/oparl/meeting/{welt.sitzung_kommend.id}/"
        ).first()
        tag = timezone.localtime(welt.sitzung_vergangen.start).date().isoformat()

        def link(name: str, **kwargs: Any) -> str:
            return f"{basis}{reverse(name, kwargs=kwargs)}"

        self.stdout.write(self.style.SUCCESS(f"\nPräsentationsumgebung „Demo-Drehbuch“ bereit (Profil {profil_name})."))

        if self.passwoerter:
            self.stdout.write(self.style.WARNING("\nNeue Zugangsdaten (werden NICHT gespeichert – jetzt notieren!):"))
            for email, passwort in self.passwoerter.items():
                self.stdout.write(f"  {email}  ->  {passwort}")
        else:
            self.stdout.write(
                f"\nLeitstelle unverändert ({LEITSTELLE['email']}). Neues Passwort bei Bedarf: "
                f"python manage.py changepassword {LEITSTELLE['email']}"
            )
        self.stdout.write(
            f"Aus der Basisdemo: {DEMO_USERS['vorsitz']['email']} (Fraktionsvorsitz), "
            f"{DEMO_USERS['verwaltung']['email']} (Verwaltung, Administrator in A)"
        )

        einstiege = [
            ("Bürgerportal-Kommune", link("insight_core:insight:set_body", body_id=kommune.id)),
            (
                "Bürgerportal-Sitzung",
                link("insight_core:insight:meeting_detail", pk=oparl_sitzung.id) if oparl_sitzung else "–",
            ),
            ("Bürgerportal-Beschluss", link("insight_core:insight:decision_detail", pk=welt.beschluss.id)),
            ("OParl-System", link("session:oparl_system", tenant_slug=a.slug)),
            ("Work-Antrag", link("work:document_editor", org_slug=DEMO_ORG_SLUG, motion_id=welt.antrag.id)),
            ("Session A", link("session:dashboard", tenant_slug=a.slug)),
            ("Session B", link("session:dashboard", tenant_slug=b.slug)),
        ]
        self.stdout.write("\nEinstiegspunkte:")
        for bezeichnung, adresse in einstiege:
            self.stdout.write(f"  {bezeichnung + ':':<24}{adresse}")

        nummer = "Drucksache 22-…" if profil_name == "hamburg" else "Vorlagen-Nr. AN/…/<Jahr>"
        sitzung = link("session:meeting_detail", tenant_slug=a.slug, meeting_id=welt.sitzung_kommend.id)
        spickzettel = [
            f"1. Work – als {DEMO_USERS['vorsitz']['email']}: Work-Antrag öffnen → Symbol „Bei Verwaltung "
            "einreichen“ → bestätigen → „Jetzt einreichen“. Zeigt die Eingangsnummer A/<Jahr>/… im Editor.",
            f"2. Session – als {DEMO_USERS['verwaltung']['email']}: Anträge → „{ANTRAG_TITEL}“ → „In Vorlage "
            f"umwandeln“ → Gremium prüfen → „In Vorlage umwandeln“. Zeigt die Vorlage mit {nummer}.",
            f"3. Session: {sitzung} → Beratungsfolge am TOP „{TOP_JUGENDZENTRUM}“ (Bauausschuss → "
            "Hauptausschuss) → Stift → „Öffentlicher Tagesordnungspunkt“ abwählen → Speichern. Die "
            "Bürgerportal-Sitzung zeigt den TOP nach dem Neuladen nicht mehr (zweites Fenster, abgemeldet).",
            f"4. OParl: {link('session:oparl_system', tenant_slug=a.slug)} – der TOP ist dort nur noch als "
            "gelöschtes Objekt (deleted) abrufbar.",
            f"5. Beschlusskontrolle: {link('session:resolutions', tenant_slug=a.slug)}?overdue=1 – überfällige "
            "Frist mit Erinnerung, Umsetzungsstand „In Umsetzung“; Niederschrift der vergangenen Sitzung mit "
            "Ö- und NÖ-Teil.",
            "6. Bürgerportal-Beschluss (Link oben): „Was wurde aus …?“ mit öffentlicher Statusmeldung, ohne "
            "internen Vermerk.",
            f"7. Sitzungsgeld: {link('session:allowances', tenant_slug=a.slug)}?from={tag}&to={tag} → „2. "
            "Genehmigen (Vier-Augen)“ – die Verwaltung wird abgewiesen, die Leitstelle genehmigt.",
            f"8. Leitstelle – als {LEITSTELLE['email']}: Seitenleiste „Mandant wechseln“ → "
            f"„{MANDANT_B_NAME}“. Mandant B zählt seine Drucksachen selbst.",
        ]
        self.stdout.write("\nDrehbuch-Spickzettel:")
        for zeile in spickzettel:
            self.stdout.write(f"  {zeile}")

        for hinweis in self.hinweise:
            self.stdout.write(self.style.WARNING(f"\nHinweis: {hinweis}"))
        self.stdout.write(
            "\nProbe zurücksetzen: Befehl erneut ausführen. Aufräumen: python manage.py setup_demo_praesentation --reset\n"
        )
