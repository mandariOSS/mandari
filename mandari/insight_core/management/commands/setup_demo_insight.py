# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Bestand der Musterstadt für Insight (Issue #99): Rat, Ausschüsse, Fraktionen, Personen, ein Jahr Sitzungen mit
Tagesordnungen, Vorlagen mit Beratungsfolgen, Beschlüssen, Abstimmungen, Umsetzungsstand, PDF-Dateien und Orten.

Grundlage ist ``apps.common.demo_stadt.stadtplan``; ``setup_demo_environment`` ruft den Befehl nach dem Aufbau der
Kommune auf und gibt Aufbautag und Umfang weiter. Alle Kennungen des Bestands beginnen mit ``b-``
(``…/oparl/<art>/b-…``). Ein wiederholter Lauf aktualisiert den Bestand an Ort und Stelle (``bulk_create`` mit
``update_conflicts`` auf ``external_id``) und entfernt nur, was der Plan nicht mehr enthält. So bleiben die Kennungen
(kanonische IDs aus ``external_id``) gleich, und Positionen und Notizen der Fraktion in Work, die auf
Tagesordnungspunkte des Bestands zeigen (geschützte Fremdschlüssel), bleiben gültig. Die Geschichten der Basisdemo
bleiben unberührt, nur ihre Sitzungen bekommen weitere Tagesordnungspunkte.

    python manage.py setup_demo_insight [--aufbautag 2026-11-11] [--umfang voll|klein]
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import models, transaction
from django.db.models import ProtectedError
from django.utils import timezone

from apps.common.demo_daten import DEMO_START_DATE, demo_ext, termin_am_sitzungstag
from apps.common.demo_pdf import demo_pdf
from apps.common.demo_stadt import (
    BESTAND,
    BUERGERMEISTERIN,
    FRAKTIONEN,
    GREMIEN,
    GREMIUM,
    ORTE,
    PERSONEN,
    UMFAENGE,
    Sitzung,
    Stadtplan,
    Top,
    beratung_kennung,
    demo_umfang,
    drucksachentext,
    mitgliedschaft_kennung,
    stadtplan,
    top_kennung,
)
from insight_core.models import (
    OParlAgendaItem,
    OParlBody,
    OParlConsultation,
    OParlFile,
    OParlMeeting,
    OParlMembership,
    OParlOrganization,
    OParlPaper,
    OParlPerson,
    PaperLocation,
)

#: Unterordner der Demo-Dateien im Media-Verzeichnis (wie die Basisdemo; ``--reset`` räumt ``demo-*.pdf`` ab)
MEDIA_UNTERORDNER = "demo"
BULK = 500
#: Abstimmungsergebnis in Insight je Ergebnis des Plans (Empfehlungen sind angenommene Beschlussempfehlungen)
VOTE_RESULT = {
    "approved": "approved",
    "recommended": "approved",
    "rejected": "rejected",
    "deferred": "deferred",
    "withdrawn": "withdrawn",
    "noted": "noted",
}
UMSETZUNG_HINWEIS = {
    "open": "Die Umsetzung ist noch nicht begonnen.",
    "in_progress": "Die Verwaltung setzt den Beschluss um; ein Sachstandsbericht folgt.",
    "done": "Der Beschluss ist umgesetzt.",
    "deferred": "Die Umsetzung ist aus Haushaltsgründen zurückgestellt.",
}


def _praefix(art: str) -> str:
    return demo_ext(art, BESTAND)


class Command(BaseCommand):
    help = "Ergänzt die Demo-Kommune Musterstadt in Insight um ein Jahr Ratsarbeit (Bestand aus demo_stadt)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--aufbautag", type=date.fromisoformat, default=None, help="Bezugstag (JJJJ-MM-TT)")
        parser.add_argument("--umfang", choices=UMFAENGE, default=None, help="voll (Demo) oder klein (Tests)")

    def handle(self, *args: Any, **options: Any) -> None:
        body = OParlBody.objects.filter(external_id=demo_ext("body", "musterstadt")).first()
        if body is None:
            raise CommandError("Demo-Kommune fehlt – zuerst python manage.py setup_demo_environment ausführen.")
        plan = stadtplan(options["aufbautag"] or timezone.localdate(), options["umfang"] or demo_umfang())
        self.jetzt = timezone.now()
        self.zaehler: dict[str, int] = {}
        self.behalten: dict[type[models.Model], set[str]] = {}
        with transaction.atomic():
            orgs = self._organisationen(body)
            personen = self._personen(body)
            self._mitgliedschaften(plan, orgs, personen)
            papiere = self._vorgaenge(body, plan)
            sitzungen = self._sitzungen(body, plan, orgs)
            self._tagesordnungen(body, plan, sitzungen, papiere, personen)
            self._dateien(body, plan, papiere, sitzungen)
            self._orte(body, plan, papiere)
            self._veraltetes_entfernen()
        self._urheber(papiere)
        self.stdout.write(self.style.SUCCESS("Musterstadt-Bestand in Insight:"))
        for name, anzahl in self.zaehler.items():
            self.stdout.write(f"  - Insight: {name}: {anzahl}")

    def _zaehlen(self, name: str, anzahl: int) -> None:
        self.zaehler[name] = self.zaehler.get(name, 0) + anzahl

    def _termin(self, plan: Stadtplan, tag: int) -> datetime:
        return termin_am_sitzungstag(plan.aufbautag, tag)

    # ------------------------------------------------------------------
    # Aufräumen, Stammdaten
    # ------------------------------------------------------------------

    def _speichern(self, modell: type[models.Model], zeilen: list[Any]) -> None:
        """Anlegen oder aktualisieren über ``external_id``; die Kennung (kanonische ID) bleibt dabei gleich."""
        felder = [
            f.name
            for f in modell._meta.concrete_fields
            if not f.primary_key and f.name not in ("external_id", "created_at")
        ]
        cast(Any, modell)._default_manager.bulk_create(
            zeilen, batch_size=BULK, update_conflicts=True, unique_fields=["external_id"], update_fields=felder
        )
        self.behalten.setdefault(modell, set()).update(z.external_id for z in zeilen)

    def _veraltetes_entfernen(self) -> None:
        """Was der Plan nicht mehr enthält (anderer Umfang), entfernen – Beratungen und Dateien zuerst."""
        arten = [
            (OParlConsultation, "consultation"),
            (OParlFile, "file"),
            (OParlAgendaItem, "agendaitem"),
            (OParlMeeting, "meeting"),
            (OParlPaper, "paper"),
            (OParlMembership, "membership"),
        ]
        for modell, art in arten:
            veraltet = (
                cast(Any, modell)
                ._default_manager.filter(external_id__startswith=_praefix(art))
                .exclude(external_id__in=self.behalten.get(modell, set()))
            )
            try:
                with transaction.atomic():
                    anzahl, _ = veraltet.delete()
            except ProtectedError:
                # Work verweist noch darauf (Positionen); setup_demo_work_bestand räumt dort beim nächsten Lauf auf
                self.stderr.write(f"Veraltete Einträge ({art}) bleiben, Work verweist noch darauf.")
                continue
            if anzahl:
                self._zaehlen("Veraltete Einträge entfernt", anzahl)
        ordner = Path(settings.MEDIA_ROOT) / MEDIA_UNTERORDNER
        aktuell = {f"demo-{ext.rsplit('/', 1)[-1]}.pdf" for ext in self.behalten.get(OParlFile, set())}
        if ordner.is_dir():
            for datei in ordner.glob(f"demo-{BESTAND}*.pdf"):
                if datei.name not in aktuell:
                    datei.unlink(missing_ok=True)

    def _organisationen(self, body: OParlBody) -> dict[str, OParlOrganization]:
        """Gremien und Fraktionen; die der Basisdemo bestehen schon und bleiben, wie sie sind."""
        orgs: dict[str, OParlOrganization] = {}
        eintraege = [(g.key, g.name, g.kurz, g.art, "Gremium", g.basis) for g in GREMIEN]
        eintraege += [(f.key, f.name, f.kurz, f.art, "Fraktion", f.basis) for f in FRAKTIONEN]
        neu = 0
        for key, name, kurz, klasse, typ, basis in eintraege:
            ext = demo_ext("organization", key)
            if basis:
                org = OParlOrganization.objects.filter(external_id=ext).first()
                if org is not None:
                    orgs[key] = org
                    continue
            orgs[key], _ = OParlOrganization.objects.update_or_create(
                external_id=ext,
                defaults={
                    "body": body,
                    "name": name,
                    "short_name": kurz,
                    "organization_type": typ,
                    "classification": klasse,
                    "deleted": False,
                    "oparl_created": self.jetzt,
                    "oparl_modified": self.jetzt,
                },
            )
            neu += 1
        self._zaehlen("Gremien und Fraktionen (neu)", neu)
        return orgs

    def _personen(self, body: OParlBody) -> dict[str, OParlPerson]:
        personen: dict[str, OParlPerson] = {}
        for p in PERSONEN:
            ext = demo_ext("person", p.key)
            vorhanden = OParlPerson.objects.filter(external_id=ext).first() if p.basis else None
            if vorhanden is not None:
                personen[p.key] = vorhanden
                continue
            personen[p.key], _ = OParlPerson.objects.update_or_create(
                external_id=ext,
                defaults={
                    "body": body,
                    "name": p.name,
                    "given_name": p.vorname,
                    "family_name": p.nachname,
                    "title": p.titel or None,
                    "gender": p.geschlecht,
                    "email": p.email,
                    "deleted": False,
                    "oparl_created": self.jetzt,
                    "oparl_modified": self.jetzt,
                },
            )
        self._zaehlen("Personen (gesamt)", len(personen))
        return personen

    def _mitgliedschaften(
        self, plan: Stadtplan, orgs: dict[str, OParlOrganization], personen: dict[str, OParlPerson]
    ) -> None:
        """Rat und Fraktion für die neuen Personen, Ausschüsse laut Besetzung (auch für Personen der Basisdemo)."""
        zeilen: list[OParlMembership] = []

        def mitglied(person: str, org: str, rolle: str, stimmrecht: bool = True) -> None:
            zeilen.append(
                OParlMembership(
                    external_id=demo_ext("membership", mitgliedschaft_kennung(person, org)),
                    person=personen[person],
                    organization=orgs[org],
                    role=rolle,
                    voting_right=stimmrecht,
                    start_date=DEMO_START_DATE,
                    deleted=False,
                    oparl_created=self.jetzt,
                    oparl_modified=self.jetzt,
                )
            )

        for p in PERSONEN:
            if p.basis:
                continue
            weiblich = p.geschlecht == "female"
            if p.key == BUERGERMEISTERIN:
                mitglied(p.key, "rat", "Vorsitzende (Bürgermeisterin)")
                mitglied(p.key, "hauptausschuss", "Vorsitzende (Bürgermeisterin)")
                continue
            if p.rat:
                mitglied(p.key, "rat", "Ratsmitglied")
            if p.fraktion:
                rolle = "Mitglied" if p.rat else ("Sachkundige Bürgerin" if weiblich else "Sachkundiger Bürger")
                mitglied(p.key, p.fraktion, rolle, stimmrecht=p.rat)
        for m in plan.besetzung:
            mitglied(m.person, m.gremium, m.rolle)
        self._speichern(OParlMembership, zeilen)
        self._zaehlen("Mitgliedschaften (Bestand)", len(zeilen))

    # ------------------------------------------------------------------
    # Vorgänge, Sitzungen, Tagesordnungen
    # ------------------------------------------------------------------

    def _vorgaenge(self, body: OParlBody, plan: Stadtplan) -> dict[str, OParlPaper]:
        zeilen = []
        for v in plan.vorgaenge:
            orte = [
                {"lat": ORTE[o][1], "lon": ORTE[o][2], "name": ORTE[o][0], "source": "manual", "confidence": 1.0}
                for o in v.orte
            ]
            zeilen.append(
                OParlPaper(
                    external_id=demo_ext("paper", v.key),
                    body=body,
                    name=v.titel,
                    reference=v.nummer,
                    paper_type=v.oparl_art,
                    date=plan.datum(v.tag),
                    locations=orte or None,
                    georef_status="completed" if orte else "no_locations",
                    georef_method="manual" if orte else None,
                    # Wie eine OParl-Quelle: Hauptdatei und antragstellende Fraktion (Insight ermittelt daraus den
                    # Urheber, Filter „Anträge der Fraktion …“)
                    raw_json={
                        "id": demo_ext("paper", v.key),
                        "mainFile": demo_ext("file", v.key),
                        **({"originatorOrganization": [demo_ext("organization", v.urheber)]} if v.von_fraktion else {}),
                    },
                    deleted=False,
                    oparl_created=self.jetzt,
                    oparl_modified=self.jetzt,
                )
            )
        self._speichern(OParlPaper, zeilen)
        self._zaehlen("Vorlagen", len(zeilen))
        return {v.key: p for v, p in zip(plan.vorgaenge, zeilen, strict=True)}

    def _sitzungen(
        self, body: OParlBody, plan: Stadtplan, orgs: dict[str, OParlOrganization]
    ) -> dict[str, OParlMeeting]:
        sitzungen: dict[str, OParlMeeting] = {}
        neue: list[tuple[Sitzung, OParlMeeting]] = []
        for s in plan.sitzungen:
            if s.basis:
                vorhanden = OParlMeeting.objects.filter(external_id=demo_ext("meeting", s.key)).first()
                if vorhanden is not None:
                    sitzungen[s.key] = vorhanden
                continue
            gremium = GREMIUM[s.gremium]
            beginn = self._termin(plan, s.tag)
            genehmigt = self._termin(plan, s.tag + 42).date() if s.tag + 42 < 0 else None
            meeting = OParlMeeting(
                external_id=demo_ext("meeting", s.key),
                body=body,
                name=s.name,
                meeting_state="durchgeführt" if s.vergangen else "terminiert",
                start=beginn,
                end=beginn + timedelta(hours=2 if gremium.key != "rat" else 4),
                location_name=f"Rathaus Musterstadt, {gremium.raum}",
                location_address="Rathausplatz 1, 12345 Musterstadt",
                cancelled=False,
                protocol_approval_mode="follow_up" if genehmigt else None,
                protocol_approved_on=genehmigt,
                deleted=False,
                oparl_created=self.jetzt,
                oparl_modified=self.jetzt,
            )
            neue.append((s, meeting))
            sitzungen[s.key] = meeting
        self._speichern(OParlMeeting, [m for _s, m in neue])
        Verbindung = OParlMeeting.organizations.through
        Verbindung.objects.filter(oparlmeeting_id__in=[m.pk for _s, m in neue]).delete()
        Verbindung.objects.bulk_create(
            [Verbindung(oparlmeeting_id=m.pk, oparlorganization_id=orgs[s.gremium].pk) for s, m in neue],
            batch_size=BULK,
        )
        self._zaehlen("Sitzungen (Bestand)", len(neue))
        return sitzungen

    def _tagesordnungen(
        self,
        body: OParlBody,
        plan: Stadtplan,
        sitzungen: dict[str, OParlMeeting],
        papiere: dict[str, OParlPaper],
        personen: dict[str, OParlPerson],
    ) -> None:
        tops: list[OParlAgendaItem] = []
        beratungen: list[OParlConsultation] = []
        beschluesse: dict[int, int] = {}
        for s in plan.sitzungen:
            meeting = sitzungen.get(s.key)
            if meeting is None:
                continue
            for t in s.tops:
                ext = demo_ext("agendaitem", top_kennung(s, t))
                vorgang = plan.vorgang.get(t.vorgang or "")
                entscheidung = t.rolle == "Entscheidung" and t.ergebnis in ("approved", "rejected")
                nummer = None
                if entscheidung:
                    jahr = plan.datum(s.tag).year
                    beschluesse[jahr] = beschluesse.get(jahr, 0) + 1
                    nummer = f"B/{jahr}/{beschluesse[jahr]:03d}"
                umsetzung = plan.umsetzung.get(vorgang.key) if vorgang and entscheidung else None
                namentlich = entscheidung and s.gremium == "rat" and t.ergebnis == "rejected" and t.stimmen
                tops.append(
                    OParlAgendaItem(
                        external_id=ext,
                        meeting=meeting,
                        number=t.nummer,
                        order=t.reihenfolge,
                        name=t.titel,
                        public=t.oeffentlich,
                        result=(t.ergebnis_text or None) if t.oeffentlich else None,
                        resolution_text=vorgang.beschluss if vorgang and t.ergebnis == "approved" else None,
                        resolution_number=nummer,
                        vote_method=("roll_call" if namentlich else "summary") if t.stimmen else None,
                        vote_result=VOTE_RESULT.get(t.ergebnis) if t.ergebnis and t.oeffentlich else None,
                        votes_yes=t.stimmen[0] if t.stimmen else None,
                        votes_no=t.stimmen[1] if t.stimmen else None,
                        votes_abstain=t.stimmen[2] if t.stimmen else None,
                        roll_call=self._namentlich(t, vorgang.urheber if vorgang else "") if namentlich else None,
                        implementation_status=umsetzung,
                        implementation_deadline=(
                            plan.datum(s.tag + 180) if umsetzung in ("open", "in_progress") else None
                        ),
                        implementation_public_note=UMSETZUNG_HINWEIS.get(umsetzung or ""),
                        implementation_modified=self.jetzt if umsetzung else None,
                        deleted=False,
                        oparl_created=self.jetzt,
                        oparl_modified=self.jetzt,
                    )
                )
                if vorgang is not None:
                    beratungen.append(
                        OParlConsultation(
                            external_id=demo_ext("consultation", beratung_kennung(s, t)),
                            body=body,
                            paper=papiere[vorgang.key],
                            paper_external_id=papiere[vorgang.key].external_id,
                            meeting_external_id=meeting.external_id,
                            agenda_item_external_id=ext,
                            role=t.rolle,
                            authoritative=t.rolle == "Entscheidung",
                            deleted=False,
                            oparl_created=self.jetzt,
                            oparl_modified=self.jetzt,
                        )
                    )
        self._speichern(OParlAgendaItem, tops)
        self._speichern(OParlConsultation, beratungen)
        self._zaehlen("Tagesordnungspunkte (Bestand)", len(tops))
        self._zaehlen("Beratungen", len(beratungen))
        self._zaehlen("Beschlüsse mit Umsetzungsstand", sum(1 for t in tops if t.implementation_status))

    def _namentlich(self, top: Top, urheber: str) -> list[dict[str, str]]:
        """Einzelstimmen einer namentlichen Abstimmung im Rat passend zu den Summen (Antragsteller zuerst Ja)."""
        ja, nein, enthaltung = top.stimmen or (0, 0, 0)
        rat = sorted(
            (p for p in PERSONEN if p.rat),
            key=lambda p: (p.fraktion != urheber, p.fraktion or "", p.nachname),
        )
        stimmen = ["yes"] * ja + ["no"] * nein + ["abstain"] * enthaltung
        return [{"name": p.name, "vote": stimme} for p, stimme in zip(rat, stimmen, strict=False)]

    # ------------------------------------------------------------------
    # Dateien, Orte, Urheber
    # ------------------------------------------------------------------

    def _dateien(
        self,
        body: OParlBody,
        plan: Stadtplan,
        papiere: dict[str, OParlPaper],
        sitzungen: dict[str, OParlMeeting],
    ) -> None:
        ordner = Path(settings.MEDIA_ROOT) / MEDIA_UNTERORDNER
        ordner.mkdir(parents=True, exist_ok=True)
        basis_url = getattr(settings, "SITE_URL", "").rstrip("/")
        media_url = settings.MEDIA_URL if settings.MEDIA_URL.startswith("/") else f"/{settings.MEDIA_URL}"
        zeilen: list[OParlFile] = []

        def datei(key: str, name: str, zeilen_text: list[str], datum: date, **ziel: Any) -> None:
            dateiname = f"demo-{key}.pdf"
            inhalt, seiten = demo_pdf(name, zeilen_text, kopf="Stadt Musterstadt (Demo)")
            (ordner / dateiname).write_bytes(inhalt)
            url = f"{basis_url}{media_url}{MEDIA_UNTERORDNER}/{dateiname}"
            zeilen.append(
                OParlFile(
                    external_id=demo_ext("file", key),
                    body=body,
                    name=name,
                    file_name=dateiname,
                    mime_type="application/pdf",
                    size=len(inhalt),
                    access_url=url,
                    download_url=url,
                    file_date=timezone.make_aware(datetime.combine(datum, datetime.min.time())),
                    text_content="\n".join(zeilen_text),
                    text_extraction_status="completed",
                    text_extraction_method="none",
                    text_extracted_at=self.jetzt,
                    page_count=seiten,
                    deleted=False,
                    oparl_created=self.jetzt,
                    oparl_modified=self.jetzt,
                    **ziel,
                )
            )

        for v in plan.vorgaenge:
            datei(v.key, f"{v.oparl_art} {v.nummer}", drucksachentext(v), plan.datum(v.tag), paper=papiere[v.key])
        for s in plan.sitzungen:
            meeting = sitzungen.get(s.key)
            if meeting is None or s.basis:
                continue
            gremium = GREMIUM[s.gremium]
            termin = timezone.localtime(meeting.start).strftime("%d.%m.%Y, %H:%M Uhr") if meeting.start else ""
            kopf = [f"{s.name}", f"am {termin}", f"Rathaus Musterstadt, {gremium.raum}", ""]
            oeffentlich = [t for t in s.tops if t.oeffentlich]
            einladung = [*kopf, "Tagesordnung – öffentlicher Teil"]
            einladung += [f"TOP {t.nummer}  {t.titel}" for t in oeffentlich]
            if len(oeffentlich) < len(s.tops):
                einladung += ["", "Nicht öffentlicher Teil", "Die Tagesordnung erhalten nur die Mitglieder."]
            einladung += ["", "Mit freundlichen Grüßen", "Dr. Miriam Mahler, Bürgermeisterin"]
            datei(
                f"{BESTAND}einladung-{s.key.removeprefix(BESTAND)}",
                f"Einladung: {s.name}",
                einladung,
                plan.datum(s.tag - 10),
                meeting=meeting,
            )
            if s.vergangen:
                niederschrift = [*kopf, "Niederschrift – öffentlicher Teil", ""]
                for t in oeffentlich:
                    niederschrift.append(f"TOP {t.nummer}  {t.titel}")
                    if t.ergebnis_text:
                        stimmen = ""
                        if t.stimmen:
                            stimmen = f" (Ja {t.stimmen[0]}, Nein {t.stimmen[1]}, Enthaltung {t.stimmen[2]})"
                        niederschrift.append(f"Ergebnis: {t.ergebnis_text}{stimmen}.")
                    niederschrift.append("")
                niederschrift += ["Die Vorsitzende schließt die Sitzung.", "", "Vorsitz und Schriftführung"]
                datei(
                    f"{BESTAND}niederschrift-{s.key.removeprefix(BESTAND)}",
                    f"Niederschrift: {s.name}",
                    niederschrift,
                    plan.datum(s.tag + 14),
                    meeting=meeting,
                )
        self._speichern(OParlFile, zeilen)
        self._zaehlen("Dateien (PDF)", len(zeilen))

    def _orte(self, body: OParlBody, plan: Stadtplan, papiere: dict[str, OParlPaper]) -> None:
        """Orte für Karte und Umkreissuche wie eine bestätigte Verortung von Hand (wie ``_verorten`` der Basisdemo)."""
        PaperLocation.objects.filter(paper__external_id__startswith=_praefix("paper")).delete()
        zeilen = [
            PaperLocation(
                paper=papiere[v.key],
                body=body,
                name=ORTE[o][0],
                source="manual",
                confidence=1.0,
                latitude=ORTE[o][1],
                longitude=ORTE[o][2],
                status=PaperLocation.STATUS_CONFIRMED,
            )
            for v in plan.vorgaenge
            for o in v.orte
        ]
        PaperLocation.objects.bulk_create(zeilen, batch_size=BULK)
        self._zaehlen("Orte", len(zeilen))

    def _urheber(self, papiere: dict[str, OParlPaper]) -> None:
        """Antragstellende Fraktionen wie im Betrieb ermitteln (Titel „Antrag der Fraktion …“, Insight-Filter)."""
        from insight_core.services import vorgang_urheber

        ergebnisse = vorgang_urheber.ermitteln([p.pk for p in papiere.values()])
        self._zaehlen("Vorgänge mit Urheber-Fraktion", sum(1 for e in ergebnisse.values() if e.fraktionen))
