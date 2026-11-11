# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Bestand der Musterstadt für Session: die Stadtverwaltung Musterstadt (Demo) über ein Jahr (Issue #99).

Die Basisdemo (``setup_demo_environment``) legt den Session-Mandanten mit wenigen, von Hand geschriebenen
Geschichten an. Dieser Befehl füllt ihn mit dem Bestand aus ``apps.common.demo_stadt.stadtplan`` – denselben
Personen, Gremien, Terminen und Drucksachennummern wie in Insight:

- Gremien, Fraktionen und Ämter; Personen mit verschlüsselten Kontaktdaten; Besetzung von Rat, Ausschüssen und
  Fraktionen (Ausschüsse nach d'Hondt aus dem Katalog).
- Je Vorgang eine Vorlage mit Beratungsfolge, Federführung und Mitzeichnung der Kämmerei; Anträge und Anfragen der
  Fraktionen gehen als eingereichter Antrag ein und sind in eine Vorlage umgewandelt.
- Sitzungen mit Tagesordnung (öffentlich und nicht öffentlich), Ergebnissen, Abstimmungen (einige namentlich),
  Beschlussnummern und Umsetzungsstand; vergangene mit Anwesenheit und Niederschrift (genehmigt in der Folgesitzung,
  die jüngste zur Prüfung), kommende geplant bzw. mit versandter Einladung und Rückmeldungen.

Die Sitzungen der Basisdemo bleiben, wie sie sind: An die kommende Ratssitzung und den kommenden Hauptausschuss
(``BASIS_SITZUNG_SESSION``) kommen nur weitere TOPs hinten an. Sitzungen, die die Basisdemo nur in Insight hat
(``rat-1`` …), legt der Bestand hier als eigene Sitzung an, damit die Beratungsfolgen vollständig sind.

Idempotent: Ein wiederholter Lauf entfernt den Bestand (Sitzungen „<n>. Sitzung …“, Vorlagen und Anträge mit
Katalogtitel und Bestandsnummer, die geplanten Mitgliedschaften) und legt ihn neu an. Personen, Gremien, Fraktionen
und Ämter bleiben erhalten und werden nur ergänzt. Alles läuft per ``bulk_create`` – ohne Signale und ohne
Audit-Einträge, wie andere Datenpflege der Demo.

    python manage.py setup_demo_session --aufbautag 2026-11-11 --umfang klein
"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from typing import TYPE_CHECKING, Any, cast

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction
from django.utils import timezone

from apps.common.demo_daten import (
    DEMO_EMAIL_DOMAIN,
    DEMO_ORG_SLUG,
    DEMO_SESSION_SLUG,
    DEMO_START_DATE,
    DEMO_USERS,
    demo_ext,
    termin_am_sitzungstag,
)
from apps.common.demo_stadt import (
    AEMTER,
    AMT,
    BASIS_SITZUNG_SESSION,
    BUERGERMEISTERIN,
    FRAKTION,
    FRAKTIONEN,
    GREMIEN,
    GREMIUM,
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
)

if TYPE_CHECKING:
    from apps.session.models import (
        SessionAgendaItem,
        SessionApplication,
        SessionAttendance,
        SessionMeeting,
        SessionOrganization,
        SessionPaper,
        SessionPerson,
        SessionUser,
        SessionVote,
    )

#: Bestandssitzungen heißen „12. Sitzung des Hauptausschusses“; Basisdemo und Drehbuch nennen ihre anders
BESTAND_SITZUNG_RE = r"^[0-9]+\. Sitzung "
#: Drucksachennummern des Bestands (V/2026/0101 …); die Basisdemo nutzt SV/2026/D-001, das Drehbuch eigene Kreise
BESTAND_NUMMER_RE = r"^(V|A|AF|M)/[0-9]{4}/[0-9]{4}$"
BATCH = 500

ORT = {
    "location": "Rathaus Musterstadt",
    "street_address": "Rathausplatz 1",
    "postal_code": "12345",
    "locality": "Musterstadt",
}
#: Standard-TOPs, die Sitzungen aus Insight-Basissitzungen hier zusätzlich bekommen (der Plan liefert dort nur
#: die Vorgänge, weil Insight die Eröffnung schon hat)
STANDARD_VORNE = (
    "Eröffnung, Feststellung der Ordnungsmäßigkeit und der Tagesordnung",
    "Genehmigung der Niederschrift über die letzte Sitzung",
    "Mitteilungen der Verwaltung",
)
STANDARD_HINTEN = "Anfragen und Verschiedenes"
NIEDERSCHRIFT_TOP = "Genehmigung der Niederschrift"

ROLLE = {"Vorberatung": "preliminary", "Entscheidung": "decision", "Kenntnisnahme": "information"}
#: Ergebnis des Plans → Ergebnis von TOP und Beratung (die Empfehlung eines Ausschusses ist ein „angenommen“)
ERGEBNIS = {
    "": "pending",
    "approved": "approved",
    "recommended": "approved",
    "rejected": "rejected",
    "deferred": "deferred",
    "withdrawn": "withdrawn",
    "noted": "noted",
}
PROTOKOLLNOTIZ = {
    "recommended": "Die Verwaltung erläutert die Vorlage. Der Ausschuss empfiehlt die Beschlussfassung.",
    "approved": "Die Verwaltung erläutert die Vorlage. Nach kurzer Aussprache wird abgestimmt.",
    "rejected": "Nach kontroverser Aussprache findet der Beschlussvorschlag keine Mehrheit.",
    "deferred": "Auf Antrag einer Fraktion wird der Punkt in die nächste Sitzung vertagt.",
    "withdrawn": "Die antragstellende Fraktion zieht den Antrag in der Sitzung zurück.",
    "noted": "Die Verwaltung beantwortet die Fragen und erläutert den Bericht; das Gremium nimmt ihn zur Kenntnis.",
}
UMSETZUNG_HINWEIS = {
    "open": "Die Umsetzung ist eingeplant.",
    "in_progress": "Die Verwaltung setzt den Beschluss um; erste Schritte sind erledigt.",
    "done": "Der Beschluss ist umgesetzt.",
    "deferred": "Die Umsetzung ist zurückgestellt, bis die Mittel im Haushalt bereitstehen.",
}
STRASSEN = ("Lindenstraße", "Hauptstraße", "Am Anger", "Musterweg", "Nordring", "Südring", "Westfalenallee")


def _eur(betrag: int) -> str:
    return f"{betrag:,}".replace(",", ".") + " € brutto"


@dataclass
class _Sitzung:
    """Eine Sitzung des Bestands mit ihren TOPs (Plan und Datenbank nebeneinander)."""

    plan: Sitzung
    meeting: SessionMeeting
    tops: list[Top]
    neu: bool  # im Bestand angelegt (sonst eine Sitzung der Basisdemo, an die nur TOPs angehängt sind)
    items: list[SessionAgendaItem] = field(default_factory=list)
    anwesend: list[SessionPerson] = field(default_factory=list)

    @property
    def vergangen(self) -> bool:
        return self.plan.tag < 0


class Command(BaseCommand):
    help = "Ergänzt die Demo-Umgebung um den Bestand der Musterstadt: Session (Stadtverwaltung Musterstadt)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--aufbautag", type=date.fromisoformat, default=None, help="Bezugstag (JJJJ-MM-TT)")
        parser.add_argument("--umfang", choices=UMFAENGE, default=None, help="voll (Demo) oder klein (Tests)")

    def handle(self, *args: Any, **options: Any) -> None:
        from apps.session.models import SessionTenant

        mandant = SessionTenant.objects.filter(slug=DEMO_SESSION_SLUG).first()
        if mandant is None:
            raise CommandError(
                f"Demo-Mandant {DEMO_SESSION_SLUG} fehlt – zuerst python manage.py setup_demo_environment ausführen."
            )
        self.aufbautag: date = options["aufbautag"] or timezone.localdate()
        self.plan: Stadtplan = stadtplan(self.aufbautag, options["umfang"] or demo_umfang())
        self.jetzt = timezone.now()
        self.zaehler: dict[str, int] = {}
        self.mandant = mandant

        with transaction.atomic():
            self._nutzer()
            self._aufraeumen()
            self._organisationen()
            self._personen()
            self._besetzung()
            self._vorlagen()
            sitzungen = self._sitzungen()
            self._anwesenheit(sitzungen)
            self._tagesordnungen(sitzungen)
            self._beratungen(sitzungen)
            self._niederschriften(sitzungen)

        self.stdout.write(self.style.SUCCESS(f"Session-Bestand der Musterstadt ({self.plan.umfang}):"))
        for name, anzahl in self.zaehler.items():
            self.stdout.write(f"  - Session: {name}: {anzahl}")

    def _zaehlen(self, name: str, anzahl: int = 1) -> None:
        self.zaehler[name] = self.zaehler.get(name, 0) + anzahl

    def _termin(self, tag: int, stunde: int = 17) -> datetime:
        return termin_am_sitzungstag(self.aufbautag, tag, stunde)

    def _zeitpunkt(self, tag: int, stunde: int = 10) -> datetime:
        """Zeitpunkt an einem Tag relativ zum Aufbautag, nie in der Zukunft (Eingang, Freigabe, Mitzeichnung)."""
        moment = timezone.make_aware(datetime.combine(self.aufbautag + timedelta(days=tag), dt_time(stunde, 0)))
        return min(moment, self.jetzt - timedelta(minutes=5))

    # ------------------------------------------------------------------
    # Grundlagen
    # ------------------------------------------------------------------

    def _nutzer(self) -> None:
        from apps.session.models import SessionUser

        self.nutzer: dict[str, SessionUser] = {}
        for key in ("verwaltung", "sachbearbeitung", "protokoll"):
            zugang = SessionUser.objects.filter(tenant=self.mandant, user__email=DEMO_USERS[key]["email"]).first()
            if zugang is None:
                raise CommandError(
                    f"Demo-Nutzer {DEMO_USERS[key]['email']} fehlt im Mandanten {DEMO_SESSION_SLUG} – "
                    "zuerst python manage.py setup_demo_environment ausführen."
                )
            self.nutzer[key] = zugang

    def _titel(self) -> set[str]:
        """Alle Vorgangstitel des Katalogs (unabhängig von Tag und Umfang) – Schlüssel fürs Aufräumen."""
        return {v.titel for v in stadtplan(self.aufbautag, "voll").vorgaenge}

    def _aufraeumen(self) -> None:
        """Bestand eines früheren Laufs entfernen; Basisdemo und Drehbuch bleiben unberührt."""
        from apps.session.models import SessionAgendaItem, SessionApplication, SessionMeeting, SessionPaper
        from apps.session.services import protocol_lock

        sitzungen = SessionMeeting.objects.filter(tenant=self.mandant, name__regex=BESTAND_SITZUNG_RE)
        # Die Niederschriften des Bestands sind genehmigt und damit gesperrt (Issue #318); das Neuanlegen der Demo ist
        # Datenpflege und löscht die Sitzungen ausdrücklich mit (Sperre bei Sammel-Löschen, Issue #427)
        with ExitStack() as erlaubt:
            for pk in sitzungen.values_list("pk", flat=True):
                erlaubt.enter_context(protocol_lock.permit(pk))
            sitzungen.delete()
        titel = self._titel()
        vorlagen = SessionPaper.objects.filter(tenant=self.mandant, name__in=titel, reference__regex=BESTAND_NUMMER_RE)
        # Angehängte TOPs in den Sitzungen der Basisdemo (kommend, nicht gesperrt)
        SessionAgendaItem.objects.filter(meeting__tenant=self.mandant, paper__in=vorlagen).delete()
        vorlagen.delete()
        SessionApplication.objects.filter(
            tenant=self.mandant, title__in=titel, reference__regex=BESTAND_NUMMER_RE
        ).delete()

    def _organisation(self, name: str, **felder: Any) -> SessionOrganization:
        """Gremium, Fraktion oder Amt über den Namen holen oder anlegen; bestehende bleiben unverändert."""
        from apps.session.models import SessionOrganization
        from insight_core.models import OParlOrganization

        org = SessionOrganization.objects.filter(tenant=self.mandant, name=name).first()
        if org is not None:
            return org
        insight_key = felder.pop("insight_key", None)
        if insight_key:
            insight = OParlOrganization.objects.filter(external_id=demo_ext("organization", insight_key)).first()
            if insight is not None and not SessionOrganization.objects.filter(oparl_organization=insight).exists():
                felder["oparl_organization"] = insight
        org = SessionOrganization.objects.create(tenant=self.mandant, name=name, is_active=True, **felder)
        self._zaehlen(f"neu angelegt: {org.get_organization_type_display()}")
        return org

    def _organisationen(self) -> None:
        self.gremien = {
            g.key: self._organisation(
                g.name,
                insight_key=g.key,
                short_name=g.kurz,
                organization_type=g.session_art,
                default_meeting_location=ORT["location"],
                default_meeting_start_time=dt_time(17, 0),
                target_member_count=g.sitze + (1 if g.key == "rat" else 0),
            )
            for g in GREMIEN
        }
        self.fraktionen = {
            f.key: self._organisation(
                f.name,
                insight_key=f.key,
                short_name=f.kurz,
                organization_type="group" if f.art == "Gruppe" else "faction",
            )
            for f in FRAKTIONEN
        }
        # Die Kämmerei der Basisdemo heißt „Kämmerei (Demo)“ – alle Ämter folgen dem Muster und finden sie so wieder
        self.aemter = {
            a.key: self._organisation(f"{a.name} (Demo)", short_name=a.kurz, organization_type="department")
            for a in AEMTER
        }
        self._zaehlen("Gremien", len(self.gremien))
        self._zaehlen("Fraktionen und Gruppen", len(self.fraktionen))
        self._zaehlen("Ämter", len(self.aemter))

    def _personen(self) -> None:
        """Personen des Katalogs; die acht der Basisdemo gibt es schon (gleiche E-Mail-Adresse)."""
        from apps.session.models import SessionPerson
        from insight_core.models import OParlPerson

        insight = {
            p.external_id: p
            for p in OParlPerson.objects.filter(external_id__in=[demo_ext("person", p.key) for p in PERSONEN])
        }
        verknuepft = set(
            SessionPerson.objects.filter(oparl_person__in=list(insight.values())).values_list(
                "oparl_person_id", flat=True
            )
        )
        self.personen: dict[str, SessionPerson] = {
            p.email: p for p in SessionPerson.objects.filter(tenant=self.mandant, email__in=[k.email for k in PERSONEN])
        }
        for idx, person in enumerate(PERSONEN):
            if person.basis and person.email in self.personen:
                continue
            vorhanden = self.personen.get(person.email)
            oparl = insight.get(demo_ext("person", person.key))
            felder: dict[str, Any] = {
                "title": person.titel,
                "given_name": person.vorname,
                "family_name": person.nachname,
                "form_of_address": "Frau" if person.geschlecht == "female" else "Herr",
                "is_active": True,
                "start_date": DEMO_START_DATE,
            }
            if oparl is not None and oparl.pk not in verknuepft:
                felder["oparl_person"] = oparl
                verknuepft.add(oparl.pk)
            eintrag = vorhanden or SessionPerson(tenant=self.mandant, email=person.email)
            for name, wert in felder.items():
                setattr(eintrag, name, wert)
            cast(Any, eintrag).set_phone_encrypted(f"+49 1234 5678-{idx + 20:02d}")
            cast(Any, eintrag).set_address_encrypted(f"{STRASSEN[idx % len(STRASSEN)]} {idx + 3}, 12345 Musterstadt")
            eintrag.save()
            self.personen[person.email] = eintrag
        self._zaehlen("Personen", len(self.personen))

    def _person(self, key: str) -> SessionPerson:
        return self.personen[PERSON[key].email]

    # ------------------------------------------------------------------
    # Besetzung
    # ------------------------------------------------------------------

    def _besetzung(self) -> None:
        """Rat, Ausschüsse und Fraktionen; die Besetzung der Basisdemo bleibt, wie sie ist."""
        from apps.session.models import SessionOrganizationMembership

        geplant: dict[tuple[Any, Any], str] = {}
        for person in PERSONEN:
            if person.rat and not person.basis:
                rolle = "chair" if person.key == BUERGERMEISTERIN else "member"
                geplant[(self.gremien["rat"].pk, self._person(person.key).pk)] = rolle
        for m in self.plan.besetzung:
            rolle = m.session_rolle if PERSON[m.person].rat else "expert_citizen"
            geplant[(self.gremien[m.gremium].pk, self._person(m.person).pk)] = rolle
        for fraktion in FRAKTIONEN:
            mitglieder = [p for p in PERSONEN if p.fraktion == fraktion.key]
            vorsitz = next(p for p in mitglieder if p.rat)
            for person in mitglieder:
                rolle = "chair" if person is vorsitz else "member"
                geplant[(self.fraktionen[fraktion.key].pk, self._person(person.key).pk)] = rolle

        bestehend = SessionOrganizationMembership.objects.filter(
            organization__tenant=self.mandant, start_date=DEMO_START_DATE
        ).values_list("pk", "organization_id", "person_id")
        alt = [pk for pk, org, person in bestehend if (org, person) in geplant]
        SessionOrganizationMembership.objects.filter(pk__in=alt).delete()
        SessionOrganizationMembership.objects.bulk_create(
            [
                SessionOrganizationMembership(
                    organization_id=org,
                    person_id=person,
                    role=rolle,
                    start_date=DEMO_START_DATE,
                    has_voting_rights=True,
                )
                for (org, person), rolle in geplant.items()
            ],
            batch_size=BATCH,
        )
        self._zaehlen("Mitgliedschaften (Rat, Ausschüsse, Fraktionen)", len(geplant))

    # ------------------------------------------------------------------
    # Vorlagen und Anträge
    # ------------------------------------------------------------------

    def _auftritte(self, vorgang: Vorgang) -> list[tuple[Sitzung, Top]]:
        return [(s, t) for s in self.plan.sitzungen for t in s.tops if t.vorgang == vorgang.key]

    def _status(self, vorgang: Vorgang, auftritte: list[tuple[Sitzung, Top]]) -> str:
        if any(s.tag > 0 for s, _t in auftritte):
            return "scheduled"
        stationen = {s.gremium for s, _t in auftritte}
        if auftritte and set(vorgang.weg) <= stationen:
            return "withdrawn" if auftritte[-1][1].ergebnis == "withdrawn" else "completed"
        return "draft" if vorgang.tag > 0 else "approved"

    def _vorlagen(self) -> None:
        from apps.session.models import SessionApplication, SessionCosignature, SessionPaper
        from apps.session.services import body_service
        from apps.tenants.models import Organization

        fraktion_work = Organization.objects.filter(slug=DEMO_ORG_SLUG).first()
        koerperschaft = body_service.default_body(self.mandant)
        kaemmerei = self.aemter["amt-20"]
        antraege: dict[str, SessionApplication] = {}
        for vorgang in self.plan.vorgaenge:
            if not vorgang.von_fraktion:
                continue
            fraktion = FRAKTION[vorgang.urheber]
            antraege[vorgang.key] = SessionApplication(
                tenant=self.mandant,
                reference=vorgang.nummer,
                title=vorgang.titel,
                application_type="inquiry" if vorgang.art == "anfrage" else "motion",
                status="converted",
                justification="\n".join(vorgang.sachverhalt).removeprefix("Begründung: "),
                resolution_proposal=vorgang.beschluss,
                financial_impact=f"ca. {_eur(vorgang.kosten)}" if vorgang.kosten else "",
                submitting_organization=fraktion_work if vorgang.urheber == "fraktion-bunte-liste" else None,
                submitter_name=fraktion.name,
                submitter_email=f"{fraktion.key}@{DEMO_EMAIL_DOMAIN}",
                target_organization=self.gremien[vorgang.weg[-1]],
                received_at=self._zeitpunkt(vorgang.tag + 1),
                received_by=self.nutzer["sachbearbeitung"],
            )
        SessionApplication.objects.bulk_create(list(antraege.values()), batch_size=BATCH)
        # „Eingereicht am“ setzt Django beim Anlegen auf jetzt; im Bestand gilt das Datum des Vorgangs
        for key, antrag in antraege.items():
            antrag.submitted_at = self._zeitpunkt(self.plan.vorgang[key].tag, 8)
        SessionApplication.objects.bulk_update(list(antraege.values()), ["submitted_at"], batch_size=BATCH)

        self.vorlagen: dict[str, SessionPaper] = {}
        mitzeichnungen = []
        for vorgang in self.plan.vorgaenge:
            status = self._status(vorgang, self._auftritte(vorgang))
            freigegeben = status != "draft"
            vorlage = SessionPaper(
                tenant=self.mandant,
                body=koerperschaft,
                reference=vorgang.nummer,
                reference_assigned_at=self._zeitpunkt(vorgang.tag, 9),
                name=vorgang.titel,
                paper_type=vorgang.session_art,
                main_text="\n\n".join(vorgang.sachverhalt),
                resolution_text=vorgang.beschluss,
                is_public=True,
                status=status,
                date=self.plan.datum(vorgang.tag),
                main_organization=self.gremien[vorgang.weg[-1]],
                lead_department=self.aemter.get(vorgang.urheber),
                originator_organization=self.fraktionen.get(vorgang.urheber),
                has_financial_impact=bool(vorgang.kosten),
                financial_impact_note=f"{_eur(vorgang.kosten)}" if vorgang.kosten else "",
                source_application=antraege.get(vorgang.key),
                created_by=self.nutzer["sachbearbeitung"],
                approved_by=self.nutzer["verwaltung"] if freigegeben else None,
                approved_at=self._zeitpunkt(vorgang.tag + 2, 11) if freigegeben else None,
            )
            self.vorlagen[vorgang.key] = vorlage
            if vorgang.kosten and not vorgang.von_fraktion:
                # Mitzeichnungsregel der Basisdemo: Vorlagen mit finanziellen Auswirkungen zeichnet die Kämmerei mit
                mitzeichnungen.append(
                    SessionCosignature(
                        paper=vorlage,
                        department=kaemmerei,
                        order=1,
                        status="signed" if freigegeben else "pending",
                        comment="Deckung im Haushalt ist gegeben." if freigegeben else "",
                        decided_by=self.nutzer["verwaltung"] if freigegeben else None,
                        decided_at=self._zeitpunkt(vorgang.tag + 1, 15) if freigegeben else None,
                    )
                )
        SessionPaper.objects.bulk_create(list(self.vorlagen.values()), batch_size=BATCH)
        SessionCosignature.objects.bulk_create(mitzeichnungen, batch_size=BATCH)
        self._zaehlen("Vorlagen", len(self.vorlagen))
        self._zaehlen("Anträge und Anfragen der Fraktionen", len(antraege))
        self._zaehlen("Mitzeichnungen der Kämmerei", len(mitzeichnungen))

    # ------------------------------------------------------------------
    # Sitzungen
    # ------------------------------------------------------------------

    def _sitzungsnamen(self) -> dict[str, str]:
        """Name je Plansitzung; Sitzungen, die die Basisdemo nur in Insight hat, bekommen die freie Nummer dazwischen."""
        namen: dict[str, str] = {}
        laufend: dict[str, int] = {}
        for sitzung in self.plan.sitzungen:
            if sitzung.name:
                laufend[sitzung.gremium] = int(sitzung.name.split(".", 1)[0])
                namen[sitzung.key] = sitzung.name
            else:
                laufend[sitzung.gremium] = laufend.get(sitzung.gremium, 0) + 1
                namen[sitzung.key] = f"{laufend[sitzung.gremium]}. Sitzung {GREMIUM[sitzung.gremium].genitiv}"
        return namen

    @staticmethod
    def _mit_standard(tops: tuple[Top, ...]) -> list[Top]:
        """TOPs einer Insight-Basissitzung um Eröffnung und Schluss ergänzen und durchnummerieren."""
        liste = [Top(nummer="", reihenfolge=0, titel=t) for t in STANDARD_VORNE]
        liste += [t for t in tops if t.oeffentlich]
        liste.append(Top(nummer="", reihenfolge=0, titel=STANDARD_HINTEN))
        nummeriert = [Top(**{**t.__dict__, "nummer": str(i), "reihenfolge": i}) for i, t in enumerate(liste, start=1)]
        return nummeriert + [t for t in tops if not t.oeffentlich]

    def _sitzungen(self) -> list[_Sitzung]:
        from apps.session.models import SessionLegislativeTerm, SessionMeeting

        namen = self._sitzungsnamen()
        ergebnis: list[_Sitzung] = []
        neue: list[SessionMeeting] = []
        naechste_kommende = sorted({s.tag for s in self.plan.sitzungen if s.tag > 0})[:6]
        for sitzung in self.plan.sitzungen:
            gremium = GREMIUM[sitzung.gremium]
            if sitzung.basis and sitzung.key in BASIS_SITZUNG_SESSION:
                vorhanden = SessionMeeting.objects.filter(
                    tenant=self.mandant, name=BASIS_SITZUNG_SESSION[sitzung.key]
                ).first()
                if vorhanden is not None:
                    ergebnis.append(_Sitzung(sitzung, vorhanden, list(sitzung.tops), neu=False))
                continue
            beginn = self._termin(sitzung.tag)
            vergangen = sitzung.tag < 0
            einladung = not vergangen and sitzung.tag in naechste_kommende
            meeting = SessionMeeting(
                tenant=self.mandant,
                organization=self.gremien[sitzung.gremium],
                name=namen[sitzung.key],
                legislative_term=cast(Any, SessionLegislativeTerm).for_date(self.mandant, beginn.date()),
                start=beginn,
                end=beginn + timedelta(hours=3),
                actual_start=beginn + timedelta(minutes=3) if vergangen else None,
                actual_end=beginn + timedelta(hours=2, minutes=10 + stabil("dauer", sitzung.key) % 50)
                if vergangen
                else None,
                room=gremium.raum,
                meeting_state="completed" if vergangen else ("invitation_sent" if einladung else "scheduled"),
                is_public=True,
                invitation_sent_at=min(beginn - timedelta(days=9), self.jetzt - timedelta(hours=2))
                if vergangen or einladung
                else None,
                created_by=self.nutzer["verwaltung"],
                **ORT,
            )
            neue.append(meeting)
            tops = list(sitzung.tops) if not sitzung.basis else self._mit_standard(sitzung.tops)
            ergebnis.append(_Sitzung(sitzung, meeting, tops, neu=True))
        SessionMeeting.objects.bulk_create(neue, batch_size=BATCH)
        self._zaehlen("Sitzungen", len(neue))
        self._zaehlen("Sitzungen der Basisdemo mit weiteren TOPs", sum(1 for s in ergebnis if not s.neu))
        return ergebnis

    # ------------------------------------------------------------------
    # Anwesenheit, Tagesordnung, Abstimmungen
    # ------------------------------------------------------------------

    def _anwesenheit(self, sitzungen: list[_Sitzung]) -> None:
        """Vergangene Sitzungen: anwesend oder entschuldigt; kommende mit Einladung: Rückmeldungen."""
        from apps.session.models import SessionAttendance, SessionOrganizationMembership

        besetzung: dict[Any, list[tuple[SessionPerson, str]]] = {}
        for m in SessionOrganizationMembership.objects.filter(organization__tenant=self.mandant).select_related(
            "person"
        ):
            besetzung.setdefault(m.organization_id, []).append((m.person, m.role))
        rolle_in_sitzung = {"chair": "chair", "deputy_chair": "deputy_chair"}
        zeilen: list[SessionAttendance] = []
        for s in sitzungen:
            if not s.neu or not (s.vergangen or s.meeting.meeting_state == "invitation_sent"):
                continue
            for person, rolle in sorted(besetzung.get(s.meeting.organization_id, []), key=lambda e: e[0].family_name):
                zufall = stabil("anwesenheit", s.plan.key, person.email)
                if s.vergangen:
                    status = "excused" if zufall % 11 == 0 else "present"
                    if status == "present":
                        s.anwesend.append(person)
                else:
                    status = "declined" if zufall % 9 == 0 else ("invited" if zufall % 4 == 0 else "confirmed")
                zeilen.append(
                    SessionAttendance(
                        meeting=s.meeting,
                        person=person,
                        status=status,
                        role=rolle_in_sitzung.get(rolle, "member"),
                        has_voting_rights=True,
                        excuse_reason="Beruflich verhindert" if status == "excused" else "",
                        responded_at=None
                        if status == "invited"
                        else min(s.meeting.start - timedelta(days=3), self.jetzt - timedelta(hours=1)),
                    )
                )
        SessionAttendance.objects.bulk_create(zeilen, batch_size=BATCH)
        self._zaehlen("Anwesenheiten und Rückmeldungen", len(zeilen))

    @staticmethod
    def _stimmen(top: Top, anwesend: int) -> tuple[int, int, int]:
        """Stimmen des Plans an die tatsächlich Anwesenden anpassen (Summe = Anwesende)."""
        if top.stimmen is None or anwesend == 0:
            return (0, 0, 0)
        ja, nein, enth = top.stimmen
        enth = min(enth, anwesend)
        if ERGEBNIS[top.ergebnis] == "rejected":
            ja = min(ja, (anwesend - enth - 1) // 2)
            return (max(ja, 0), anwesend - max(ja, 0) - enth, enth)
        nein = min(nein, (anwesend - enth - 1) // 2)
        return (anwesend - max(nein, 0) - enth, max(nein, 0), enth)

    def _tagesordnungen(self, sitzungen: list[_Sitzung]) -> None:
        from apps.session.models import SessionAgendaItem, SessionVote

        beschlussnummern: dict[int, int] = {}
        items: list[SessionAgendaItem] = []
        stimmen: list[SessionVote] = []
        for s in sorted(sitzungen, key=lambda e: e.plan.tag):
            versatz = reihe = 0
            if not s.neu:
                # Hinten an die Tagesordnung der Basisdemo anhängen
                vorhandene = SessionAgendaItem.objects.filter(meeting=s.meeting).values_list("number", "order")
                versatz = max((int(n) for n, _o in vorhandene if str(n).isdigit()), default=0)
                reihe = max((o for _n, o in vorhandene), default=0)
            for i, top in enumerate(s.tops, start=1):
                vorgang = self.plan.vorgang.get(top.vorgang or "")
                ergebnis = ERGEBNIS[top.ergebnis] if s.vergangen else "pending"
                ja, nein, enth = self._stimmen(top, len(s.anwesend)) if s.vergangen else (0, 0, 0)
                namentlich = (
                    s.plan.gremium == "rat"
                    and ja + nein + enth > 0
                    and stabil("namentlich", s.plan.key, top.nummer) % 5 == 0
                )
                beschluss = (
                    vorgang is not None
                    and s.vergangen
                    and top.rolle == "Entscheidung"
                    and ergebnis in ("approved", "rejected")
                )
                felder: dict[str, Any] = {}
                if beschluss:
                    jahr = timezone.localtime(s.meeting.start).year
                    beschlussnummern[jahr] = beschlussnummern.get(jahr, 0) + 1
                    felder["resolution_number"] = f"B/{jahr}/{beschlussnummern[jahr]:04d}"
                umsetzung = self.plan.umsetzung.get(vorgang.key) if vorgang and beschluss else None
                if umsetzung and vorgang is not None and ergebnis == "approved":
                    felder.update(self._umsetzung(vorgang, s, umsetzung))
                item = SessionAgendaItem(
                    meeting=s.meeting,
                    number=str(versatz + i) if not s.neu and top.oeffentlich else top.nummer,
                    name=top.titel,
                    order=(reihe + i) if not s.neu else top.reihenfolge,
                    is_public=top.oeffentlich,
                    paper=self.vorlagen.get(top.vorgang or ""),
                    resolution_text=vorgang.beschluss if vorgang and ergebnis in ("approved", "rejected") else "",
                    protocol_note=PROTOKOLLNOTIZ.get(top.ergebnis, "") if s.vergangen and top.oeffentlich else "",
                    vote_result=ergebnis,
                    votes_yes=ja,
                    votes_no=nein,
                    votes_abstain=enth,
                    voting_method="roll_call" if namentlich else "summary",
                    **felder,
                )
                items.append(item)
                s.items.append(item)
                if namentlich:
                    stimmen.extend(self._namentlich(item, s.anwesend, (ja, nein, enth)))
        SessionAgendaItem.objects.bulk_create(items, batch_size=BATCH)
        SessionVote.objects.bulk_create(stimmen, batch_size=BATCH)
        self._zaehlen("Tagesordnungspunkte", len(items))
        self._zaehlen("Beschlüsse mit Nummer", sum(beschlussnummern.values()))
        self._zaehlen("Einzelstimmen (namentliche Abstimmungen)", len(stimmen))

    def _namentlich(
        self, item: SessionAgendaItem, anwesend: list[SessionPerson], summe: tuple[int, int, int]
    ) -> list[SessionVote]:
        """Einzelstimmen passend zur Summe; Fraktionen stimmen geschlossen (Reihenfolge je TOP gemischt)."""
        from apps.session.models import SessionVote

        fraktion_je_mail = {p.email: p.fraktion or "" for p in PERSONEN}
        reihenfolge = sorted(
            anwesend,
            key=lambda p: (stabil("block", item.name, fraktion_je_mail.get(p.email, "")), p.family_name),
        )
        ja, nein, enth = summe
        stimmen = ["no"] * nein + ["abstain"] * enth + ["yes"] * ja
        return [
            SessionVote(agenda_item=item, person=person, vote=stimme, recorded_by=self.nutzer["protokoll"])
            for person, stimme in zip(reihenfolge, stimmen, strict=False)
        ]

    def _umsetzung(self, vorgang: Vorgang, s: _Sitzung, status: str) -> dict[str, Any]:
        beginn = s.meeting.start
        return {
            "implementation_status": status,
            "implementation_recipient": AMT[vorgang.urheber].name if vorgang.urheber in AMT else "Verwaltung",
            "implementation_deadline": (beginn + timedelta(days=120)).date(),
            "implementation_note": UMSETZUNG_HINWEIS[status],
            "implementation_public": True,
            "implementation_public_note": UMSETZUNG_HINWEIS[status],
            "implementation_updated_at": min(beginn + timedelta(days=21), self.jetzt - timedelta(hours=3)),
            "implementation_updated_by": self.nutzer["verwaltung"],
        }

    # ------------------------------------------------------------------
    # Beratungsfolgen
    # ------------------------------------------------------------------

    def _beratungen(self, sitzungen: list[_Sitzung]) -> None:
        """Eine Station je TOP, dazu offene Stationen ohne Termin für den Rest des Beratungswegs."""
        from apps.session.models import SessionConsultation

        auftritte: dict[str, list[tuple[_Sitzung, Top, SessionAgendaItem]]] = {}
        for s in sorted(sitzungen, key=lambda e: e.plan.tag):
            for top, item in zip(s.tops, s.items, strict=True):
                if top.vorgang:
                    auftritte.setdefault(top.vorgang, []).append((s, top, item))
        zeilen: list[SessionConsultation] = []
        for vorgang in self.plan.vorgaenge:
            liste = auftritte.get(vorgang.key, [])
            reihe = 0
            for s, top, item in liste:
                reihe += 1
                rolle = ROLLE.get(top.rolle, "decision")
                zeilen.append(
                    SessionConsultation(
                        paper=self.vorlagen[vorgang.key],
                        organization=s.meeting.organization,
                        meeting=s.meeting,
                        agenda_item=item,
                        role=rolle,
                        authoritative=rolle == "decision",
                        order=reihe,
                        result=item.vote_result,
                    )
                )
            erreicht = {s.plan.gremium for s, _t, _i in liste}
            for stufe, gremium in enumerate(vorgang.weg):
                if gremium in erreicht:
                    continue
                reihe += 1
                letzte = stufe == len(vorgang.weg) - 1
                kenntnis = vorgang.art in ("anfrage", "mitteilung")
                zeilen.append(
                    SessionConsultation(
                        paper=self.vorlagen[vorgang.key],
                        organization=self.gremien[gremium],
                        role="information" if kenntnis else ("decision" if letzte else "preliminary"),
                        authoritative=letzte and not kenntnis,
                        order=reihe,
                        result="pending",
                    )
                )
        SessionConsultation.objects.bulk_create(zeilen, batch_size=BATCH)
        self._zaehlen("Beratungen (Stationen der Beratungsfolge)", len(zeilen))

    # ------------------------------------------------------------------
    # Niederschriften
    # ------------------------------------------------------------------

    def _niederschrift_text(self, s: _Sitzung) -> str:
        zeilen = [
            f"Niederschrift über die {s.meeting.name}",
            "",
            f"Beginn {timezone.localtime(s.meeting.actual_start or s.meeting.start):%H:%M} Uhr, "
            f"Ende {timezone.localtime(s.meeting.actual_end or s.meeting.end):%H:%M} Uhr, "
            f"{s.meeting.location}, {s.meeting.room}. Anwesend: {len(s.anwesend)} Mitglieder.",
            "",
        ]
        oeffentlich = [(t, i) for t, i in zip(s.tops, s.items, strict=True) if t.oeffentlich]
        for top, item in oeffentlich:
            zeile = f"TOP {item.number} – {top.titel}"
            if top.ergebnis_text:
                zeile += f": {top.ergebnis_text}"
            if item.votes_yes or item.votes_no or item.votes_abstain:
                zeile += f" (Ja {item.votes_yes}, Nein {item.votes_no}, Enthaltungen {item.votes_abstain})"
            zeilen.append(zeile + ".")
            if item.protocol_note:
                zeilen.append(item.protocol_note)
            zeilen.append("")
        if len(oeffentlich) < len(s.tops):
            zeilen.append("Der nicht öffentliche Teil ist in einer gesonderten Niederschrift festgehalten.")
        return "\n".join(zeilen).strip()

    def _niederschriften(self, sitzungen: list[_Sitzung]) -> None:
        """Genehmigt in der Folgesitzung desselben Gremiums; die jeweils jüngste liegt noch zur Prüfung."""
        from apps.session.models import SessionProtocol

        vergangen = [s for s in sorted(sitzungen, key=lambda e: e.plan.tag) if s.neu and s.vergangen]
        folgesitzung: dict[int, _Sitzung] = {}
        je_gremium: dict[str, list[_Sitzung]] = {}
        for s in vergangen:
            je_gremium.setdefault(s.plan.gremium, []).append(s)
        for liste in je_gremium.values():
            for frueher, spaeter in zip(liste, liste[1:], strict=False):
                folgesitzung[id(frueher)] = spaeter
        vorsitz = self._vorsitze()
        niederschriften = []
        for s in vergangen:
            folge = folgesitzung.get(id(s))
            genehmigung = next(
                (i for i in (folge.items if folge else []) if i.name.startswith(NIEDERSCHRIFT_TOP)), None
            )
            chair = vorsitz.get(s.meeting.organization_id)
            niederschriften.append(
                SessionProtocol(
                    meeting=s.meeting,
                    content=self._niederschrift_text(s),
                    status="approved" if folge else "review",
                    created_by=self.nutzer["protokoll"],
                    review_requested_by=self.nutzer["protokoll"],
                    review_requested_at=s.meeting.start + timedelta(days=5),
                    approved_by=self.nutzer["verwaltung"] if folge else None,
                    approved_at=folge.meeting.start + timedelta(hours=1) if folge else None,
                    approval_mode="follow_up" if folge else "",
                    approval_meeting=folge.meeting if folge else None,
                    approval_agenda_item=genehmigung,
                    approval_note=f"Genehmigt in der {folge.meeting.name}." if folge else "",
                    chair_name=f"{chair.given_name} {chair.family_name}" if chair else "",
                    recorder_name=f"{DEMO_USERS['protokoll']['first_name']} {DEMO_USERS['protokoll']['last_name']}",
                )
            )
        SessionProtocol.objects.bulk_create(niederschriften, batch_size=BATCH)
        self._zaehlen("Niederschriften", len(niederschriften))

    def _vorsitze(self) -> dict[Any, SessionPerson]:
        """Vorsitz je Gremium aus der Besetzung (Rat: Bürgermeisterin) – für die Unterschrift der Niederschrift."""
        from apps.session.models import SessionOrganizationMembership

        return {
            m.organization_id: m.person
            for m in SessionOrganizationMembership.objects.filter(
                organization__tenant=self.mandant, role="chair"
            ).select_related("person")
        }
