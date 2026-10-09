# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Datenkataloge nach DCAT-AP.de gegen die offiziellen SHACL-Regeln prüfen (Issue #104).

    python scripts/dcat_shacl.py [--ablage VERZEICHNIS]

Baut wie ``scripts/oparl_validator.py`` eine frische Instanz mit den Demo-Daten (``setup_demo_praesentation``) in
einer temporären SQLite-Datenbank, legt dazu eine Kommune aus einem fremden Ratsinformationssystem an und prüft
mit pySHACL jeden Katalog in allen drei Formen (Turtle, RDF/XML, JSON-LD):

- den Gesamtkatalog des Aggregators und den Katalog der fremden Kommune (``/data/dcat/…``),
- den Katalog des Demo-Mandanten (``/session/<slug>/api/dcat/…``).

**Regeln:** DCAT-AP 3.0.0 der SEMIC in der Fassung, die der DCAT-AP.de-Validator lädt (Repository
SEMICeu/DCAT-AP, CC BY 4.0, © Europäische Union; byte-gleich mit der früheren Spiegelung init-dcat-ap-de/DCAT-AP), und die DCAT-AP.de-3.0-Regeln von GovData
(GovDataOfficial/DCAT-AP.de-SHACL-Validation, CC0) – dieselbe Zusammenstellung wie das Profil „DCAT-AP.de 3.0 –
Spezifikation“ des Validators. Sie werden nicht mitgeliefert, sondern in einer festgelegten Fassung (Commit) geladen
und per SHA-256 geprüft. Neue Fassungen übernimmt man bewusst: Commit und Prüfsumme in ``REGELN`` ändern.

**Kontrollierte Vokabulare:** Die Regeln prüfen, ob ein Wert im vorgeschriebenen Vokabular steht
(``skos:inScheme``). Das Skript lädt die Vokabulare, die der Katalog verwendet, von ihren offiziellen Adressen – wie
der Validator auch. Sie ändern sich mit neuen Fassungen und tragen deshalb keine Prüfsumme; so fällt auch auf, wenn
ein verwendeter Wert aus einem Vokabular verschwindet. Ist eine Quelle nicht erreichbar, gilt die Kopie aus der
Ablage (in der CI ein Zwischenspeicher) mit einem Hinweis; ohne Kopie scheitert der Lauf.

**Ergebnis:** Exit-Code 1 bei einem Verstoß (``sh:Violation``) in irgendeiner Ausgabe. Warnungen werden gemeldet,
scheitern den Lauf aber nicht. Nötig ist ``pyshacl`` (Version wie in ``mandari/pyproject.toml``, Extra ``dev``).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mandari"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Offizielle Quelle (SEMIC, EU-Kommission). Die frühere Spiegelung unter init-dcat-ap-de ist seit 09.10.2026
# nicht mehr erreichbar; der Inhalt ist byte-gleich, die Prüfsumme unten sichert das ab.
_DCAT_AP = "https://raw.githubusercontent.com/SEMICeu/DCAT-AP/fff059bd9c14c1adff5abfd6f32ff437eaec3390"
_GOVDATA = (
    "https://raw.githubusercontent.com/GovDataOfficial/DCAT-AP.de-SHACL-Validation/"
    "cb9b73a6ff9e4bdf8605ba84d5336da7086ea844/validator/resources/v3.0/shapes"
)

#: SHACL-Regeln: Adresse in festgelegter Fassung -> SHA-256
REGELN: dict[str, str] = {
    f"{_DCAT_AP}/releases/3.0.0/shacl/dcat-ap-SHACL.ttl": (
        "92f76609d78d257123e75bc6b7155df5cc0a63f14c29fbc12b0ac95c56af2059"
    ),
    f"{_GOVDATA}/dcat-ap-SHACL-DE.ttl": "f37caba134234963a0555b05ae84c26163b5493a366b154a2b5688320d67f7a0",
    f"{_GOVDATA}/dcat-ap-de-deprecated.ttl": "236c038de933af7b3fe7fff0e58d483b44f42ec2b74ede5d56775ac5f324d61c",
    f"{_GOVDATA}/dcat-ap-spec-german-additions.ttl": (
        "86a51b5eef5df4e6ea80e2cf33e4375ee61af2bb955fb80808975d90088765f6"
    ),
    f"{_GOVDATA}/dcat-ap-de-controlledvocabularies.ttl": (
        "5a83bf7d17f60598f8959062c74c536b7659972eee370e425817bd063efe49e8"
    ),
}

_EU = "http://publications.europa.eu/resource/authority"
#: Kontrollierte Vokabulare, die der Katalog verwendet (wie in dcat-ap-de-imports.ttl von GovData): Name -> Adresse
VOKABULARE: dict[str, str] = {
    "sprache": f"{_EU}/language",
    "themen": f"{_EU}/data-theme",
    "frequenz": f"{_EU}/frequency",
    "dateiformate": f"{_EU}/file-type",
    "zugang": f"{_EU}/access-right",
    "verfuegbarkeit": f"{_EU}/planned-availability",
    "lizenzen": "https://www.dcat-ap.de/def/licenses/20210721.rdf",
    "geoebenen": "https://www.dcat-ap.de/def/politicalGeocoding/Level/1_0.rdf",
}

#: Ausgabeformen: Endung -> Name in rdflib
FORMEN = {"ttl": "turtle", "rdf": "xml", "jsonld": "json-ld"}


def laden(adresse: str, *, versuche: int = 3) -> bytes:
    """Adresse abrufen (RDF/XML bevorzugt), mit Wiederholung bei vorübergehenden Fehlern."""
    letzter: Exception | None = None
    for versuch in range(versuche):
        anfrage = urllib.request.Request(  # noqa: S310 – feste https/http-Adressen aus diesem Skript
            adresse,
            headers={"Accept": "application/rdf+xml, text/turtle;q=0.9", "User-Agent": "mandari-dcat-shacl"},
        )
        try:
            with urllib.request.urlopen(anfrage, timeout=60) as antwort:  # noqa: S310
                return bytes(antwort.read())
        except (urllib.error.URLError, TimeoutError) as fehler:
            letzter = fehler
            time.sleep(2 * (versuch + 1))
    raise OSError(f"{adresse} nicht erreichbar: {letzter}")


def regeln(ablage: Path) -> Any:
    """Die SHACL-Regeln in der festgelegten Fassung; aus der Ablage, wenn die Prüfsumme dort stimmt."""
    from rdflib import Graph

    g = Graph()
    (ablage / "regeln").mkdir(parents=True, exist_ok=True)
    for adresse, pruefsumme in REGELN.items():
        datei = ablage / "regeln" / f"{pruefsumme}.ttl"
        inhalt = datei.read_bytes() if datei.is_file() else b""
        if hashlib.sha256(inhalt).hexdigest() != pruefsumme:
            inhalt = laden(adresse)
            if hashlib.sha256(inhalt).hexdigest() != pruefsumme:
                raise SystemExit(f"Prüfsumme stimmt nicht: {adresse}")
            datei.write_bytes(inhalt)
        g.parse(data=inhalt, format="turtle")
    return g


def vokabulare(ablage: Path) -> Any:
    """Die kontrollierten Vokabulare von ihren offiziellen Adressen, ersatzweise aus der Ablage."""
    from rdflib import Graph

    g = Graph()
    (ablage / "vokabulare").mkdir(parents=True, exist_ok=True)
    for name, adresse in VOKABULARE.items():
        datei = ablage / "vokabulare" / f"{name}.rdf"
        try:
            inhalt = laden(adresse)
            Graph().parse(data=inhalt, format="xml")  # nur Gültiges in die Ablage
            datei.write_bytes(inhalt)
        except Exception as fehler:  # noqa: BLE001 – jede Störung der Quelle: Kopie aus der Ablage
            if not datei.is_file():
                raise SystemExit(f"Vokabular {name} nicht erreichbar und nicht in der Ablage: {fehler}") from None
            print(f"Hinweis: Vokabular {name} nicht erreichbar ({fehler}); Kopie aus der Ablage.")
            inhalt = datei.read_bytes()
        g.parse(data=inhalt, format="xml")
    return g


@dataclass
class Ergebnis:
    verstoesse: list[str]
    warnungen: int
    hinweise: int


def pruefen(daten: bytes, form: str, shapes: Any, vokabular: Any) -> Ergebnis:
    """Eine Ausgabe gegen die Regeln prüfen; die Vokabulare gehören zu den geprüften Daten (wie im Validator)."""
    from pyshacl import validate
    from rdflib import Graph, Namespace

    sh = Namespace("http://www.w3.org/ns/shacl#")
    g = Graph().parse(data=daten, format=form)
    if len(g) < 50:
        return Ergebnis([f"Nur {len(g)} Tripel – die Ausgabe ist kein vollständiger Katalog."], 0, 0)
    _, bericht, _ = validate(g + vokabular, shacl_graph=shapes, inference="none", allow_warnings=True, advanced=True)
    verstoesse: list[str] = []
    warnungen = hinweise = 0
    for ergebnis in bericht.subjects(sh.resultSeverity, None):
        schwere = bericht.value(ergebnis, sh.resultSeverity)
        if schwere == sh.Warning:
            warnungen += 1
        elif schwere == sh.Info:
            hinweise += 1
        else:
            verstoesse.append(
                f"{bericht.value(ergebnis, sh.focusNode)} {bericht.value(ergebnis, sh.resultPath)}: "
                f"{bericht.value(ergebnis, sh.resultMessage)}"
            )
    return Ergebnis(verstoesse, warnungen, hinweise)


def fremde_kommune() -> str:
    """Eine gelistete Kommune aus einem fremden Ratsinformationssystem (der Demo-Mandant gibt seinen Katalog selbst heraus)."""
    from insight_core.models import OParlBody, OParlMeeting, OParlOrganization, OParlPaper, OParlPerson, OParlSource

    ris = "https://ris.beispielhausen.example/oparl"
    quelle = OParlSource.objects.create(name="RIS Beispielhausen", url=f"{ris}/system")
    body = OParlBody.objects.create(
        external_id=f"{ris}/body/1",
        source=quelle,
        name="Stadt Beispielhausen",
        slug="beispielhausen",
        website="https://www.beispielhausen.example",
        license="https://www.govdata.de/dl-de/by-2-0",
        ags="05515000",
    )
    OParlOrganization.objects.create(
        external_id=f"{ris}/organization/1", body=body, name="Rat", start_date=date(2020, 11, 1)
    )
    OParlPerson.objects.create(
        external_id=f"{ris}/person/1", body=body, name="Ratsmitglied", family_name="Ratsmitglied"
    )
    OParlMeeting.objects.create(
        external_id=f"{ris}/meeting/1", body=body, name="Ratssitzung", start=datetime(2026, 3, 4, 17, tzinfo=UTC)
    )
    OParlPaper.objects.create(external_id=f"{ris}/paper/1", body=body, name="Radweg", date=date(2026, 2, 1))
    return str(body.pk)


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument(
        "--ablage",
        type=Path,
        default=Path(tempfile.gettempdir()) / "mandari-dcat-ablage",
        help="Verzeichnis für Regeln und Vokabulare (in der CI ein Zwischenspeicher)",
    )
    args = parser.parse_args()

    import oparl_validator as basis

    tmp = Path(tempfile.mkdtemp(prefix="mandari_dcat_shacl_"))
    env = basis.umgebung(tmp, basis.freier_port())
    env.update(
        {
            "DCAT_ENABLED": "true",
            "DCAT_PUBLISHER_NAME": "Betreiber der Testinstanz",
            "DCAT_CONTACT_EMAIL": "daten@betreiber.example",
            "DCAT_CACHE_SECONDS": "0",
        }
    )
    os.environ.update(env)
    slug = basis.daten_anlegen(tmp)
    body_id = fremde_kommune()

    from django.test import Client

    shapes = regeln(args.ablage)
    vokabular = vokabulare(args.ablage)
    ausgaben = {
        "Gesamtkatalog des Aggregators": "/data/dcat/catalog",
        "Katalog einer fremden Kommune": f"/data/dcat/body/{body_id}/catalog",
        "Katalog des Demo-Mandanten (Session)": f"/session/{slug}/api/dcat/catalog",
    }

    client = Client()
    verstoesse = 0
    for name, pfad in ausgaben.items():
        for endung, form in FORMEN.items():
            antwort = client.get(f"{pfad}.{endung}")
            if antwort.status_code != 200:
                print(f"== {name} ({endung}): HTTP {antwort.status_code}")
                verstoesse += 1
                continue
            ergebnis = pruefen(antwort.content, form, shapes, vokabular)
            print(
                f"== {name} ({endung}): {len(ergebnis.verstoesse)} Verstöße, {ergebnis.warnungen} Warnungen, "
                f"{ergebnis.hinweise} Hinweise"
            )
            for verstoss in ergebnis.verstoesse[:30]:
                print(f"   - {verstoss}")
            verstoesse += len(ergebnis.verstoesse)

    if verstoesse:
        print(f"\nDCAT-AP.de-Prüfung fehlgeschlagen: {verstoesse} Verstöße.")
        return 1
    print("\nDCAT-AP.de-Prüfung bestanden.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
