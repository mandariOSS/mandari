# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Bezugsantrag und Bezugssitzung eines Dokuments (Issue #586).

Dokumente hatten Felder für einen Bezugsantrag („Änderungsantrag zu …“) und eine Bezugssitzung, die
Oberfläche konnte sie aber nicht setzen; ein alter Speicherweg dafür wurde nie erreicht. Jetzt: Suche
in Dokumenten der Organisation und Vorlagen bzw. Sitzungen des RIS (nur eigene Kommunen), Anzeige am
Dokument und Rückverweis am Bezugsantrag. Dazu #616: Änderungsanträge, deren Bezugsantrag nicht in der
Liste steht, erscheinen als eigene Einträge.
"""

from __future__ import annotations

import importlib
from datetime import datetime
from typing import Any

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.urls import reverse
from django.utils import timezone

from apps.work.faction.models import FactionMeeting
from apps.work.motions.models import Motion
from insight_core.models import OParlBody, OParlMeeting, OParlPaper, OParlSource

RECHTE = ["motions.view", "motions.view_drafts", "motions.create", "motions.edit", "motions.comment"]


@pytest.fixture
def ris(org: Any) -> dict[str, Any]:
    source = OParlSource.objects.create(name="Test-RIS", url="https://ris.example.org/system")
    body = OParlBody.objects.create(external_id="https://ris.example.org/body/1", source=source, name="Stadt Test")
    fremd = OParlBody.objects.create(external_id="https://ris.example.org/body/2", source=source, name="Stadt Fremd")
    org.body = body
    org.save(update_fields=["body"])
    start = timezone.make_aware(datetime(2026, 3, 12, 17, 0))
    return {
        "vorlage": OParlPaper.objects.create(
            external_id="https://ris.example.org/paper/1", body=body, name="Spielplatz am Markt", reference="V/2026/17"
        ),
        "fremde_vorlage": OParlPaper.objects.create(
            external_id="https://ris.example.org/paper/2", body=fremd, name="Spielplatz im Park", reference="F/2026/1"
        ),
        "sitzung": OParlMeeting.objects.create(
            external_id="https://ris.example.org/meeting/1", body=body, name="Rat der Stadt", start=start
        ),
        "fremde_sitzung": OParlMeeting.objects.create(
            external_id="https://ris.example.org/meeting/2", body=fremd, name="Rat Fremdstadt", start=start
        ),
    }


@pytest.fixture
def autorin(org: Any, make_member: Any) -> Any:
    return make_member(org, RECHTE, email="autorin@example.org")


@pytest.fixture
def kollege(org: Any, make_member: Any) -> Any:
    return make_member(org, RECHTE, email="kollege@example.org")


def _dokument(org: Any, autor: Any, titel: str, visibility: str = "organization") -> Motion:
    motion: Any = Motion(organization=org, author=autor, title=titel, visibility=visibility)
    motion.set_content_encrypted("<p>Text</p>")
    motion.save()
    return motion  # type: ignore[no-any-return]


@pytest.fixture
def dokumente(org: Any, autorin: Any, kollege: Any) -> dict[str, Motion]:
    return {
        "haupt": _dokument(org, autorin, "Haushaltsantrag Radwege"),
        "aenderung": _dokument(org, autorin, "Änderung zum Radwegeantrag"),
        "privat_kollege": _dokument(org, kollege, "Privater Radwegeentwurf", visibility="private"),
    }


def _meta(client: Any, org: Any, motion: Motion, **data: str) -> Any:
    url = reverse("work:document_meta", kwargs={"org_slug": org.slug, "motion_id": motion.id})
    return client.post(url, data, HTTP_X_REQUESTED_WITH="XMLHttpRequest")


def _editor(client: Any, org: Any, motion: Motion) -> str:
    url = reverse("work:document_editor", kwargs={"org_slug": org.slug, "motion_id": motion.id})
    response = client.get(url)
    assert response.status_code == 200
    return str(response.content.decode())


def _suche(client: Any, org: Any, motion: Motion, art: str, q: str) -> Any:
    url = reverse("work:document_reference_search", kwargs={"org_slug": org.slug, "motion_id": motion.id})
    return client.get(url, {"art": art, "q": q})


# ---------------------------------------------------------------------------
# Setzen und anzeigen
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_bezugsantrag_dokument_setzen_anzeigen_und_rueckverweis(
    org: Any, autorin: Any, dokumente: dict[str, Motion], client_for: Any
) -> None:
    client = client_for(autorin.user)
    haupt, aenderung = dokumente["haupt"], dokumente["aenderung"]

    antwort = _meta(client, org, aenderung, action="set_parent", parent_motion=str(haupt.id))

    assert antwort.status_code == 200, antwort.content
    aenderung.refresh_from_db()
    assert aenderung.parent_motion_id == haupt.id and aenderung.parent_paper_id is None
    html = _editor(client, org, aenderung)
    assert 'data-testid="kopf-bezug"' in html and "Haushaltsantrag Radwege" in html
    rueck = _editor(client, org, haupt)
    assert "Änderungsanträge zu diesem Dokument" in rueck and "Änderung zum Radwegeantrag" in rueck


@pytest.mark.django_db
def test_bezugsantrag_ris_vorlage_nur_aus_eigener_kommune(
    org: Any, autorin: Any, dokumente: dict[str, Motion], ris: dict[str, Any], client_for: Any
) -> None:
    client = client_for(autorin.user)
    motion = dokumente["aenderung"]

    fremd = _meta(client, org, motion, action="set_parent", parent_paper=str(ris["fremde_vorlage"].id))
    assert fremd.status_code == 400
    motion.refresh_from_db()
    assert motion.parent_paper_id is None

    eigen = _meta(client, org, motion, action="set_parent", parent_paper=str(ris["vorlage"].id))
    assert eigen.status_code == 200
    motion.refresh_from_db()
    assert motion.parent_paper_id == ris["vorlage"].id and motion.parent_motion_id is None
    assert "V/2026/17 – Spielplatz am Markt" in _editor(client, org, motion)


@pytest.mark.django_db
def test_dokument_ersetzt_vorlage_und_beides_zugleich_geht_nicht(
    org: Any, autorin: Any, dokumente: dict[str, Motion], ris: dict[str, Any], client_for: Any
) -> None:
    client = client_for(autorin.user)
    motion = dokumente["aenderung"]
    _meta(client, org, motion, action="set_parent", parent_paper=str(ris["vorlage"].id))

    beides = _meta(
        client,
        org,
        motion,
        action="set_parent",
        parent_paper=str(ris["vorlage"].id),
        parent_motion=str(dokumente["haupt"].id),
    )
    assert beides.status_code == 400

    _meta(client, org, motion, action="set_parent", parent_motion=str(dokumente["haupt"].id))
    motion.refresh_from_db()
    assert motion.parent_motion_id == dokumente["haupt"].id and motion.parent_paper_id is None


@pytest.mark.django_db
def test_bezugssitzung_nur_aus_eigener_kommune_und_entfernbar(
    org: Any, autorin: Any, dokumente: dict[str, Motion], ris: dict[str, Any], client_for: Any
) -> None:
    client = client_for(autorin.user)
    motion = dokumente["haupt"]

    assert (
        _meta(client, org, motion, action="set_reference_meeting", meeting=str(ris["fremde_sitzung"].id)).status_code
        == 400
    )
    assert _meta(client, org, motion, action="set_reference_meeting", meeting=str(ris["sitzung"].id)).status_code == 200
    motion.refresh_from_db()
    assert motion.related_meeting_id == ris["sitzung"].id
    assert "Rat der Stadt, 12.03.2026" in _editor(client, org, motion)

    assert _meta(client, org, motion, action="set_reference_meeting").status_code == 200
    motion.refresh_from_db()
    assert motion.related_meeting_id is None


@pytest.mark.django_db
def test_kein_bezug_auf_sich_selbst_oder_eigene_aenderungsantraege(
    org: Any, autorin: Any, dokumente: dict[str, Motion], client_for: Any
) -> None:
    client = client_for(autorin.user)
    haupt, aenderung = dokumente["haupt"], dokumente["aenderung"]
    Motion.objects.filter(pk=aenderung.pk).update(parent_motion=haupt)
    unteraenderung = _dokument(org, autorin, "Unteränderung")
    Motion.objects.filter(pk=unteraenderung.pk).update(parent_motion=aenderung)

    assert _meta(client, org, haupt, action="set_parent", parent_motion=str(haupt.id)).status_code == 400
    assert _meta(client, org, haupt, action="set_parent", parent_motion=str(unteraenderung.id)).status_code == 400
    haupt.refresh_from_db()
    assert haupt.parent_motion_id is None


@pytest.mark.django_db
def test_nicht_sichtbares_bezugsdokument_bleibt_verborgen(
    org: Any, autorin: Any, kollege: Any, dokumente: dict[str, Motion], client_for: Any
) -> None:
    # Nur wählbar, was man sehen darf …
    antwort = _meta(
        client_for(autorin.user),
        org,
        dokumente["aenderung"],
        action="set_parent",
        parent_motion=str(dokumente["privat_kollege"].id),
    )
    assert antwort.status_code == 400
    # … und ein von anderen gesetzter Bezug verrät den Titel nicht
    Motion.objects.filter(pk=dokumente["aenderung"].pk).update(parent_motion=dokumente["privat_kollege"])
    html = _editor(client_for(autorin.user), org, dokumente["aenderung"])
    assert "Dokument ohne Freigabe" in html
    assert "Privater Radwegeentwurf" not in html


# ---------------------------------------------------------------------------
# Suche
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_suche_liefert_dokumente_und_vorlagen_der_eigenen_kommune(
    org: Any, autorin: Any, dokumente: dict[str, Motion], ris: dict[str, Any], client_for: Any
) -> None:
    client = client_for(autorin.user)

    html = _suche(client, org, dokumente["aenderung"], "antrag", "Radweg").content.decode()
    assert "Haushaltsantrag Radwege" in html
    assert "Änderung zum Radwegeantrag" not in html  # nicht sich selbst
    assert "Privater Radwegeentwurf" not in html  # nicht sichtbar

    html = _suche(client, org, dokumente["aenderung"], "antrag", "Spielplatz").content.decode()
    assert "V/2026/17 – Spielplatz am Markt" in html
    assert "Spielplatz im Park" not in html  # andere Kommune
    assert f'name="parent_paper" value="{ris["vorlage"].id}"' in html


@pytest.mark.django_db
def test_suche_nach_sitzung_per_name_und_datum(
    org: Any, autorin: Any, dokumente: dict[str, Motion], ris: dict[str, Any], client_for: Any
) -> None:
    client = client_for(autorin.user)
    for anfrage in ("Rat", "12.03.2026"):
        html = _suche(client, org, dokumente["haupt"], "sitzung", anfrage).content.decode()
        assert "Rat der Stadt, 12.03.2026" in html, anfrage
        assert "Rat Fremdstadt" not in html, anfrage
    assert "Rat der Stadt" not in _suche(client, org, dokumente["haupt"], "sitzung", "R").content.decode()


@pytest.mark.django_db
def test_suche_nur_mit_bearbeitungsrecht(
    org: Any, dokumente: dict[str, Motion], make_member: Any, client_for: Any
) -> None:
    leser = make_member(org, ["motions.view", "motions.edit"], email="leser@example.org")
    # Organisationsweites Dokument einer anderen Person: lesbar, aber nicht bearbeitbar
    assert _suche(client_for(leser.user), org, dokumente["haupt"], "antrag", "Radweg").status_code == 403


# ---------------------------------------------------------------------------
# Toter Speicherweg, Liste (#616), Migration
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_alter_formular_speicherweg_ist_entfernt(
    org: Any, autorin: Any, dokumente: dict[str, Motion], client_for: Any
) -> None:
    motion = dokumente["aenderung"]
    url = reverse("work:document_editor", kwargs={"org_slug": org.slug, "motion_id": motion.id})
    antwort = client_for(autorin.user).post(
        url,
        {"action": "form", "title": "Per Formular", "parent_motion": str(dokumente["haupt"].id)},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert antwort.status_code == 400
    motion.refresh_from_db()
    assert motion.title == "Änderung zum Radwegeantrag" and motion.parent_motion_id is None


@pytest.mark.django_db
def test_liste_zeigt_aenderungsantrag_ohne_sichtbaren_bezug(
    org: Any, autorin: Any, kollege: Any, dokumente: dict[str, Motion], client_for: Any
) -> None:
    Motion.objects.filter(pk=dokumente["aenderung"].pk).update(parent_motion=dokumente["privat_kollege"])
    url = reverse("work:documents", kwargs={"org_slug": org.slug})

    html = client_for(autorin.user).get(url).content.decode()
    assert "Änderung zum Radwegeantrag" in html
    assert "Privater Radwegeentwurf" not in html


@pytest.mark.django_db
def test_suche_in_der_liste_findet_aenderungsantrag(
    org: Any, autorin: Any, dokumente: dict[str, Motion], client_for: Any
) -> None:
    Motion.objects.filter(pk=dokumente["aenderung"].pk).update(parent_motion=dokumente["haupt"])
    url = reverse("work:documents", kwargs={"org_slug": org.slug})

    html = client_for(autorin.user).get(url, {"q": "Änderung zum"}).content.decode()
    assert "Änderung zum Radwegeantrag" in html


MIGRATION = importlib.import_module("apps.work.migrations.0063_bezug_dokumente")
NACHHER = ("work", MIGRATION.__name__.rsplit(".", 1)[1])
VORHER = next(dep for dep in MIGRATION.Migration.dependencies if dep[0] == "work")


@pytest.mark.django_db(transaction=True)
def test_migration_leert_zielsitzung_und_behaelt_die_spalte(org: Any, autorin: Any) -> None:
    motion = _dokument(org, autorin, "Mit Zielsitzung")
    sitzung = FactionMeeting.objects.create(organization=org, title="Klausur", start=timezone.now())

    executor = MigrationExecutor(connection)
    executor.migrate([VORHER])
    try:
        alt = executor.loader.project_state([VORHER]).apps
        alt.get_model("work", "Motion").objects.filter(pk=motion.pk).update(target_meeting_id=sitzung.pk)

        executor = MigrationExecutor(connection)
        executor.migrate([NACHHER])

        spalten = {c.name for c in connection.introspection.get_table_description(connection.cursor(), "work_motion")}
        assert "target_meeting_id" in spalten, "Die Spalte bleibt für ältere Versionen"
        with connection.cursor() as cursor:
            cursor.execute("SELECT target_meeting_id FROM work_motion WHERE id = %s", [motion.pk.hex])
            row = cursor.fetchone()
        assert row is not None and row[0] is None
        FactionMeeting.objects.filter(pk=sitzung.pk).delete()  # kein verwaister Verweis mehr

        # Wiederholbar
        MIGRATION.zielsitzung_leeren(executor.loader.project_state([VORHER]).apps, None)
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
