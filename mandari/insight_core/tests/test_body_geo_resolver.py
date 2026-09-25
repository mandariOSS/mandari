# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Automatische Geo-Zuordnung neuer Kommunen (Issue #351): Schlüssel aus OParl, OSM-Grenze per AGS
oder Name, Vorschläge bei Mehrdeutigkeit, Körperschaften ohne Gebiet, Nachlauf mit
``fetch_osm_geodata`` und ``import_streets``. Overpass, Nominatim und OParl sind gefälscht –
kein Netzzugriff.
"""

from __future__ import annotations

import re
from io import StringIO
from typing import Any

import pytest
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, RequestFactory
from django.urls import reverse

from insight_core.admin import OParlBodyAdmin
from insight_core.models import Address, OParlBody, OParlBodyGeoSuggestion, OParlSource, Street
from insight_core.services.body_geo_resolver import (
    OsmBoundary,
    is_non_territorial_name,
    keys_from_oparl,
    lies_within,
    match_boundaries,
    parse_body_name,
)
from insight_core.services.geo_coverage import bodies_without_osm_filter, geo_status_for_bodies

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Gefälschte Antworten
# ---------------------------------------------------------------------------


def _relation(
    relation_id: int, name: str, level: int, *, ags: str = "", rgs: str = "", prefix: str = ""
) -> dict[str, Any]:
    tags = {"boundary": "administrative", "type": "boundary", "name": name, "admin_level": str(level)}
    if ags:
        tags["de:amtlicher_gemeindeschluessel"] = ags
    if rgs:
        tags["de:regionalschluessel"] = rgs
    if prefix:
        tags["name:prefix"] = prefix
    return {"type": "relation", "id": relation_id, "tags": tags}


# Wie Overpass am 25.09.2026 für die Verbandsgemeinde Montabaur (Auszug, Nomborn doppelt als Datenfehler)
VG_MONTABAUR = _relation(449385, "Montabaur", 7, rgs="071435004", prefix="Verbandsgemeinde")
STADT_MONTABAUR = _relation(453842, "Montabaur", 8, ags="07143048", rgs="071435004048", prefix="Stadt")
KOELN = _relation(62578, "Köln", 6, ags="05315000", rgs="053150000000", prefix="Kreisfreie Stadt")
AREA_VG_MONTABAUR = [
    _relation(62362, "Westerwaldkreis", 6, ags="07143", rgs="07143"),
    _relation(453829, "Boden", 8, ags="07143005", rgs="071435004005"),
    STADT_MONTABAUR,
    _relation(453847, "Nomborn", 8, ags="07143055", rgs="071435004055"),
    _relation(999001, "Nomborn", 8, ags="07143099", rgs="071435004099"),
    _relation(453851, "Stahlhofen am Wiesensee", 8, ags="07143072", rgs="071435004072"),
    # Nachbar mit gleichem Namen, der das Gebiet nur berührt (anderer Regionalschlüssel)
    _relation(470001, "Boden", 8, ags="07138005", rgs="071385001005"),
]
GLOBAL_MONTABAUR = [
    VG_MONTABAUR,
    STADT_MONTABAUR,
    {"type": "relation", "id": 1, "tags": {"name": "Montabaur", "boundary": "administrative"}},  # ohne DE-Schlüssel
]
STREETS = [
    {
        "type": "way",
        "id": 100,
        "tags": {"name": "Kirchstraße", "highway": "residential"},
        "center": {"lat": 50.4, "lon": 7.8},
    }
]
ADDRESSES = [
    {
        "type": "node",
        "id": 1,
        "lat": 50.41,
        "lon": 7.81,
        "tags": {"addr:street": "Kirchstraße", "addr:housenumber": "1"},
    }
]
BBOX = {449385: (50.33, 50.51, 7.71, 7.95), 453842: (50.40, 50.46, 7.78, 7.86), 453829: (50.44, 50.46, 7.88, 7.91)}


class _Response:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        return self._payload


class FakeOsm:
    """Overpass (``httpx.post``) und Nominatim (``httpx.Client``) aus einer Hand, mit Protokoll."""

    def __init__(self) -> None:
        self.overpass: list[str] = []
        self.nominatim: list[int] = []
        self.status_code = 200
        self.streets = STREETS

    def post(self, url: str, data: dict[str, str], timeout: float, headers: dict[str, str]) -> _Response:
        query = data["data"]
        self.overpass.append(query)
        assert "support@mandari.de" in headers["User-Agent"]
        if self.status_code != 200:
            return _Response({}, self.status_code)
        return _Response({"elements": self._elements(query)})

    def _elements(self, query: str) -> list[dict[str, Any]]:
        if 'de:amtlicher_gemeindeschluessel"="05315000"' in query:
            return [KOELN]
        if "de:amtlicher_gemeindeschluessel" in query:
            return []
        if f"area({3600000000 + 449385})->.gebiet" in query:
            return AREA_VG_MONTABAUR
        if '["name"="Montabaur"]' in query:
            return GLOBAL_MONTABAUR
        if "relation(62578)" in query:
            return [KOELN]
        if "addr:housenumber" in query:
            return ADDRESSES
        if 'way["highway"' in query:
            return self.streets
        return []

    def client(self, *args: Any, **kwargs: Any) -> FakeOsm:
        return self

    def __enter__(self) -> FakeOsm:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get(self, url: str, params: dict[str, Any]) -> _Response:
        relation_id = int(str(params["osm_ids"]).removeprefix("R"))
        self.nominatim.append(relation_id)
        south, north, west, east = BBOX.get(relation_id, (50.0, 50.1, 7.0, 7.1))
        return _Response(
            [
                {
                    "lat": str((south + north) / 2),
                    "lon": str((west + east) / 2),
                    "boundingbox": [str(south), str(north), str(west), str(east)],
                }
            ]
        )

    def queries(self, pattern: str) -> list[str]:
        return [q for q in self.overpass if re.search(pattern, q)]


@pytest.fixture
def fake_osm(monkeypatch: pytest.MonkeyPatch) -> FakeOsm:
    fake = FakeOsm()
    monkeypatch.setattr("insight_core.services.overpass.httpx.post", fake.post)
    monkeypatch.setattr("insight_core.management.commands.fetch_osm_geodata.httpx.Client", fake.client)
    monkeypatch.setattr("insight_core.services.overpass.time.sleep", lambda _seconds: None)
    return fake


def _oparl_body(short: str, name: str, **extra: Any) -> dict[str, Any]:
    """OParl-1.0-Body wie von more! rubin (gremien.info) geliefert – ohne ags/rgs."""
    return {
        "id": f"https://montabaur.gremien.info/oparl/body/{short}",
        "type": "https://schema.oparl.org/1.0/Body",
        "shortName": short,
        "name": name,
        "location": {"type": "https://schema.oparl.org/1.0/Location", "postalCode": "56410"},
        **extra,
    }


@pytest.fixture
def montabaur(db: Any) -> dict[str, OParlBody]:
    """Quelle einer Verbandsgemeinde mit Ortsgemeinden und einem Zweckverband."""
    source = OParlSource.objects.create(
        name="Verbandsgemeinde Montabaur", url="https://montabaur.gremien.info/oparl/system"
    )
    bodies = {}
    for key, short, name in [
        ("vg", "00VG", "Verbandsgemeinde Montabaur"),
        ("stadt", "01Stadt", "Stadt Montabaur"),
        ("boden", "02Bod", "Ortsgemeinde Boden"),
        ("nomborn", "19Nom", "Ortsgemeinde Nomborn"),
        ("stahlhofen", "23Sta", "Ortsgemeinde Stahlhofen a.W."),
        ("zweckverband", "44Gac", "Kindergartenzweckverband Gackenbach-Horbach"),
    ]:
        raw = _oparl_body(short, name)
        bodies[key] = OParlBody.objects.create(
            external_id=raw["id"], source=source, name=name, short_name=short, raw_json=raw, is_listed=False
        )
    return bodies


def _run(*args: str, **options: Any) -> str:
    out = StringIO()
    call_command("resolve_body_geodata", *args, pause=0, stdout=out, **options)
    return out.getvalue()


# ---------------------------------------------------------------------------
# Regeln
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"ags": "05315000", "rgs": "053150000000"}, ("05315000", "053150000000")),  # Köln liefert beides
        ({"rgs": "053150000000"}, ("05315000", "053150000000")),  # Stellen 1–5 + 10–12
        ({"rgs": "071435004048"}, ("07143048", "071435004048")),
        ({"rgs": "071435004"}, ("", "071435004")),  # Verbandsgemeinde: kein AGS
        ({"ags": 5315000}, ("05315000", "")),  # als Zahl verlorene führende Null
        ({"ags": "053"}, ("053", "")),  # Regierungsbezirk
        ({"ags": "keiner", "rgs": "12"}, ("12", "12")),
        ({"ags": "1234567890"}, ("", "")),
        ({}, ("", "")),
        (None, ("", "")),
    ],
)
def test_keys_from_oparl(raw: dict[str, Any] | None, expected: tuple[str, str]) -> None:
    assert keys_from_oparl(raw) == expected


@pytest.mark.parametrize(
    ("name", "base", "category"),
    [
        ("Ortsgemeinde Boden", "Boden", "gemeinde"),
        ("Verbandsgemeinde Enkenbach-Alsenborn", "Enkenbach-Alsenborn", "gemeindeverband"),
        ("Stadt Köln, kreisfreie Stadt", "Köln", "stadt"),
        ("Köln, kreisfreie Stadt", "Köln", "stadt"),
        ("Kreisstadt Homberg (Efze)", "Homberg (Efze)", "stadt"),
        ("Landkreis Ludwigslust-Parchim", "Ludwigslust-Parchim", "kreis"),
        ("Samtgemeinde Sögel", "Sögel", "gemeindeverband"),
        ("Amt Itzstedt", "Itzstedt", "gemeindeverband"),
        ("Ortsbezirk Hayna", "Hayna", "ortsteil"),
        ("Aachen", "Aachen", None),
        ("Stadt", "Stadt", None),
    ],
)
def test_parse_body_name(name: str, base: str, category: str | None) -> None:
    parsed = parse_body_name(name)
    assert (parsed.base, parsed.category) == (base, category)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Kindergartenzweckverband Gackenbach-Horbach", True),
        ("Schulzweckverband IGS Enkenbach-Alsenborn", True),
        ("Zweckverband Friedhof Nahe", True),
        ("Schulverband im Amt Itzstedt", True),
        ("VEGA-net GmbH", True),
        ("Waldgemark Neukirchen, Mehlingen, Baalborn", True),
        ("Wasserwerk Beispiel AöR", True),
        ("Bürgerstiftung Beispielstadt", True),
        ("Stadtwerke Beispielstadt", True),
        ("Verbandsgemeinde Montabaur", False),
        ("Ortsgemeinde Hagenbach", False),
        ("Regionalverband Ruhr", False),
        ("Stadt Bad Kreuznach", False),
        ("Ortsbezirk Hayna", False),
    ],
)
def test_non_territorial_name_rule(name: str, expected: bool) -> None:
    assert is_non_territorial_name(name) is expected


def test_match_boundaries_filters_by_level_category_and_keys() -> None:
    area = [b for b in (OsmBoundary.from_element(el) for el in AREA_VG_MONTABAUR) if b is not None]

    exact, _ = match_boundaries(parse_body_name("Ortsgemeinde Boden"), area, within_area=True, parent_rgs="071435004")
    assert [b.relation_id for b in exact] == [453829]  # Nachbar 470001 liegt laut Schlüssel draußen

    exact, _ = match_boundaries(parse_body_name("Stadt Montabaur"), area, within_area=True, parent_rgs="071435004")
    assert [b.relation_id for b in exact] == [453842]

    exact, approximate = match_boundaries(
        parse_body_name("Ortsgemeinde Stahlhofen a.W."), area, within_area=True, parent_rgs="071435004"
    )
    assert exact == []
    assert [b.relation_id for b in approximate] == [453851]

    exact, _ = match_boundaries(parse_body_name("Ortsgemeinde Stahlhofen"), area, within_area=True)
    assert [b.relation_id for b in exact] == [453851]  # „am Wiesensee“ als amtlicher Zusatz

    # Bundesweit: nur deutsche Grenzen, Ebene aus dem Präfix trennt Verbandsgemeinde und Stadt
    world = [b for b in (OsmBoundary.from_element(el) for el in GLOBAL_MONTABAUR) if b is not None]
    exact, approximate = match_boundaries(parse_body_name("Verbandsgemeinde Montabaur"), world, within_area=False)
    assert [b.relation_id for b in exact] == [449385]
    assert approximate == []


def test_lies_within_by_regional_keys() -> None:
    boundary = OsmBoundary(relation_id=1, name="Boden", ags="07143005", rgs="071435004005")
    assert lies_within(boundary, parent_rgs="071435004")
    assert not lies_within(boundary, parent_rgs="071385001")
    assert lies_within(boundary, parent_ags="07143")
    assert not lies_within(boundary, parent_ags="07138")
    assert lies_within(OsmBoundary(relation_id=2, name="Hayna"), parent_rgs="071435004")  # ohne Schlüssel


# ---------------------------------------------------------------------------
# Befehl resolve_body_geodata
# ---------------------------------------------------------------------------


def test_verbandsgemeinde_is_resolved_end_to_end(
    montabaur: dict[str, OParlBody], fake_osm: FakeOsm, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("insight_core.management.commands.import_streets.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("insight_core.management.commands.resolve_body_geodata.time.sleep", lambda _seconds: None)

    output = _run(source=["montabaur.gremien.info"])
    for body in montabaur.values():
        body.refresh_from_db()
    vg, stadt, boden = montabaur["vg"], montabaur["stadt"], montabaur["boden"]

    # Verbandsgemeinde bundesweit per Name, Gemeinden im Gebiet der Verbandsgemeinde
    assert (vg.osm_relation_id, vg.ags, vg.rgs) == (449385, None, "071435004")
    assert (stadt.osm_relation_id, stadt.ags, stadt.rgs) == (453842, "07143048", "071435004048")
    assert (boden.osm_relation_id, boden.ags) == (453829, "07143005")
    # Nachlauf: Kartenausschnitt, Straßen und Adressen
    for body in (vg, stadt, boden):
        assert body.bbox_north is not None and body.latitude is not None
        assert Street.objects.filter(body=body).count() == 1
        assert Address.objects.filter(body=body).count() == 1
    assert sorted(fake_osm.nominatim) == [449385, 453829, 453842]
    assert len(fake_osm.queries(r"area\(3600449385\)->\.gebiet")) == 1  # Gebietsabfrage nur einmal

    # Mehrdeutig bzw. nur ungefähr: nicht geraten, sondern Vorschläge
    nomborn, stahlhofen = montabaur["nomborn"], montabaur["stahlhofen"]
    assert nomborn.osm_relation_id is None and stahlhofen.osm_relation_id is None
    assert sorted(nomborn.geo_suggestions.values_list("osm_relation_id", flat=True)) == [453847, 999001]
    assert list(stahlhofen.geo_suggestions.values_list("osm_relation_id", flat=True)) == [453851]
    suggestion = nomborn.geo_suggestions.first()
    assert suggestion is not None
    assert suggestion.reason == "2 Namenstreffer in Verbandsgemeinde Montabaur"

    # Zweckverband: ohne Gebiet, Gebiet der Verbandsgemeinde für die Karte, keine OSM-Abfrage
    zweckverband = montabaur["zweckverband"]
    assert zweckverband.is_non_territorial and not zweckverband.territory_set_manually
    assert zweckverband.territory_parent_id == vg.id
    assert zweckverband.osm_relation_id is None
    assert (zweckverband.bbox_north, zweckverband.bbox_west) == (vg.bbox_north, vg.bbox_west)
    assert not Street.objects.filter(body=zweckverband).exists()

    # Lückenliste: nur noch die beiden Vorschläge, der Zweckverband fällt heraus
    gaps = {status.body.name for status in geo_status_for_bodies()}
    assert gaps == {"Ortsgemeinde Nomborn", "Ortsgemeinde Stahlhofen a.W."}
    assert "zugeordnet: 3" in output and "vorschlag: 2" in output and "ohne Gebiet: 1" in output

    # Zweiter Lauf: nichts mehr zu laden, Vorschläge unverändert
    fake_osm.nominatim.clear()
    queries_before = len(fake_osm.overpass)
    _run(source=["montabaur.gremien.info"])
    new_queries = fake_osm.overpass[queries_before:]
    assert fake_osm.nominatim == []
    assert not [q for q in new_queries if "addr:housenumber" in q or 'way["highway"' in q]
    assert OParlBodyGeoSuggestion.objects.count() == 3


def test_ags_from_oparl_resolves_by_key(fake_osm: FakeOsm, monkeypatch: pytest.MonkeyPatch) -> None:
    """Köln liefert ags und rgs: Relation per Schlüssel, ohne Namenssuche."""
    monkeypatch.setattr("insight_core.management.commands.import_streets.time.sleep", lambda _seconds: None)
    source = OParlSource.objects.create(name="Köln", url="https://buergerinfo.stadt-koeln.de/oparl/system")
    raw = {"id": "https://buergerinfo.stadt-koeln.de/oparl/bodies/stadtverwaltung_koeln", "name": "Stadt Köln"}
    raw |= {"ags": "05315000", "rgs": "053150000000"}
    koeln = OParlBody.objects.create(
        external_id=raw["id"], source=source, name="Stadt Köln, kreisfreie Stadt", raw_json=raw
    )

    _run(body=[str(koeln.id)])
    koeln.refresh_from_db()
    assert (koeln.osm_relation_id, koeln.ags, koeln.rgs) == (62578, "05315000", "053150000000")
    assert len(fake_osm.queries("de:amtlicher_gemeindeschluessel")) == 1
    assert not fake_osm.queries(r'\["name"=')
    assert koeln.bbox_north is not None
    assert Street.objects.filter(body=koeln).exists() and Address.objects.filter(body=koeln).exists()


def test_existing_relation_gets_keys_from_osm(fake_osm: FakeOsm) -> None:
    source = OParlSource.objects.create(name="Köln", url="https://ris.example/oparl/system")
    body = OParlBody.objects.create(
        external_id="https://ris.example/oparl/body/1", source=source, name="Stadt Köln", osm_relation_id=62578
    )
    _run(body=[str(body.id)], no_import=True)
    body.refresh_from_db()
    assert (body.ags, body.rgs) == ("05315000", "053150000000")
    assert fake_osm.queries(r"relation\(62578\)")


def test_dry_run_changes_nothing(montabaur: dict[str, OParlBody], fake_osm: FakeOsm) -> None:
    output = _run(source=["montabaur.gremien.info"], dry_run=True)

    assert "Probelauf" in output
    assert "würde zugeordnet" in output
    assert "würde Straßen und Adressen importieren: Ortsgemeinde Boden" in output
    for body in montabaur.values():
        body.refresh_from_db()
        assert body.osm_relation_id is None and not body.is_non_territorial and not body.rgs
    assert not OParlBodyGeoSuggestion.objects.exists()
    assert fake_osm.nominatim == []
    assert not fake_osm.queries("addr:housenumber")


def test_manual_territory_is_kept(montabaur: dict[str, OParlBody], fake_osm: FakeOsm) -> None:
    """Im Admin als Gebietskörperschaft markiert: Die Namensregel ändert nichts mehr."""
    zweckverband = montabaur["zweckverband"]
    zweckverband.territory_set_manually = True
    zweckverband.save()

    _run(body=[str(zweckverband.id)], no_import=True)
    zweckverband.refresh_from_db()
    assert not zweckverband.is_non_territorial
    assert zweckverband.territory_parent_id is None


def test_overpass_outage_aborts_without_damage(montabaur: dict[str, OParlBody], fake_osm: FakeOsm) -> None:
    fake_osm.status_code = 504
    with pytest.raises(CommandError, match="Overpass mehrfach nicht erreichbar"):
        _run(source=["montabaur.gremien.info"])
    # Drei Fehlschläge mit je zwei Endpoints und drei Versuchen, danach keine weiteren Abfragen
    assert len(fake_osm.overpass) == 3 * 2 * 3
    montabaur["boden"].refresh_from_db()
    assert montabaur["boden"].osm_relation_id is None
    # Die Namensregel braucht kein Netz und ist gespeichert
    montabaur["zweckverband"].refresh_from_db()
    assert montabaur["zweckverband"].is_non_territorial


def test_empty_street_imports_abort_follow_up(montabaur: dict[str, OParlBody], fake_osm: FakeOsm) -> None:
    """Bleibt der Straßenimport dreimal in Folge leer, hört der Befehl auf, statt Overpass weiter zu belasten."""
    fake_osm.streets = []
    with pytest.raises(CommandError, match="3-mal in Folge ohne Ergebnis"):
        _run(source=["montabaur.gremien.info"])
    assert len(fake_osm.queries(r'way\["highway"')) == 3
    # Zuordnungen bleiben gespeichert; der nächste Lauf importiert die Gemeinden ohne Straßen erneut
    montabaur["boden"].refresh_from_db()
    assert montabaur["boden"].osm_relation_id == 453829
    fake_osm.streets = STREETS
    _run(source=["montabaur.gremien.info"])
    assert Street.objects.filter(body=montabaur["boden"]).exists()


def test_selection_requires_scope(db: Any) -> None:
    with pytest.raises(CommandError, match="--source, --body oder --all"):
        _run()
    with pytest.raises(CommandError, match="nicht gefunden"):
        _run(source=["gibt-es-nicht.example"])


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_client_(db: Any) -> Client:
    user = get_user_model()(email="admin@example.org", is_staff=True, is_superuser=True, is_active=True)
    user.set_password("geheim-123")
    user.save()
    client = Client()
    client.force_login(user)
    return client


def _suggest(body: OParlBody, relation_id: int, name: str, ags: str = "") -> OParlBodyGeoSuggestion:
    return OParlBodyGeoSuggestion.objects.create(
        body=body, osm_relation_id=relation_id, name=name, admin_level=8, ags=ags, reason="2 Namenstreffer"
    )


def test_admin_apply_suggestion(admin_client_: Client, montabaur: dict[str, OParlBody]) -> None:
    nomborn = montabaur["nomborn"]
    right = _suggest(nomborn, 453847, "Nomborn", ags="07143055")
    _suggest(nomborn, 999001, "Nomborn", ags="07143099")

    changelist = admin_client_.get(reverse("admin:insight_core_oparlbodygeosuggestion_changelist"))
    assert changelist.status_code == 200
    assert (
        reverse("admin:insight_core_oparlbodygeosuggestion_apply_row", args=[right.pk]) in changelist.content.decode()
    )

    body_page = admin_client_.get(reverse("admin:insight_core_oparlbody_change", args=[nomborn.pk]))
    assert body_page.status_code == 200
    assert "Geo-Vorschläge" in body_page.content.decode()

    response = admin_client_.get(reverse("admin:insight_core_oparlbodygeosuggestion_apply_detail", args=[right.pk]))
    assert response.status_code == 302
    nomborn.refresh_from_db()
    assert (nomborn.osm_relation_id, nomborn.ags) == (453847, "07143055")
    assert not nomborn.geo_suggestions.exists()


def test_admin_bulk_apply_needs_one_per_body(admin_client_: Client, montabaur: dict[str, OParlBody]) -> None:
    nomborn = montabaur["nomborn"]
    first, second = _suggest(nomborn, 453847, "Nomborn"), _suggest(nomborn, 999001, "Nomborn")
    boden = _suggest(montabaur["boden"], 453829, "Boden", ags="07143005")

    admin_client_.post(
        reverse("admin:insight_core_oparlbodygeosuggestion_changelist"),
        {"action": "apply_selected", "_selected_action": [first.pk, second.pk, boden.pk]},
    )
    nomborn.refresh_from_db()
    montabaur["boden"].refresh_from_db()
    assert nomborn.osm_relation_id is None and nomborn.geo_suggestions.count() == 2
    assert montabaur["boden"].osm_relation_id == 453829


def test_admin_filters_and_manual_territory(admin_client_: Client, montabaur: dict[str, OParlBody]) -> None:
    zweckverband, vg = montabaur["zweckverband"], montabaur["vg"]
    zweckverband.is_non_territorial = True
    zweckverband.save()
    _suggest(montabaur["nomborn"], 453847, "Nomborn")

    changelist = reverse("admin:insight_core_oparlbody_changelist")
    html = admin_client_.get(changelist, {"geo": "ohne_gebiet"}).content.decode()
    assert "Kindergartenzweckverband" in html and "Ortsgemeinde Boden" not in html
    html = admin_client_.get(changelist, {"geo": "mit_vorschlag"}).content.decode()
    assert "Ortsgemeinde Nomborn" in html and "Ortsgemeinde Boden" not in html
    assert not OParlBody.objects.filter(bodies_without_osm_filter(), pk=zweckverband.pk).exists()

    # Gebietsangabe im Admin geändert → von Hand gesetzt
    class _Form:
        changed_data = ["territory_parent"]

    request = RequestFactory().post("/")
    zweckverband.territory_parent = vg
    OParlBodyAdmin(OParlBody, admin.site).save_model(request, zweckverband, _Form(), change=True)
    zweckverband.refresh_from_db()
    assert zweckverband.territory_set_manually and zweckverband.territory_parent_id == vg.id
