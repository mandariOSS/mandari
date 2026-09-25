# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Synthetische Lasttest-Daten nach Mengengerüst erzeugen (Issue #228).

Die Mengengerüste ``klein``, ``mittel`` und ``gross`` stehen in
``docs/LASTTESTS.md``. Dieses Kommando erzeugt sie reproduzierbar:

- Session (Verwaltungs-RIS): Mandanten mit Gremien, Personen, Besetzungen,
  Sitzungen über zwei Jahre, Tagesordnungen, Vorlagen, Dateien, Anwesenheiten
  und Einzelstimmen; dazu eine laufende Ratssitzung mit offener Abstimmung.
- Work (Fraktionen): eine Organisation je Mandant mit Mitgliedern und Anträgen.
- Insight (Bürgerportal): eine Kommune mit denselben Gremien, Personen,
  Sitzungen, Vorlagen und Dateien — ohne Ingestor, direkt in den Tabellen.

Alle Objekte tragen die Kennung ``last-<profil>`` in Slugs, E-Mail-Adressen und
OParl-Kennungen (Domäne ``lasttest.mandari.invalid``); Personendaten sind aus
Namenslisten zusammengesetzt und gehören niemandem. Ein wiederholter Lauf
entfernt die Daten des Profils zuerst und legt sie neu an, ``--reset`` entfernt
sie nur. Der Zufall hängt allein am ``--seed``, zwei Läufe mit demselben Seed
liefern denselben Datenbestand.

Das Kommando ist für Entwicklungs- und Testumgebungen gedacht. Außerhalb von
``DEBUG`` bricht es ab, sofern nicht ``--ich-weiss-was-ich-tue`` gesetzt ist.

    python manage.py generate_load_data --profile klein
    python manage.py generate_load_data --profile gross --seed 7
    python manage.py generate_load_data --profile klein --reset
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta
from datetime import time as dt_time
from pathlib import Path
from typing import Any, cast

from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction
from django.utils import timezone

PRAEFIX = "last"
DOMAENE = "lasttest.mandari.invalid"
OPARL_PRAEFIX = f"https://{DOMAENE}/oparl"
#: Ein Passwort für alle Lasttest-Konten — bewusst bekannt, damit Locust sich anmelden kann.
PASSWORT = "Lasttest-2026!"
#: Anteil, den Nebenmandanten (Eigenbetrieb, Zweckverband) vom Hauptmandanten erhalten.
NEBENMANDANT_ANTEIL = 0.1
#: Historie in Jahren; die Mengengerüste nennen Werte je Jahr.
JAHRE = 2
BULK = 500


@dataclass(frozen=True)
class Profil:
    """Ein Mengengerüst, siehe Tabelle in docs/LASTTESTS.md."""

    mandanten: int
    gremien: int
    personen: int
    sitzungen_pro_jahr: int
    vorlagen_pro_jahr: int
    dokumente: int
    nutzer: int
    abstimmende: int
    fraktionsmitglieder: int
    antraege: int
    tops_pro_sitzung: int


PROFILE: dict[str, Profil] = {
    # Kreisangehörige Gemeinde, rund 20.000 Einwohner
    "klein": Profil(
        mandanten=1,
        gremien=6,
        personen=60,
        sitzungen_pro_jahr=40,
        vorlagen_pro_jahr=300,
        dokumente=600,
        nutzer=20,
        abstimmende=30,
        fraktionsmitglieder=8,
        antraege=100,
        tops_pro_sitzung=8,
    ),
    # Mittelstadt, rund 100.000 Einwohner, mit Eigenbetrieb als zweitem Mandanten
    "mittel": Profil(
        mandanten=2,
        gremien=20,
        personen=250,
        sitzungen_pro_jahr=200,
        vorlagen_pro_jahr=1500,
        dokumente=4000,
        nutzer=100,
        abstimmende=60,
        fraktionsmitglieder=15,
        antraege=400,
        tops_pro_sitzung=12,
    ),
    # Großstadt mit Bezirksvertretungen, rund 500.000 Einwohner und mehr
    "gross": Profil(
        mandanten=3,
        gremien=60,
        personen=900,
        sitzungen_pro_jahr=800,
        vorlagen_pro_jahr=6000,
        dokumente=20000,
        nutzer=400,
        abstimmende=90,
        fraktionsmitglieder=25,
        antraege=1500,
        tops_pro_sitzung=12,
    ),
}

VORNAMEN = [
    "Alex", "Bettina", "Cem", "Dana", "Emre", "Frauke", "Gero", "Hanna", "Ilse", "Jonas",
    "Karin", "Lars", "Mira", "Nils", "Ortrud", "Peer", "Quirin", "Rosa", "Sören", "Tessa",
    "Udo", "Vera", "Wim", "Xenia", "Yusuf", "Zoe",
]  # fmt: skip
NACHNAMEN = [
    "Achenbach", "Bergmann", "Conrad", "Dörfler", "Ebert", "Falk", "Gläser", "Hummel", "Ising", "Jung",
    "Kessler", "Lindner", "Möller", "Nolte", "Oster", "Pfeiffer", "Quandt", "Rehm", "Seidel", "Thiel",
    "Ulrich", "Vogt", "Wendt", "Ziegler",
]  # fmt: skip
THEMEN = [
    "Sanierung der Grundschule", "Radwegeausbau", "Haushaltsplan", "Bebauungsplan", "Spielplatz",
    "Straßenbeleuchtung", "Kita-Bedarfsplanung", "Feuerwehrbedarfsplan", "Lärmaktionsplan", "Baumschutzsatzung",
    "Ladesäulen", "Sportstättenkonzept", "Wochenmarkt", "Friedhofsgebühren", "Bibliothek", "Stadtbus",
    "Trinkbrunnen", "Jugendzentrum", "Schwimmbad", "Klimaanpassung", "Digitalisierung", "Wohnungsbau",
]  # fmt: skip
ORTSTEILE = ["Nord", "Süd", "Ost", "West", "Mitte", "Altstadt", "Hafen", "Bergviertel", "Neustadt"]
AUSSCHUESSE = [
    "Hauptausschuss", "Finanzausschuss", "Bauausschuss", "Schulausschuss", "Sozialausschuss",
    "Umweltausschuss", "Sportausschuss", "Kulturausschuss", "Verkehrsausschuss", "Jugendhilfeausschuss",
    "Rechnungsprüfungsausschuss", "Wirtschaftsausschuss", "Digitalausschuss", "Gesundheitsausschuss",
    "Liegenschaftsausschuss", "Wahlprüfungsausschuss", "Betriebsausschuss", "Personalausschuss",
]  # fmt: skip


def _pdf(titel: str) -> bytes:
    """Kleinstes gültiges PDF mit Titel — Dateigröße ist hier kein Prüfziel, Anzahl schon."""
    return (
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]>>endobj\n"
        b"4 0 obj<</Title(" + titel.encode("ascii", "replace") + b")>>endobj\n"
        b"xref\n0 5\n0000000000 65535 f \ntrailer<</Size 5/Root 1 0 R/Info 4 0 R>>\nstartxref\n0\n%%EOF\n"
    )


def _skaliert(wert: int, anteil: float) -> int:
    return max(1, round(wert * anteil))


class Command(BaseCommand):
    help = "Erzeugt synthetische Lasttest-Daten nach Mengengerüst (klein, mittel, gross)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--profile", choices=sorted(PROFILE), default="klein", help="Mengengerüst")
        parser.add_argument("--seed", type=int, default=228, help="Zufalls-Seed, gleicher Seed = gleiche Daten")
        parser.add_argument("--reset", action="store_true", help="Nur die Lasttest-Daten dieses Profils entfernen")
        parser.add_argument(
            "--ich-weiss-was-ich-tue",
            action="store_true",
            dest="bestaetigt",
            help="Auch ohne DEBUG ausführen (niemals in Produktion)",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if not settings.DEBUG and not options["bestaetigt"]:
            raise CommandError(
                "Lasttest-Daten sind nur für Entwicklungs- und Testumgebungen gedacht (DEBUG ist aus). "
                "Wer es wirklich will: --ich-weiss-was-ich-tue"
            )
        profil_name: str = options["profile"]
        self.profil = PROFILE[profil_name]
        self.profil_name = profil_name
        self.zufall = random.Random(options["seed"])
        self.zaehler: dict[str, int] = {}
        self.jetzt = timezone.now()

        with transaction.atomic():
            self._entfernen()
            if options["reset"]:
                self.stdout.write(self.style.SUCCESS(f"Lasttest-Daten des Profils „{profil_name}“ entfernt."))
                return
            for nummer in range(self.profil.mandanten):
                self._mandant_anlegen(nummer)

        self._zusammenfassung()

    # ------------------------------------------------------------------
    # Hilfen
    # ------------------------------------------------------------------

    def _zaehle(self, schluessel: str, n: int = 1) -> None:
        self.zaehler[schluessel] = self.zaehler.get(schluessel, 0) + n

    def _kennung(self, *teile: str) -> str:
        return "-".join((PRAEFIX, self.profil_name, *teile))

    def _ext(self, art: str, schluessel: str) -> str:
        return f"{OPARL_PRAEFIX}/{self.profil_name}/{art}/{schluessel}"

    def _name(self, index: int) -> tuple[str, str]:
        vor = VORNAMEN[index % len(VORNAMEN)]
        nach = NACHNAMEN[(index // len(VORNAMEN) + index) % len(NACHNAMEN)]
        # Bei mehr als 600 Personen wiederholen sich Kombinationen — die laufende Nummer hält sie unterscheidbar
        return vor, f"{nach}-{index + 1}"

    def _thema(self, index: int) -> str:
        return f"{THEMEN[index % len(THEMEN)]} {ORTSTEILE[(index // len(THEMEN)) % len(ORTSTEILE)]}"

    def _nutzer(self, schluessel: str, vorname: str, nachname: str) -> Any:
        from apps.accounts.models import User

        # Alle Konten teilen ein Passwort; einmal gehasht statt je Konto knapp eine Sekunde PBKDF2
        if not hasattr(self, "passwort_hash"):
            self.passwort_hash = make_password(PASSWORT)
        user = User(
            email=f"{self._kennung(schluessel)}@{DOMAENE}",
            password=self.passwort_hash,
            first_name=vorname,
            last_name=nachname,
            is_active=True,
            email_verified=True,
        )
        user.save()
        self._zaehle("Nutzerkonten")
        return user

    # ------------------------------------------------------------------
    # Entfernen
    # ------------------------------------------------------------------

    def _entfernen(self) -> None:
        from apps.accounts.models import User
        from apps.session.models import SessionFile, SessionTenant
        from apps.tenants.models import Organization
        from insight_core.models import OParlSource

        praefix = self._kennung()
        # Dateien der Vorlagen liegen im Media-Verzeichnis; die Kaskade der Datenbank räumt sie nicht ab
        for datei in SessionFile.objects.filter(tenant__slug__startswith=praefix).iterator(chunk_size=BULK):
            datei.file.delete(save=False)
        SessionTenant.objects.filter(slug__startswith=praefix).delete()
        Organization.objects.filter(slug__startswith=praefix).delete()
        User.objects.filter(email__endswith=f"@{DOMAENE}", email__startswith=praefix).delete()
        OParlSource.objects.filter(url__startswith=f"{OPARL_PRAEFIX}/{self.profil_name}/").delete()

    # ------------------------------------------------------------------
    # Ein Mandant: Session + Insight + Work
    # ------------------------------------------------------------------

    def _mandant_anlegen(self, nummer: int) -> None:
        anteil = 1.0 if nummer == 0 else NEBENMANDANT_ANTEIL
        art = ["stadt", "eigenbetrieb", "zweckverband"][nummer % 3]
        stadtname = {
            "stadt": f"Stadt Lastheim ({self.profil_name})",
            "eigenbetrieb": f"Eigenbetrieb Stadtwerke Lastheim ({self.profil_name})",
            "zweckverband": f"Zweckverband Lastheim ({self.profil_name})",
        }[art]
        p = self.profil

        body = self._insight_kommune(art, stadtname)
        tenant = self._session_mandant(art, stadtname, body)
        gremien = self._gremien(tenant, body, anteil)
        personen = self._personen(tenant, body, _skaliert(p.personen, anteil))
        self._besetzung(tenant, body, gremien, personen, anteil)
        vorlagen = self._vorlagen(tenant, body, gremien, _skaliert(p.vorlagen_pro_jahr * JAHRE, anteil))
        self._dateien(tenant, body, vorlagen, _skaliert(p.dokumente, anteil))
        self._sitzungen(tenant, body, gremien, vorlagen, personen, anteil)
        self._nutzerkonten(tenant, art, _skaliert(p.nutzer, anteil))
        self._fraktion(tenant, body, art, gremien, personen, anteil)

    # --- Insight --------------------------------------------------------

    def _insight_kommune(self, art: str, name: str) -> Any:
        from insight_core.models import OParlBody, OParlLegislativeTerm, OParlSource

        quelle = OParlSource.objects.create(
            url=self._ext(art, "system"),
            name=f"Lasttest-Quelle {name}",
            # Der Sync-Daemon darf diese Quelle nie abrufen
            is_active=False,
            sync_config={"lasttest": True},
        )
        body = OParlBody.objects.create(
            source=quelle,
            external_id=self._ext(art, "body"),
            name=name,
            short_name=f"Lastheim ({self.profil_name})",
            display_name=name,
            slug=self._kennung(art),
            is_listed=True,
            description="Synthetische Lasttest-Daten (Issue #228). Alle Inhalte sind erfunden.",
            classification="Lasttest",
            deleted=False,
            oparl_created=self.jetzt,
            oparl_modified=self.jetzt,
        )
        heute = self.jetzt.date()
        OParlLegislativeTerm.objects.create(
            external_id=self._ext(art, "term"),
            body=body,
            name="Wahlperiode (Lasttest)",
            start_date=heute.replace(year=heute.year - 2),
            end_date=heute.replace(year=heute.year + 3),
            deleted=False,
        )
        self._zaehle("Insight: Kommunen")
        return body

    # --- Session --------------------------------------------------------

    def _session_mandant(self, art: str, name: str, body: Any) -> Any:
        from apps.session.models import SessionRole, SessionTenant

        tenant = SessionTenant.objects.create(
            name=name,
            slug=self._kennung(art),
            short_name="Lastheim",
            description="Synthetischer Lasttest-Mandant (Issue #228).",
            oparl_body=body,
            is_active=True,
        )
        # Dieselben Standardrollen wie beim Anlegen eines Mandanten (Issue #317)
        cast(Any, SessionRole).ensure_default_roles(tenant)
        self._zaehle("Session: Mandanten")
        return tenant

    def _gremien(self, tenant: Any, body: Any, anteil: float) -> list[Any]:
        from apps.session.models import SessionOrganization
        from insight_core.models import OParlOrganization

        anzahl = _skaliert(self.profil.gremien, anteil)
        definitionen: list[tuple[str, str, str]] = [("rat", "Rat der Stadt Lastheim", "council")]
        for i in range(anzahl - 1):
            if i < len(AUSSCHUESSE):
                definitionen.append((f"ausschuss-{i + 1}", AUSSCHUESSE[i], "committee"))
            elif i < len(AUSSCHUESSE) + len(ORTSTEILE):
                bezirk = ORTSTEILE[i - len(AUSSCHUESSE)]
                definitionen.append((f"bezirk-{i + 1}", f"Bezirksvertretung {bezirk}", "committee"))
            else:
                definitionen.append((f"beirat-{i + 1}", f"Beirat {i + 1}", "advisory"))
        gremien: list[Any] = []
        for schluessel, name, typ in definitionen[:anzahl]:
            oparl_org = OParlOrganization.objects.create(
                external_id=self._ext(tenant.slug, f"organization/{schluessel}"),
                body=body,
                name=name,
                short_name=name[:40],
                organization_type="Gremium",
                classification={"council": "Rat", "committee": "Ausschuss"}.get(typ, "Beirat"),
                deleted=False,
                oparl_created=self.jetzt,
                oparl_modified=self.jetzt,
            )
            gremien.append(
                SessionOrganization.objects.create(
                    tenant=tenant,
                    name=name,
                    organization_type=typ,
                    oparl_organization=oparl_org,
                    default_meeting_location="Rathaus Lastheim",
                    default_meeting_start_time=dt_time(17, 0),
                    is_active=True,
                )
            )
        self._zaehle("Session: Gremien", len(gremien))
        self._zaehle("Insight: Gremien", len(gremien))
        return gremien

    def _personen(self, tenant: Any, body: Any, anzahl: int) -> list[Any]:
        from apps.session.models import SessionPerson
        from insight_core.models import OParlPerson

        oparl_personen = OParlPerson.objects.bulk_create(
            [
                OParlPerson(
                    external_id=self._ext(tenant.slug, f"person/{i}"),
                    body=body,
                    name=" ".join(self._name(i)),
                    given_name=self._name(i)[0],
                    family_name=self._name(i)[1],
                    deleted=False,
                    oparl_created=self.jetzt,
                    oparl_modified=self.jetzt,
                )
                for i in range(anzahl)
            ],
            batch_size=BULK,
        )
        personen: list[Any] = []
        for i, oparl_person in enumerate(oparl_personen):
            vor, nach = self._name(i)
            person = SessionPerson(
                tenant=tenant,
                oparl_person=oparl_person,
                given_name=vor,
                family_name=nach,
                email=f"{self._kennung('person', str(i))}@{DOMAENE}",
                is_active=True,
                start_date=date(self.jetzt.year - 2, 7, 1),
            )
            # Verschlüsselte Felder nur über die erzeugten Zugriffsmethoden (Tenant-Schlüssel)
            cast(Any, person).set_phone_encrypted(f"+49 000 {i:06d}")
            cast(Any, person).set_address_encrypted(f"Lastweg {i + 1}, 00000 Lastheim")
            personen.append(person)
        SessionPerson.objects.bulk_create(personen, batch_size=BULK)
        self._zaehle("Session: Personen", anzahl)
        self._zaehle("Insight: Personen", anzahl)
        return personen

    def _besetzung(self, tenant: Any, body: Any, gremien: list[Any], personen: list[Any], anteil: float) -> None:
        from apps.session.models import SessionOrganizationMembership
        from insight_core.models import OParlMembership

        beginn = date(self.jetzt.year - 2, 7, 1)
        ratsgroesse = min(_skaliert(self.profil.abstimmende, anteil), len(personen))
        ausschussgroesse = max(3, ratsgroesse // 3)
        mitgliedschaften: list[Any] = []
        oparl_mitgliedschaften: list[Any] = []
        for index, gremium in enumerate(gremien):
            groesse = ratsgroesse if gremium.organization_type == "council" else ausschussgroesse
            # Rat: die ersten Personen; Ausschüsse: gleichmäßig über alle Personen verteilt
            auswahl = (
                personen[:groesse]
                if gremium.organization_type == "council"
                else [personen[(index * 7 + k) % len(personen)] for k in range(groesse)]
            )
            gesehen: set[Any] = set()
            for k, person in enumerate(auswahl):
                if person.pk in gesehen:
                    continue
                gesehen.add(person.pk)
                rolle = "chair" if k == 0 else "deputy_chair" if k == 1 else "member"
                mitgliedschaften.append(
                    SessionOrganizationMembership(
                        organization=gremium,
                        person=person,
                        role=rolle,
                        start_date=beginn,
                        has_voting_rights=True,
                    )
                )
                oparl_mitgliedschaften.append(
                    OParlMembership(
                        external_id=self._ext(tenant.slug, f"membership/{index}-{person.pk}"),
                        person=person.oparl_person,
                        organization=gremium.oparl_organization,
                        role={"chair": "Vorsitz", "deputy_chair": "Stellv. Vorsitz"}.get(rolle, "Mitglied"),
                        voting_right=True,
                        start_date=beginn,
                        deleted=False,
                    )
                )
        SessionOrganizationMembership.objects.bulk_create(mitgliedschaften, batch_size=BULK)
        OParlMembership.objects.bulk_create(oparl_mitgliedschaften, batch_size=BULK)
        self._zaehle("Session: Gremienbesetzungen", len(mitgliedschaften))
        self._zaehle("Insight: Mitgliedschaften", len(oparl_mitgliedschaften))

    def _vorlagen(self, tenant: Any, body: Any, gremien: list[Any], anzahl: int) -> list[Any]:
        from apps.session.models import SessionPaper
        from insight_core.models import OParlPaper

        typen = ["proposal", "proposal", "proposal", "report", "motion", "inquiry", "bylaw"]
        oparl_typen = {
            "proposal": "Beschlussvorlage",
            "report": "Mitteilungsvorlage",
            "motion": "Antrag",
            "inquiry": "Anfrage",
            "bylaw": "Satzung",
        }
        heute = self.jetzt.date()
        oparl_vorlagen: list[Any] = []
        vorlagen: list[Any] = []
        for i in range(anzahl):
            typ = typen[i % len(typen)]
            jahr = heute.year - (i * JAHRE) // max(anzahl, 1)
            kennzeichen = f"V/{jahr}/{i + 1:04d}"
            datum = heute - timedelta(days=(anzahl - i) * (JAHRE * 365) // max(anzahl, 1))
            oparl_vorlagen.append(
                OParlPaper(
                    external_id=self._ext(tenant.slug, f"paper/{i}"),
                    body=body,
                    name=self._thema(i),
                    reference=kennzeichen,
                    paper_type=oparl_typen[typ],
                    date=datum,
                    georef_status="no_locations",
                    deleted=False,
                    oparl_created=self.jetzt,
                    oparl_modified=self.jetzt,
                )
            )
            vorlagen.append(
                SessionPaper(
                    tenant=tenant,
                    reference=kennzeichen,
                    name=self._thema(i),
                    paper_type=typ,
                    status="completed" if datum < heute - timedelta(days=30) else "approved",
                    main_text=f"Sachverhalt zu {self._thema(i)} (synthetischer Lasttest-Text).",
                    resolution_text=f"Der Rat beschließt: {self._thema(i)}.",
                    is_public=i % 10 != 0,
                    date=datum,
                    main_organization=gremien[i % len(gremien)],
                    has_financial_impact=i % 3 == 0,
                )
            )
        oparl_gespeichert = OParlPaper.objects.bulk_create(oparl_vorlagen, batch_size=BULK)
        for vorlage, oparl_vorlage in zip(vorlagen, oparl_gespeichert, strict=True):
            vorlage.oparl_paper = oparl_vorlage
        SessionPaper.objects.bulk_create(vorlagen, batch_size=BULK)
        self._zaehle("Session: Vorlagen", anzahl)
        self._zaehle("Insight: Vorlagen", anzahl)
        return vorlagen

    def _dateien(self, tenant: Any, body: Any, vorlagen: list[Any], anzahl: int) -> None:
        from apps.session.models import SessionFile
        from insight_core.models import OParlFile

        inhalt = _pdf("Lasttest")
        dateien: list[Any] = []
        oparl_dateien: list[Any] = []
        for i in range(anzahl):
            vorlage = vorlagen[i % len(vorlagen)]
            name = f"{self._kennung('datei', str(i))}.pdf"
            datei = SessionFile(
                tenant=tenant,
                name=f"Anlage {i % 3 + 1} zu {vorlage.reference}",
                mime_type="application/pdf",
                size=len(inhalt),
                text_content=f"Anlage zu {vorlage.name}. Synthetischer Lasttest-Text.",
                is_public=vorlage.is_public,
                paper=vorlage,
            )
            # save=False: erst die Datei auf die Platte, die Zeile kommt gesammelt per bulk_create
            datei.file.save(name, ContentFile(inhalt), save=False)
            dateien.append(datei)
            oparl_dateien.append(
                OParlFile(
                    external_id=self._ext(tenant.slug, f"file/{i}"),
                    body=body,
                    paper=vorlage.oparl_paper,
                    name=datei.name,
                    file_name=name,
                    mime_type="application/pdf",
                    size=len(inhalt),
                    access_url=f"{OPARL_PRAEFIX}/{self.profil_name}/dateien/{name}",
                    download_url=f"{OPARL_PRAEFIX}/{self.profil_name}/dateien/{name}",
                    text_content=datei.text_content,
                    text_extraction_status="completed",
                    text_extraction_method="none",
                    page_count=1,
                    deleted=False,
                    oparl_created=self.jetzt,
                    oparl_modified=self.jetzt,
                )
            )
        SessionFile.objects.bulk_create(dateien, batch_size=BULK)
        OParlFile.objects.bulk_create(oparl_dateien, batch_size=BULK)
        self._zaehle("Session: Dateien", anzahl)
        self._zaehle("Insight: Dateien", anzahl)

    def _sitzungen(
        self, tenant: Any, body: Any, gremien: list[Any], vorlagen: list[Any], personen: list[Any], anteil: float
    ) -> None:
        from apps.session.models import (
            SessionAgendaItem,
            SessionAttendance,
            SessionMeeting,
            SessionOrganizationMembership,
            SessionVote,
        )
        from insight_core.models import OParlAgendaItem, OParlConsultation, OParlMeeting

        anzahl = _skaliert(self.profil.sitzungen_pro_jahr * JAHRE, anteil)
        # Zwei Jahre zurück, zwei Monate voraus — gleichmäßig verteilt, Rat sitzt am häufigsten
        spanne_tage = JAHRE * 365 + 60
        mitglieder_je_gremium: dict[Any, list[Any]] = {}
        for m in SessionOrganizationMembership.objects.filter(organization__tenant=tenant).select_related("person"):
            mitglieder_je_gremium.setdefault(m.organization_id, []).append(m.person)

        sitzungen: list[Any] = []
        oparl_sitzungen: list[Any] = []
        for i in range(anzahl):
            gremium = gremien[0] if i % 4 == 0 else gremien[i % len(gremien)]
            beginn = (self.jetzt - timedelta(days=spanne_tage - (i * spanne_tage) // anzahl)).replace(
                hour=17, minute=0, second=0, microsecond=0
            )
            vergangen = beginn < self.jetzt
            sitzungen.append(
                SessionMeeting(
                    tenant=tenant,
                    organization=gremium,
                    name=f"{i + 1}. Sitzung {gremium.name}",
                    start=beginn,
                    end=beginn + timedelta(hours=3),
                    location="Rathaus Lastheim",
                    room="Ratssaal" if gremium.organization_type == "council" else "Sitzungssaal",
                    meeting_state="completed" if vergangen else "invitation_sent",
                    is_public=True,
                )
            )
            oparl_sitzungen.append(
                OParlMeeting(
                    external_id=self._ext(tenant.slug, f"meeting/{i}"),
                    body=body,
                    name=f"{i + 1}. Sitzung {gremium.name}",
                    meeting_state="durchgeführt" if vergangen else "terminiert",
                    start=beginn,
                    end=beginn + timedelta(hours=3),
                    location_name="Rathaus Lastheim",
                    cancelled=False,
                    deleted=False,
                    oparl_created=self.jetzt,
                    oparl_modified=self.jetzt,
                )
            )
        SessionMeeting.objects.bulk_create(sitzungen, batch_size=BULK)
        oparl_gespeichert = OParlMeeting.objects.bulk_create(oparl_sitzungen, batch_size=BULK)
        for sitzung, oparl_sitzung in zip(sitzungen, oparl_gespeichert, strict=True):
            oparl_sitzung.organizations.add(sitzung.organization.oparl_organization)
        for sitzung, oparl_sitzung in zip(sitzungen, oparl_gespeichert, strict=True):
            sitzung.oparl_meeting = oparl_sitzung
        SessionMeeting.objects.bulk_update(sitzungen, ["oparl_meeting"], batch_size=BULK)
        self._zaehle("Session: Sitzungen", anzahl)
        self._zaehle("Insight: Sitzungen", anzahl)

        # Tagesordnungen: TOP 1 Eröffnung, danach Vorlagen; jeder dritte Vorlagen-TOP wird namentlich abgestimmt
        tops: list[Any] = []
        oparl_tops: list[Any] = []
        beratungen: list[Any] = []
        vorlagen_index = 0
        for i, (sitzung, oparl_sitzung) in enumerate(zip(sitzungen, oparl_gespeichert, strict=True)):
            vergangen = sitzung.meeting_state == "completed"
            for nr in range(1, self.profil.tops_pro_sitzung + 1):
                vorlage = None
                if nr > 1:
                    vorlage = vorlagen[vorlagen_index % len(vorlagen)]
                    vorlagen_index += 1
                name = "Eröffnung und Feststellung der Tagesordnung" if vorlage is None else vorlage.name
                namentlich = vorlage is not None and nr % 3 == 0
                tops.append(
                    SessionAgendaItem(
                        meeting=sitzung,
                        number=str(nr),
                        name=name,
                        order=nr,
                        is_public=vorlage is None or vorlage.is_public,
                        paper=vorlage,
                        voting_method="roll_call" if namentlich else "summary",
                        vote_result="approved" if vergangen and vorlage is not None else "pending",
                        votes_yes=self.zufall.randint(5, 20) if vergangen and vorlage is not None else 0,
                        votes_no=self.zufall.randint(0, 5) if vergangen and vorlage is not None else 0,
                    )
                )
                ext = self._ext(tenant.slug, f"agendaitem/{i}-{nr}")
                oparl_tops.append(
                    OParlAgendaItem(
                        external_id=ext,
                        meeting=oparl_sitzung,
                        number=str(nr),
                        order=nr,
                        name=name,
                        public=vorlage is None or vorlage.is_public,
                        result="Beschlossen" if vergangen and vorlage is not None else "",
                        deleted=False,
                        oparl_created=self.jetzt,
                        oparl_modified=self.jetzt,
                    )
                )
                if vorlage is not None:
                    beratungen.append(
                        OParlConsultation(
                            external_id=self._ext(tenant.slug, f"consultation/{i}-{nr}"),
                            body=body,
                            paper=vorlage.oparl_paper,
                            paper_external_id=vorlage.oparl_paper.external_id,
                            meeting_external_id=oparl_sitzung.external_id,
                            agenda_item_external_id=ext,
                            role="Entscheidung"
                            if sitzung.organization.organization_type == "council"
                            else "Vorberatung",
                            authoritative=sitzung.organization.organization_type == "council",
                            deleted=False,
                        )
                    )
        SessionAgendaItem.objects.bulk_create(tops, batch_size=BULK)
        OParlAgendaItem.objects.bulk_create(oparl_tops, batch_size=BULK)
        OParlConsultation.objects.bulk_create(beratungen, batch_size=BULK)
        self._zaehle("Session: Tagesordnungspunkte", len(tops))
        self._zaehle("Insight: Tagesordnungspunkte", len(oparl_tops))
        self._zaehle("Insight: Beratungen", len(beratungen))

        # Anwesenheit und Einzelstimmen für vergangene Sitzungen
        anwesenheiten: list[Any] = []
        stimmen: list[Any] = []
        stimmwerte = ["yes", "yes", "yes", "no", "abstain"]
        tops_je_sitzung: dict[Any, list[Any]] = {}
        for top in tops:
            tops_je_sitzung.setdefault(top.meeting_id, []).append(top)
        for sitzung in sitzungen:
            if sitzung.meeting_state != "completed":
                continue
            mitglieder = mitglieder_je_gremium.get(sitzung.organization_id, [])
            for k, person in enumerate(mitglieder):
                anwesend = k % 9 != 8
                anwesenheiten.append(
                    SessionAttendance(
                        meeting=sitzung,
                        person=person,
                        status="present" if anwesend else "excused",
                        role="chair" if k == 0 else "member",
                        has_voting_rights=True,
                    )
                )
                if not anwesend:
                    continue
                for top in tops_je_sitzung.get(sitzung.pk, []):
                    if top.voting_method == "roll_call":
                        stimmen.append(SessionVote(agenda_item=top, person=person, vote=self.zufall.choice(stimmwerte)))
        SessionAttendance.objects.bulk_create(anwesenheiten, batch_size=BULK)
        SessionVote.objects.bulk_create(stimmen, batch_size=BULK)
        self._zaehle("Session: Anwesenheiten", len(anwesenheiten))
        self._zaehle("Session: Einzelstimmen", len(stimmen))

        # Eine laufende Ratssitzung mit Anwesenheit — Ziel des Locust-Szenarios „Live-Abstimmung“
        live = SessionMeeting.objects.create(
            tenant=tenant,
            organization=gremien[0],
            name=f"Laufende Ratssitzung (Lasttest {self.profil_name})",
            start=self.jetzt - timedelta(hours=1),
            actual_start=self.jetzt - timedelta(hours=1),
            location="Rathaus Lastheim",
            room="Ratssaal",
            meeting_state="in_progress",
            is_public=True,
        )
        live_tops = [
            SessionAgendaItem(
                meeting=live,
                number=str(nr),
                name="Eröffnung" if nr == 1 else f"Abstimmung {nr - 1}: {self._thema(nr)}",
                order=nr,
                is_public=True,
                paper=None if nr == 1 else vorlagen[nr % len(vorlagen)],
                voting_method="summary" if nr == 1 else "roll_call",
            )
            for nr in range(1, 11)
        ]
        SessionAgendaItem.objects.bulk_create(live_tops)
        SessionAttendance.objects.bulk_create(
            [
                SessionAttendance(meeting=live, person=person, status="present", role="member", has_voting_rights=True)
                for person in mitglieder_je_gremium.get(gremien[0].pk, [])
            ]
        )
        self._zaehle("Session: laufende Sitzungen", 1)

    def _nutzerkonten(self, tenant: Any, art: str, anzahl: int) -> None:
        from apps.session.models import SessionUser

        rollen = {r.name: r for r in tenant.roles.all()}
        # Ein Konto mit Verwaltungsrechten und viele Sachbearbeitungskonten, damit Locust parallel anmelden kann.
        # Administratoren unterliegen der 2FA-Pflicht — deshalb melden sich die Lastszenarien als Sachbearbeitung an.
        admin = self._nutzer(f"{art}-verwaltung", "Verena", "Verwaltung")
        SessionUser.objects.create(user=admin, tenant=tenant, is_active=True).roles.set([rollen["Administrator"]])
        for i in range(anzahl):
            vor, nach = self._name(i + 1000)
            user = self._nutzer(f"{art}-sachbearbeitung-{i + 1}", vor, nach)
            SessionUser.objects.create(user=user, tenant=tenant, is_active=True).roles.set([rollen["Sachbearbeiter"]])
        self._zaehle("Session: Nutzerkonten", anzahl + 1)

    # --- Work -------------------------------------------------------------

    def _fraktion(
        self, tenant: Any, body: Any, art: str, gremien: list[Any], personen: list[Any], anteil: float
    ) -> None:
        from apps.tenants.models import Membership, Organization, Role
        from apps.work.models import Motion

        org = Organization.objects.create(
            name=f"Lasttest-Fraktion {art} ({self.profil_name})",
            slug=self._kennung(art, "fraktion"),
            description="Synthetische Fraktion für Lasttests (Issue #228).",
            body=body,
            is_active=True,
            plan="community",
        )
        rollen = {r.name: r for r in cast(Any, Role).create_default_roles(org)}
        anzahl = _skaliert(self.profil.fraktionsmitglieder, anteil)
        mitglieder: list[Any] = []
        for i in range(anzahl):
            vor, nach = self._name(i + 2000)
            user = self._nutzer(f"{art}-fraktion-{i + 1}", vor, nach)
            mitglied = Membership.objects.create(user=user, organization=org, is_active=True, is_guest=False)
            mitglied.roles.set([rollen["Fraktionsvorsitz" if i == 0 else "Fraktionsmitglied"]])
            if i < len(personen):
                mitglied.oparl_person = personen[i].oparl_person
                mitglied.save(update_fields=["oparl_person"])
            mitglieder.append(mitglied)
        org.owner = mitglieder[0].user
        cast(Any, org).save(update_fields=["owner"])
        org.oparl_organizations.set([gremien[0].oparl_organization])

        stati = ["draft", "review", "submitted", "completed", "internal_review"]
        for i in range(_skaliert(self.profil.antraege, anteil)):
            antrag = Motion(
                organization=org,
                title=f"Antrag {i + 1}: {self._thema(i)}",
                motion_type="motion" if i % 4 else "inquiry",
                status=stati[i % len(stati)],
                visibility="organization",
                author=mitglieder[i % len(mitglieder)],
                summary=f"Kurzfassung zu {self._thema(i)}.",
            )
            # Der Editor-Inhalt ist verschlüsselt (Mandantenschlüssel); Absätze wie im echten Betrieb
            cast(Any, antrag).set_content_encrypted(
                f"<h2>Antrag {i + 1}: {self._thema(i)}</h2>"
                + "".join(f"<p>Begründung Absatz {k + 1}: synthetischer Lasttest-Text.</p>" for k in range(6))
            )
            antrag.save()
        self._zaehle("Work: Organisationen")
        self._zaehle("Work: Mitgliedschaften", anzahl)
        self._zaehle("Work: Anträge", _skaliert(self.profil.antraege, anteil))

    # ------------------------------------------------------------------
    # Ausgabe
    # ------------------------------------------------------------------

    def _zusammenfassung(self) -> None:
        self.stdout.write(self.style.SUCCESS(f"\nLasttest-Daten „{self.profil_name}“ angelegt:"))
        for schluessel, wert in sorted(self.zaehler.items()):
            self.stdout.write(f"  - {schluessel}: {wert}")
        haupt = self._kennung("stadt")
        self.stdout.write("\nEinstiegspunkte (Passwort aller Konten: siehe PASSWORT in diesem Kommando):")
        self.stdout.write(f"  Insight:  /insight/  (Kommune „{haupt}“)")
        self.stdout.write(f"  Session:  /session/{haupt}/  ({self._kennung('stadt-sachbearbeitung-1')}@{DOMAENE})")
        self.stdout.write(f"  Work:     /work/{haupt}-fraktion/  ({self._kennung('stadt-fraktion-1')}@{DOMAENE})")
        medien = Path(settings.MEDIA_ROOT)
        self.stdout.write(f"\nDateien liegen unter {medien / 'session' / 'files'}; --reset entfernt sie wieder.")
