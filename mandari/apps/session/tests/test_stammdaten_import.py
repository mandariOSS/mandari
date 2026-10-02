# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Stammdaten-Import aus Bestandssystemen (Issue #762, L11a).

- Prüflauf meldet Fehler zeilengenau und schreibt nichts; mit Fehlern schreibt auch der Import nichts.
- Ein zweiter Import derselben Dateien ändert nichts (auch kein neuer Eintrag im Prüfprotokoll).
- Zählabgleich je Objektart stimmt und steht im Bericht; Bankdaten erscheinen nie in Ausgabe oder Bericht.
- Gegenprobe mit den öffentlichen Mitgliederlisten des RIS-Bestands.
"""

from __future__ import annotations

import json
import zipfile
from datetime import date
from io import StringIO
from pathlib import Path
from typing import Any, cast

import pytest
from django.core.management import CommandError, call_command

from apps.session.models import (
    SessionAuditLog,
    SessionLegislativeTerm,
    SessionOrganization,
    SessionOrganizationMembership,
    SessionPerson,
    SessionTenant,
)
from apps.session.services import stammdaten_import
from apps.session.services.stammdaten_tabellen import read_table
from insight_core.models import (
    OParlBody,
    OParlLegislativeTerm,
    OParlMembership,
    OParlOrganization,
    OParlPerson,
    OParlSource,
)

pytestmark = pytest.mark.django_db

IBAN = "DE02120300000000202051"
IBAN_NEU = "DE02100100100006820101"

WAHLPERIODEN = """name;nummer;beginn;ende
Wahlperiode 2021–2026;19;01.11.2021;31.10.2026
Wahlperiode 2026–2031;20;01.11.2026;31.10.2031
"""

GREMIEN = """name;kurzname;art;ausschussart;uebergeordnet;ladungsfrist_tage;sollstaerke;beginn;ende;aktiv
Rat der Gemeinde Musterdorf;Rat;Rat;;;7;15;;;ja
Verwaltungsausschuss;VA;Verwaltungsausschuss;;Rat der Gemeinde Musterdorf;3;7;;;
Ausschuss für Finanzen;FIN;Ausschuss;Finanzausschuss;Rat der Gemeinde Musterdorf;;;;;
"""

FRAKTIONEN = """name;kurzname;beginn;ende;aktiv
Fraktion A;A;01.11.2021;;
Gruppe B/C;B/C;;;
"""

AEMTER = """name;kurzname;uebergeordnet;aktiv
Fachbereich Zentrale Dienste;FB 1;;
Amt für Ratsangelegenheiten;FB 1.1;Fachbereich Zentrale Dienste;
"""

PERSONEN = f"""kennung;anrede;titel;vorname;nachname;email;telefon;adresse;kontoinhaber;iban;bic;zustellweg;mandat_beginn;mandat_ende;aktiv
P1;Frau;Dr.;Erika;Musterfrau;erika@example.org;0123 4567;Am Markt 1, 12345 Musterdorf;Erika Musterfrau;{IBAN};;Portal;01.11.2021;;ja
P2;Herr;;Max;Mustermann;max@example.org;;;;;;E-Mail;01.11.2021;;
P3;;;Lena;Beispiel;;;;;;;Brief;;;
"""

BESETZUNGEN = """person;gremium;funktion;stimmrecht;beginn;ende;wahlperiode;vertretung_fuer
P1;Rat der Gemeinde Musterdorf;Vorsitzende;;01.11.2021;;;
P2;Rat der Gemeinde Musterdorf;Ratsherr;;01.11.2021;;;
P3;Rat der Gemeinde Musterdorf;Grundmandat;;01.11.2021;;Wahlperiode 2021–2026;
P2;Verwaltungsausschuss;stellv. Vorsitz;;01.11.2021;31.10.2026;;
P3;Verwaltungsausschuss;stellvertretendes Mitglied;;01.11.2021;;;P2
P1;Fraktion A;Vorsitz;;01.11.2021;;;
P2;Ausschuss für Finanzen;Mitglied;nein;01.11.2021;;;
"""


def write(directory: Path, name: str, content: str, encoding: str = "utf-8") -> None:
    (directory / name).write_text(content, encoding=encoding, newline="")


@pytest.fixture
def tenant() -> SessionTenant:
    return SessionTenant.objects.create(name="Gemeinde Musterdorf", slug="musterdorf")


@pytest.fixture
def daten(tmp_path: Path) -> Path:
    write(tmp_path, "wahlperioden.csv", WAHLPERIODEN)
    write(tmp_path, "gremien.csv", GREMIEN)
    # Excel unter Windows speichert CSV in Windows-1252
    write(tmp_path, "fraktionen.csv", FRAKTIONEN, encoding="cp1252")
    write(tmp_path, "aemter.csv", AEMTER, encoding="utf-8-sig")
    write(tmp_path, "personen.csv", PERSONEN)
    write(tmp_path, "besetzungen.csv", BESETZUNGEN)
    return tmp_path


def run(*args: Any) -> str:
    out = StringIO()
    call_command("session_stammdaten_import", *args, stdout=out)
    return out.getvalue()


def stock(tenant: SessionTenant) -> dict[str, int]:
    return {kind: stammdaten_import._stock(tenant, kind) for kind in stammdaten_import.KINDS}


def test_vorlagen_enthalten_alle_spalten(tmp_path: Path) -> None:
    run("--vorlagen", str(tmp_path / "vorlagen"))
    for kind, columns in stammdaten_import.COLUMNS.items():
        table = read_table(tmp_path / "vorlagen" / f"{kind}.csv")
        assert table.header == [c.key for c in columns]
        assert table.rows == []


def test_pruefung_schreibt_nichts(tenant: SessionTenant, daten: Path) -> None:
    audit_before = SessionAuditLog.objects.filter(tenant=tenant).count()
    out = run("--tenant", "musterdorf", str(daten), "--dry-run")
    assert "Prüflauf, nichts geschrieben" in out
    assert "Prüflauf ohne Fehler" in out
    assert all(count == 0 for count in stock(tenant).values())
    assert SessionAuditLog.objects.filter(tenant=tenant).count() == audit_before
    plan = stammdaten_import.plan_import(tenant, daten)
    assert plan.tallies["personen"].created == 3
    assert plan.tallies["besetzungen"].created == 7
    assert plan.tallies["besetzungen"].after == 7


def test_import_legt_alles_an_und_ist_wiederholbar(tenant: SessionTenant, daten: Path) -> None:
    out = run("--tenant", "musterdorf", str(daten), "--bericht", str(daten / "bericht.json"))
    assert "Import ausgeführt." in out
    assert stock(tenant) == {
        "wahlperioden": 2,
        "gremien": 3,
        "fraktionen": 2,
        "aemter": 2,
        "personen": 3,
        "besetzungen": 7,
    }

    term = SessionLegislativeTerm.objects.get(tenant=tenant, number=19)
    assert term.start_date == date(2021, 11, 1)
    rat = SessionOrganization.objects.get(tenant=tenant, name="Rat der Gemeinde Musterdorf")
    assert rat.organization_type == "council"
    assert rat.target_member_count == 15
    va = SessionOrganization.objects.get(tenant=tenant, name="Verwaltungsausschuss")
    assert va.organization_type == "committee"
    assert va.committee_kind == SessionOrganization.COMMITTEE_KIND_MAIN
    assert va.parent == rat
    assert va.invitation_period_days == 3
    fin = SessionOrganization.objects.get(tenant=tenant, name="Ausschuss für Finanzen")
    assert fin.committee_kind == SessionOrganization.COMMITTEE_KIND_FINANCE
    assert SessionOrganization.objects.get(tenant=tenant, name="Gruppe B/C").organization_type == "faction"
    amt = SessionOrganization.objects.get(tenant=tenant, name="Amt für Ratsangelegenheiten")
    assert amt.organization_type == "department"
    assert amt.parent is not None and amt.parent.name == "Fachbereich Zentrale Dienste"

    erika = SessionPerson.objects.get(tenant=tenant, family_name="Musterfrau")
    person = cast(Any, erika)
    assert erika.title == "Dr."
    assert erika.delivery_channel == "portal"
    assert person.get_bank_iban_decrypted() == IBAN
    assert person.get_phone_decrypted() == "0123 4567"
    assert IBAN.encode() not in bytes(erika.bank_iban_encrypted or b"")
    assert SessionPerson.objects.get(tenant=tenant, family_name="Beispiel").delivery_channel == "letter"

    memberships = SessionOrganizationMembership.objects.filter(organization__tenant=tenant)
    chair = memberships.get(organization=rat, person=erika)
    assert chair.role == "chair"
    assert chair.has_voting_rights is True
    assert chair.legislative_term == term  # nach dem Beginn
    lena = SessionPerson.objects.get(tenant=tenant, family_name="Beispiel")
    advisor = memberships.get(organization=rat, person=lena)
    assert advisor.role == "advisor"
    assert advisor.has_voting_rights is False
    deputy = memberships.get(organization=va, person__family_name="Mustermann")
    assert deputy.role == "deputy_chair"
    assert deputy.end_date == date(2026, 10, 31)
    substitute = memberships.get(organization=va, person=lena)
    assert substitute.role == "member"
    assert substitute.substitute_for is not None and substitute.substitute_for.family_name == "Mustermann"
    assert memberships.get(organization=fin).has_voting_rights is False

    # Jede Anlage steht im Prüfprotokoll
    audit = SessionAuditLog.objects.filter(tenant=tenant, action="create")
    assert audit.filter(model_name="SessionPerson").count() == 3
    assert audit.filter(model_name="SessionOrganizationMembership").count() == 7

    # Bericht mit Zählabgleich, ohne Bankdaten
    report = json.loads((daten / "bericht.json").read_text(encoding="utf-8"))
    assert report["ausgefuehrt"] is True
    personen = report["zaehlabgleich"]["Personen"]
    assert (personen["datei"], personen["neu"], personen["bestand_vorher"], personen["bestand_nachher"]) == (3, 3, 0, 3)
    assert all(entry["stimmig"] for entry in report["zaehlabgleich"].values())
    assert IBAN not in (daten / "bericht.json").read_text(encoding="utf-8")
    assert IBAN not in out

    # Zweiter Lauf: nichts ändert sich, kein neuer Protokolleintrag
    audit_count = SessionAuditLog.objects.filter(tenant=tenant).count()
    before = stock(tenant)
    plan = stammdaten_import.plan_import(tenant, daten)
    assert not plan.errors
    for kind, tally in plan.tallies.items():
        assert tally.created == tally.updated == 0, kind
        assert tally.unchanged == tally.rows, kind
    out = run("--tenant", "musterdorf", str(daten))
    assert "Import ausgeführt." in out
    assert stock(tenant) == before
    assert SessionAuditLog.objects.filter(tenant=tenant).count() == audit_count


def test_aenderungen_werden_gemeldet_und_leere_zellen_aendern_nichts(tenant: SessionTenant, daten: Path) -> None:
    run("--tenant", "musterdorf", str(daten))
    personen = PERSONEN.replace("erika@example.org", "erika.neu@example.org").replace(IBAN, IBAN_NEU)
    # Telefon und Adresse leer: bleiben erhalten
    personen = personen.replace("0123 4567;Am Markt 1, 12345 Musterdorf", ";")
    write(daten, "personen.csv", personen)

    plan = stammdaten_import.plan_import(tenant, daten)
    changed = [i for i in plan.persons.values() if i.action == stammdaten_import.CHANGED]
    assert [(i.label, i.changes) for i in changed] == [("Erika Musterfrau", ["E-Mail", "IBAN"])]
    out = run("--tenant", "musterdorf", str(daten))
    assert "Erika Musterfrau – E-Mail, IBAN" in out
    assert IBAN_NEU not in out

    erika = cast(Any, SessionPerson.objects.get(tenant=tenant, family_name="Musterfrau"))
    assert erika.email == "erika.neu@example.org"
    assert erika.get_bank_iban_decrypted() == IBAN_NEU
    assert erika.get_phone_decrypted() == "0123 4567"
    assert SessionAuditLog.objects.filter(tenant=tenant, action="update", model_name="SessionPerson").count() == 1


def test_fehler_zeilengenau_und_nichts_geschrieben(tenant: SessionTenant, daten: Path) -> None:
    write(
        daten,
        "personen.csv",
        PERSONEN
        + "P4;;;Ohne;Bank;;;;;DE02120300000000202052;;;;;\n"  # Prüfziffer falsch
        + "P2;;;Doppelt;Kennung;;;;;;;;;;\n"
        + "P5;;;;Ohnevorname;;;;;;;;;;\n"
        + "P6;;;Falsch;Datum;;;;;;;;31.02.2021;;\n",
    )
    write(
        daten,
        "besetzungen.csv",
        BESETZUNGEN
        + "P1;Unbekanntes Gremium;;;;;;\n"
        + "P2;Rat der Gemeinde Musterdorf;Mitglied;;01.05.2024;;;\n"  # überschneidet sich mit Zeile 3
        + "P9;Rat der Gemeinde Musterdorf;;;;;;\n"
        + "P1;Ausschuss für Finanzen;Kassenwart;;;;;\n",
    )

    with pytest.raises(CommandError, match="Fehler – nichts geschrieben"):
        run("--tenant", "musterdorf", str(daten), "--bericht", str(daten / "bericht.txt"))
    assert all(count == 0 for count in stock(tenant).values())

    report = (daten / "bericht.txt").read_text(encoding="utf-8")
    assert "personen.csv, Zeile 5: Ohne Bank: Die IBAN ist ungültig: die Prüfziffer stimmt nicht" in report
    assert "DE02120300000000202052" not in report
    assert "personen.csv, Zeile 6: Kennung „P2“ doppelt (zuerst Zeile 3)." in report
    assert "personen.csv, Zeile 7: Ohnevorname: Vor- und Nachname sind Pflicht." in report
    assert "personen.csv, Zeile 8: Spalte mandat_beginn: „31.02.2021“ ist kein Datum" in report
    assert "besetzungen.csv, Zeile 9: Gremium „Unbekanntes Gremium“ unbekannt" in report
    assert (
        "besetzungen.csv, Zeile 10: Max Mustermann – Rat der Gemeinde Musterdorf: Zeitraum überschneidet sich mit Zeile 3."
        in report
    )
    assert "besetzungen.csv, Zeile 11: Person „P9“ fehlt in der Personendatei." in report
    assert "besetzungen.csv, Zeile 12: Spalte funktion: „Kassenwart“ ist unbekannt" in report

    plan = stammdaten_import.plan_import(tenant, daten)
    personen, besetzungen = plan.tallies["personen"], plan.tallies["besetzungen"]
    assert (personen.rows, personen.created, personen.failed) == (7, 3, 4)
    assert (besetzungen.rows, besetzungen.created, besetzungen.failed) == (11, 7, 4)
    assert personen.balanced and besetzungen.balanced
    with pytest.raises(ValueError):
        stammdaten_import.apply_plan(plan)


def test_ueberschneidung_mit_vorhandener_besetzung_und_wahlperiode(tenant: SessionTenant, daten: Path) -> None:
    run("--tenant", "musterdorf", str(daten))
    write(daten, "wahlperioden.csv", WAHLPERIODEN + "Übergang;;01.10.2026;30.11.2026\n")
    write(
        daten, "besetzungen.csv", "person;gremium;funktion;beginn\nP1;Rat der Gemeinde Musterdorf;Mitglied;01.01.2025\n"
    )
    plan = stammdaten_import.plan_import(tenant, daten)
    messages = [f.text() for f in plan.errors]
    assert "wahlperioden.csv, Zeile 4: Zeitraum überschneidet sich mit „Wahlperiode 2021–2026“." in messages
    assert any(
        m.startswith("besetzungen.csv, Zeile 2: Erika Musterfrau – Rat der Gemeinde Musterdorf: Zeitraum überschneidet")
        and "vorhandenen Besetzung ab 01.11.2021" in m
        for m in messages
    )


def test_namensgleiche_personen_brauchen_die_email(tenant: SessionTenant, tmp_path: Path) -> None:
    for email in ("max.a@example.org", "max.b@example.org"):
        SessionPerson.objects.create(tenant=tenant, given_name="Max", family_name="Mustermann", email=email)
    write(
        tmp_path,
        "personen.csv",
        "kennung;vorname;nachname;email\nP1;Max;Mustermann;\nP2;Max;Mustermann;max.b@example.org\n",
    )
    plan = stammdaten_import.plan_import(tenant, tmp_path)
    assert [f.text() for f in plan.errors] == [
        "personen.csv, Zeile 2: Max Mustermann: Mehrere Personen dieses Namens im Bestand – "
        "E-Mail-Adresse angeben, um zu unterscheiden."
    ]
    assert plan.persons["P2"].action == stammdaten_import.UNCHANGED


def test_spaltennamen_komma_und_bom_werden_erkannt(tenant: SessionTenant, tmp_path: Path) -> None:
    write(
        tmp_path,
        "personen.csv",
        "Kennung,Vorname,Nachname,E-Mail,Mandatsbeginn,Bemerkung\nP1,Ida,Muster,ida@example.org,2021-11-01,x\n",
        encoding="utf-8-sig",
    )
    plan = stammdaten_import.plan_import(tenant, tmp_path)
    assert not plan.errors
    assert [f.text() for f in plan.hints] == ["personen.csv, Zeile 1: Spalten werden nicht übernommen: Bemerkung."]
    item = plan.persons["P1"]
    assert item.values["email"] == "ida@example.org"
    assert item.values["start_date"] == date(2021, 11, 1)


def test_pflichtspalten_und_fehlende_dateien(tenant: SessionTenant, tmp_path: Path) -> None:
    plan = stammdaten_import.plan_import(tenant, tmp_path)
    assert "Keine Importdatei gefunden" in plan.errors[0].message
    write(tmp_path, "personen.csv", "kennung;vorname\nP1;Ida\n")
    write(tmp_path, "besetzungen.xlsx", "")
    write(tmp_path, "besetzungen.csv", "person;gremium\n")
    plan = stammdaten_import.plan_import(tenant, tmp_path)
    texts = [f.text() for f in plan.errors]
    assert "personen.csv, Zeile 1: Pflichtspalten fehlen: nachname." in texts
    assert any("CSV und XLSX zugleich" in t for t in texts)


def _xlsx(path: Path, rows: list[list[Any]], *, date1904: bool = False) -> None:
    """Minimale XLSX-Datei: gemeinsame Zeichenketten, Zahlen, Datum als Seriennummer."""
    strings: list[str] = []
    xml_rows = []
    for r, row in enumerate(rows, start=1):
        cells = []
        for c, value in enumerate(row):
            ref = f"{chr(ord('A') + c)}{r}"
            if value is None:
                continue
            if isinstance(value, (int, float)):
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
            else:
                strings.append(str(value))
                cells.append(f'<c r="{ref}" t="s"><v>{len(strings) - 1}</v></c>')
        xml_rows.append(f'<row r="{r}">{"".join(cells)}</row>')
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "xl/workbook.xml",
            f'<workbook xmlns="{main}" xmlns:r="{rel}"><workbookPr date1904="{int(date1904)}"/>'
            '<sheets><sheet name="Tabelle1" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        )
        archive.writestr(
            "xl/sharedStrings.xml",
            f'<sst xmlns="{main}">' + "".join(f"<si><t>{s}</t></si>" for s in strings) + "</sst>",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            f'<worksheet xmlns="{main}"><sheetData>{"".join(xml_rows)}</sheetData></worksheet>',
        )


def test_xlsx_mit_datum_als_seriennummer(tenant: SessionTenant, tmp_path: Path) -> None:
    _xlsx(
        tmp_path / "wahlperioden.xlsx",
        [["Name", "Nummer", "Beginn", "Ende"], ["Wahlperiode 2021–2026", 19, 44501, "31.10.2026"]],
    )
    plan = stammdaten_import.plan_import(tenant, tmp_path)
    assert not plan.errors
    item = plan.terms["wahlperiode 2021–2026"]
    assert item.values == {
        "name": "Wahlperiode 2021–2026",
        "number": 19,
        "start_date": date(2021, 11, 1),
        "end_date": date(2026, 10, 31),
    }
    _xlsx(tmp_path / "wahlperioden.xlsx", [["name", "beginn"], ["WP", 43039]], date1904=True)
    plan = stammdaten_import.plan_import(tenant, tmp_path)
    assert plan.terms["wp"].values["start_date"] == date(2021, 11, 1)


def test_kaputte_xlsx_wird_gemeldet(tenant: SessionTenant, tmp_path: Path) -> None:
    (tmp_path / "personen.xlsx").write_bytes(b"kein zip")
    plan = stammdaten_import.plan_import(tenant, tmp_path)
    assert [f.text() for f in plan.errors] == ["personen.xlsx: Die Datei ist keine gültige XLSX-Datei."]


def test_gegenprobe_mit_oeffentlichen_mitgliederlisten(tenant: SessionTenant, daten: Path) -> None:
    ris = "https://ratsinfo.example.org/bi/"
    source = OParlSource.objects.create(name="RIS", url=ris)
    body = OParlBody.objects.create(external_id=f"{ris}?__cpanr=2", source=source, name="Gemeinde Musterdorf")
    rat = OParlOrganization.objects.create(
        external_id=f"{ris}kp0040.asp?__kgrnr=2", body=body, name="Rat der Gemeinde Musterdorf"
    )
    for number, name in enumerate(("Dr. Erika Musterfrau", "Mustermann, Max", "Tom Testmann"), start=1):
        person = OParlPerson.objects.create(external_id=f"{ris}pe0051.asp?__kpenr={number}", body=body, name=name)
        OParlMembership.objects.create(
            external_id=f"{rat.external_id}#membership/{number}", person=person, organization=rat
        )

    out = run(
        "--tenant",
        "musterdorf",
        str(daten),
        "--dry-run",
        "--gegenprobe",
        body.external_id,
        "--stichtag",
        "2026-01-15",
        "--bericht",
        str(daten / "bericht.json"),
    )
    assert (
        "Rat der Gemeinde Musterdorf: 2 übereinstimmend; nur öffentlich: Tom Testmann; nur im Import: Lena Beispiel"
        in out
    )
    assert "Verwaltungsausschuss: kein öffentliches Gremium gleichen Namens" in out
    report = json.loads((daten / "bericht.json").read_text(encoding="utf-8"))
    entry = next(e for e in report["gegenprobe"] if e["gremium"] == "Rat der Gemeinde Musterdorf")
    assert entry["nur_oeffentlich"] == ["Tom Testmann"]
    assert entry["nur_import"] == ["Lena Beispiel"]

    with pytest.raises(CommandError, match="nicht im RIS-Bestand"):
        run("--tenant", "musterdorf", str(daten), "--dry-run", "--gegenprobe", "https://unbekannt.example/")


def test_personenschluessel_der_gegenprobe() -> None:
    assert stammdaten_import.person_key("Dr. Erika Musterfrau") == "erika musterfrau"
    assert stammdaten_import.person_key("Mustermann, Max") == "max mustermann"
    assert stammdaten_import.person_key("Prof. Dr.-Ing. Ida  Muster") == "ida muster"
    assert stammdaten_import.person_key("Frau Lena Beispiel") == "lena beispiel"
    assert stammdaten_import.person_key("Beispiel, Frau Dr. Lena") == "lena beispiel"


# ---------------------------------------------------------------- Vorlagen aus dem RIS-Bestand


def _ris_body() -> OParlBody:
    """Öffentlicher Bestand einer Körperschaft, wie ihn der SessionNet-Adapter liefert (nur Namen)."""
    ris = "https://ratsinfo.example.org/bi/"
    source = OParlSource.objects.create(name="RIS", url=ris)
    body = OParlBody.objects.create(external_id=f"{ris}?__cpanr=2", source=source, name="Gemeinde Musterdorf")
    OParlLegislativeTerm.objects.create(
        external_id=f"{ris}?__cpanr=2#legislativeterm/19",
        body=body,
        name="Wahlperiode 2021–2026",
        start_date=date(2021, 11, 1),
        end_date=date(2026, 10, 31),
    )
    org_names = ("Rat der Gemeinde Musterdorf", "Verwaltungsausschuss", "Fraktion A", "=Sonderausschuss")
    orgs = {
        name: OParlOrganization.objects.create(
            external_id=f"{ris}kp0040.asp?__kgrnr={number}", body=body, name=name, organization_type="Gremium"
        )
        for number, name in enumerate(org_names, start=1)
    }
    OParlOrganization.objects.create(
        external_id=f"{ris}kp0040.asp?__kgrnr=9", body=body, name="Alter Ausschuss", deleted=True
    )
    person_names = ("Dr. Erika Musterfrau", "Mustermann, Max", "Hans von der Heide", "Frau Lena Beispiel", "Ehemalige")
    persons = {
        name: OParlPerson.objects.create(
            external_id=f"{ris}pe0051.asp?__cpanr=2&__kpenr={number}", body=body, name=name
        )
        for number, name in enumerate(person_names, start=1)
    }
    rows = [
        ("Dr. Erika Musterfrau", "Rat der Gemeinde Musterdorf", "Ratsvorsitzende", True, None),
        ("Mustermann, Max", "Rat der Gemeinde Musterdorf", "Ratsherr", True, None),
        ("Hans von der Heide", "Rat der Gemeinde Musterdorf", "Mitglieder", True, None),
        ("Frau Lena Beispiel", "Verwaltungsausschuss", "Stellvertretende Mitglieder", True, None),
        ("Mustermann, Max", "Verwaltungsausschuss", "Beratende Mitglieder", False, None),
        ("Dr. Erika Musterfrau", "Fraktion A", "Vorsitz", True, None),
        ("Hans von der Heide", "=Sonderausschuss", "Mitglied", True, None),
        # Am Stichtag nicht mehr laufend: fällt weg (und mit ihr die Person)
        ("Ehemalige", "Rat der Gemeinde Musterdorf", "Mitglied", True, date(2024, 1, 31)),
    ]
    for number, (person, org, role, vote, end) in enumerate(rows, start=1):
        OParlMembership.objects.create(
            external_id=f"{orgs[org].external_id}#membership/{number}",
            person=persons[person],
            organization=orgs[org],
            role=role,
            voting_right=vote,
            end_date=end,
        )
    return body


def test_vorlagen_aus_dem_ris_bestand_lassen_sich_importieren(tenant: SessionTenant, tmp_path: Path) -> None:
    body = _ris_body()
    vorlagen = tmp_path / "vorlagen"
    out = run("--vorlagen", str(vorlagen), "--aus-ris", body.external_id, "--stichtag", "2026-01-15")
    assert "besetzungen.csv (7 Zeilen)" in out
    assert "Kontakt- und Bankdaten ergänzen" in out

    def rows(kind: str) -> list[dict[str, str]]:
        table = read_table(vorlagen / f"{kind}.csv")
        return [dict(zip(table.header, values, strict=True)) for _line, values in table.rows]

    assert [(r["name"], r["beginn"], r["ende"]) for r in rows("wahlperioden")] == [
        ("Wahlperiode 2021–2026", "01.11.2021", "31.10.2026")
    ]
    assert [(r["name"], r["art"], r["ausschussart"]) for r in rows("gremien")] == [
        ("'=Sonderausschuss", "Ausschuss", ""),  # Formel-Anfang entschärft
        ("Rat der Gemeinde Musterdorf", "Rat", ""),
        ("Verwaltungsausschuss", "Ausschuss", "Hauptausschuss"),
    ]
    assert [r["name"] for r in rows("fraktionen")] == ["Fraktion A"]
    assert [(r["anrede"], r["titel"], r["vorname"], r["nachname"]) for r in rows("personen")] == [
        ("Frau", "", "Lena", "Beispiel"),
        ("", "Dr.", "Erika", "Musterfrau"),
        ("", "", "Max", "Mustermann"),
        ("", "", "Hans", "von der Heide"),
    ]
    assert all(r["kennung"].startswith("https://ratsinfo.example.org/bi/pe0051") for r in rows("personen"))
    assert all(r["iban"] == "" and r["telefon"] == "" for r in rows("personen"))
    besetzungen = {(r["gremium"], r["funktion"], r["stimmrecht"], r["wahlperiode"]) for r in rows("besetzungen")}
    assert besetzungen == {
        ("'=Sonderausschuss", "Mitglied", "ja", "Wahlperiode 2021–2026"),
        ("Fraktion A", "Vorsitz", "ja", "Wahlperiode 2021–2026"),
        ("Rat der Gemeinde Musterdorf", "Vorsitz", "ja", "Wahlperiode 2021–2026"),
        ("Rat der Gemeinde Musterdorf", "Mitglied", "ja", "Wahlperiode 2021–2026"),
        ("Verwaltungsausschuss", "stellvertretendes Mitglied", "", "Wahlperiode 2021–2026"),
        ("Verwaltungsausschuss", "beratend", "nein", "Wahlperiode 2021–2026"),
    }

    # Vorbefüllte Vorlagen gehen ohne Nacharbeit durch Prüflauf und Import; die Gegenprobe stimmt überein
    out = run("--tenant", "musterdorf", str(vorlagen), "--gegenprobe", body.external_id, "--stichtag", "2026-01-15")
    assert "Import ausgeführt." in out
    assert "Rat der Gemeinde Musterdorf: 3 übereinstimmend" in out
    assert "nur öffentlich" not in out
    assert "nur im Import" not in out
    assert stock(tenant) == {
        "wahlperioden": 1,
        "gremien": 3,
        "fraktionen": 1,
        "aemter": 0,
        "personen": 4,
        "besetzungen": 7,
    }
    assert SessionOrganization.objects.filter(tenant=tenant, name="=Sonderausschuss").exists()
    heide = SessionPerson.objects.get(tenant=tenant, family_name="von der Heide")
    assert heide.given_name == "Hans"
    terms = SessionOrganizationMembership.objects.filter(organization__tenant=tenant).values_list(
        "legislative_term__name", flat=True
    )
    assert set(terms) == {"Wahlperiode 2021–2026"}

    # Stellvertretung ohne vertretene Person: ohne Stimmrecht, sonst zählte sie zur Beschlussfähigkeit
    vertretung = SessionOrganizationMembership.objects.get(
        organization__tenant=tenant, organization__name="Verwaltungsausschuss", person__family_name="Beispiel"
    )
    assert (vertretung.role, vertretung.has_voting_rights) == ("member", False)
    assert "stellvertretendes Mitglied ohne vertretung_fuer" in out

    # Ein zweiter Lauf ändert nichts
    plan = stammdaten_import.plan_import(tenant, vorlagen)
    assert plan.errors == []
    assert all(t.created == 0 and t.updated == 0 for t in plan.tallies.values())


def test_vorlagen_ueberschreiben_keine_importdateien(tmp_path: Path) -> None:
    write(tmp_path, "personen.xlsx", "kein Inhalt")
    with pytest.raises(CommandError, match=r"schon Importdateien \(personen.xlsx\)"):
        run("--vorlagen", str(tmp_path))
    assert (tmp_path / "personen.xlsx").read_text(encoding="utf-8") == "kein Inhalt"
    assert not (tmp_path / "gremien.csv").exists()
    with pytest.raises(CommandError, match="nur zusammen mit --vorlagen"):
        run("--tenant", "musterdorf", str(tmp_path), "--aus-ris", "https://ratsinfo.example.org/bi/")
    with pytest.raises(CommandError, match="nicht im RIS-Bestand"):
        run("--vorlagen", str(tmp_path / "neu"), "--aus-ris", "https://unbekannt.example/")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Dr. Erika Musterfrau", ("", "Dr.", "Erika", "Musterfrau")),
        ("Mustermann, Max", ("", "", "Max", "Mustermann")),
        ("Musterfrau, Dr. Erika", ("", "Dr.", "Erika", "Musterfrau")),
        ("Herr Prof. Dr.-Ing. Karl  Beispiel", ("Herr", "Prof. Dr.-Ing.", "Karl", "Beispiel")),
        ("Anna Maria van den Berg", ("", "", "Anna Maria", "van den Berg")),
        ("Einname", ("", "", "", "Einname")),
    ],
)
def test_namen_fuer_die_vorlage(name: str, expected: tuple[str, str, str, str]) -> None:
    assert stammdaten_import.split_name(name) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Samtgemeinderat", ("gremien", "Rat", "")),
        ("Samtgemeindeausschuss", ("gremien", "Ausschuss", "Hauptausschuss")),
        ("Ausschuss für Finanzen", ("gremien", "Ausschuss", "")),
        ("Rechnungsprüfungsausschuss", ("gremien", "Ausschuss", "Rechnungsprüfungsausschuss")),
        ("Seniorenbeirat", ("gremien", "Beirat", "")),
        ("Ortsrat Nord", ("gremien", "Rat", "")),
        ("Gruppe B/C", ("fraktionen", "", "")),
        ("Ratsfraktion X", ("fraktionen", "", "")),
        ("Arbeitskreis Dorfentwicklung", ("gremien", "", "")),
    ],
)
def test_gremienarten_fuer_die_vorlage(name: str, expected: tuple[str, str, str]) -> None:
    assert stammdaten_import._guess_organization(name) == expected


@pytest.mark.parametrize(
    ("text", "expected", "vorlage"),
    [
        ("Stellvertretende Mitglieder", ("member", True), "stellvertretendes Mitglied"),
        ("stellv. Mitglied", ("member", True), "stellvertretendes Mitglied"),
        ("Vertreterin", ("member", True), "stellvertretendes Mitglied"),
        ("1. stellv. Vorsitzender", ("deputy_chair", False), "stellv. Vorsitz"),
        ("Ratsvorsitzende", ("chair", False), "Vorsitz"),
        ("Beratende Mitglieder", ("advisor", False), "beratend"),
        ("Ordentliche Mitglieder", ("member", False), "Mitglied"),
        ("Sachkundige Bürger", ("expert_citizen", False), "sachkundige/r Bürger/in"),
        # Ämter sind keine Stellvertretung im Gremium: Die Verwaltung entscheidet, die Vorlage behält den Text
        ("stellv. Bürgermeister", None, "stellv. Bürgermeister"),
        ("Bürgermeisterin", None, "Bürgermeisterin"),
    ],
)
def test_funktionen_aus_dem_altsystem(text: str, expected: tuple[str, bool] | None, vorlage: str) -> None:
    assert stammdaten_import.resolve_role(text) == expected
    assert stammdaten_import.role_text(text) == vorlage
    # Was die Vorlage schreibt, liest der Import wieder gleich
    if expected is not None:
        assert stammdaten_import.resolve_role(vorlage) == expected


def test_stellvertretung_ohne_vertretene_person_hat_kein_stimmrecht(tenant: SessionTenant, daten: Path) -> None:
    write(
        daten,
        "besetzungen.csv",
        "person;gremium;funktion;stimmrecht;beginn\n"
        "P3;Verwaltungsausschuss;stellvertretendes Mitglied;;01.11.2021\n"
        "P2;Verwaltungsausschuss;stellvertretendes Mitglied;ja;01.11.2021\n",
    )
    run("--tenant", "musterdorf", str(daten))
    votes = dict(
        SessionOrganizationMembership.objects.filter(organization__name="Verwaltungsausschuss").values_list(
            "person__family_name", "has_voting_rights"
        )
    )
    # Ausdrückliche Angabe gilt
    assert votes == {"Beispiel": False, "Mustermann": True}
