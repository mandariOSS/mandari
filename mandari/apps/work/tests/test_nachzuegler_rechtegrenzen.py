# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Drei Rechtegrenzen in Work, die auf frühere Korrekturen aufbauen.

- Unterpunkt in der Vorbereitung: Wer „nichtöffentlich“ wählt, bekommt auch unter einem öffentlichen TOP einen
  nichtöffentlichen Unterpunkt (wie in der laufenden Sitzung).
- Aufgabenimport: Neue Labels im gemeinsamen Katalog legt nur an, wer Labels verwalten darf, und nur begrenzt viele.
- Dokumentordner: Durchläufe über die Ordnerstruktur enden auch, wenn der Bestand einen Zyklus enthält.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.work.faction.models import FactionAgendaItem, FactionMeeting
from apps.work.motions.models import DocumentFolder, Motion
from apps.work.tasks import import_service
from apps.work.tasks.models import TaskLabel

pytestmark = pytest.mark.django_db


# --- Unterpunkt in der Vorbereitung -------------------------------------------------------------------------


@pytest.fixture
def leitung(org: Any, make_member: Any) -> Any:
    mitglied = make_member(
        org, ["faction.view_public", "faction.view_non_public", "faction.manage"], email="leitung@example.org"
    )
    mitglied.is_sworn_in = True
    mitglied.save(update_fields=["is_sworn_in"])
    return mitglied


@pytest.fixture
def sitzung(org: Any, leitung: Any) -> FactionMeeting:
    return FactionMeeting.objects.create(
        organization=org,
        title="Fraktionssitzung",
        start=timezone.now() + timedelta(days=3),
        status="planned",
        created_by=leitung,
    )


def _unterpunkt(client_for: Any, mitglied: Any, org: Any, sitzung: FactionMeeting, eltern: Any, teil: str) -> Any:
    url = reverse("work:faction_action", kwargs={"org_slug": org.slug, "meeting_id": sitzung.pk})
    daten = {"action": "add_item", "title": "Grundstücksfrage", "visibility": teil, "parent_id": str(eltern.pk)}
    return client_for(mitglied.user).post(url, daten)


def test_nichtoeffentlicher_unterpunkt_unter_oeffentlichem_top_bleibt_nichtoeffentlich(
    org: Any, client_for: Any, leitung: Any, sitzung: FactionMeeting
) -> None:
    eltern = FactionAgendaItem.objects.create(meeting=sitzung, number="1", title="Bauen", visibility="public", order=1)

    _unterpunkt(client_for, leitung, org, sitzung, eltern, "internal")
    _unterpunkt(client_for, leitung, org, sitzung, eltern, "public")

    teile = sorted(FactionAgendaItem.objects.filter(parent=eltern).values_list("visibility", flat=True))
    assert teile == ["internal", "public"]


def test_unterpunkt_unter_nichtoeffentlichem_top_ist_immer_nichtoeffentlich(
    org: Any, client_for: Any, make_member: Any, leitung: Any, sitzung: FactionMeeting
) -> None:
    eltern = FactionAgendaItem.objects.create(
        meeting=sitzung, number="N1", title="Personal", visibility="internal", order=1
    )

    _unterpunkt(client_for, leitung, org, sitzung, eltern, "public")

    assert list(FactionAgendaItem.objects.filter(parent=eltern).values_list("visibility", flat=True)) == ["internal"]
    # Ohne Vereidigung kein Unterpunkt im nichtöffentlichen Teil
    andere = make_member(org, ["faction.view_public", "faction.manage"], email="andere@example.org")
    assert _unterpunkt(client_for, andere, org, sitzung, eltern, "public").status_code == 403
    assert FactionAgendaItem.objects.filter(parent=eltern).count() == 1


# --- Labels beim Aufgabenimport -----------------------------------------------------------------------------


def test_import_legt_labels_nur_mit_verwaltungsrecht_an(org: Any, make_member: Any) -> None:
    TaskLabel.objects.create(organization=org, name="Haushalt", color="blue")
    ohne = make_member(org, ["tasks.view", "tasks.create"], email="ohne@example.org")
    mit = make_member(org, ["tasks.view", "tasks.create", "tasks.manage"], email="mit@example.org")

    aufloesung = import_service._LabelResolver(org, ohne)
    gefunden = aufloesung.resolve(["haushalt", "Neu erfunden"])

    assert [label.name for label in gefunden] == ["Haushalt"]
    assert not TaskLabel.objects.filter(organization=org, name="Neu erfunden").exists()
    assert "Neu erfunden" in aufloesung.warning() and "verwalten" in aufloesung.warning()

    aufloesung = import_service._LabelResolver(org, mit)
    assert [label.name for label in aufloesung.resolve(["Neu erfunden"])] == ["Neu erfunden"]
    assert aufloesung.warning() == ""


def test_import_legt_hoechstens_begrenzt_viele_labels_an(org: Any, make_member: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr(import_service, "MAX_NEW_LABELS_PER_IMPORT", 2)
    mit = make_member(org, ["tasks.view", "tasks.create", "tasks.manage"], email="mit@example.org")

    aufloesung = import_service._LabelResolver(org, mit)
    aufloesung.resolve([f"Label {nummer}" for nummer in range(5)])

    assert TaskLabel.objects.filter(organization=org).count() == 2
    assert "3 unbekannte Labels" in aufloesung.warning()


def test_import_erhaelt_bestehende_labels_wenn_neue_nicht_angelegt_werden(org: Any, make_member: Any) -> None:
    from apps.work.tasks.models import Task

    ohne = make_member(org, ["tasks.view", "tasks.create", "tasks.edit"], email="ohne@example.org")
    vorhanden = TaskLabel.objects.create(organization=org, name="Haushalt", color="blue")
    aufgabe = Task.objects.create(organization=org, title="Rede", created_by=ohne)
    aufgabe.labels.add(vorhanden)
    zeile = {
        "_line": 2,
        "id": str(aufgabe.pk),
        "title": "Rede",
        "description": "Entwurf bis Freitag",
        "labels": "Neu erfunden",
    }

    bericht = import_service.classify_rows(org, ohne, [zeile])
    # Der Hinweis steht schon in der Vorschau, vorn im Bericht
    assert "Neu erfunden" in bericht.warnings[0]

    import_service.apply_import(org, ohne, bericht)

    aufgabe.refresh_from_db()
    # Die Zeile wurde übernommen, die bestehenden Labels sind geblieben
    assert aufgabe.description == "Entwurf bis Freitag"
    assert list(aufgabe.labels.values_list("name", flat=True)) == ["Haushalt"]
    # Der Hinweis steht nach dem Import weiter vorn und nur einmal
    assert "Neu erfunden" in bericht.warnings[0]
    assert sum("unbekannte Labels" in warnung for warnung in bericht.warnings) == 1


# --- Ordner: Durchläufe enden auch im Zyklus ------------------------------------------------------------------


def test_ordnerdurchlaeufe_enden_in_einem_zyklus(org: Any, make_member: Any) -> None:
    autorin = make_member(org, ["motions.view", "motions.create", "motions.edit"], email="autorin@example.org")
    a = DocumentFolder.objects.create(organization=org, name="A", created_by=autorin)
    b = DocumentFolder.objects.create(organization=org, name="B", parent=a, created_by=autorin)
    c = DocumentFolder.objects.create(organization=org, name="C", parent=b, created_by=autorin)
    dokument = Motion.objects.create(organization=org, author=autorin, title="Antrag", folder=c)
    # Zyklus wie nach zwei gleichzeitigen Verschiebungen: A unter C
    DocumentFolder.objects.filter(pk=a.pk).update(parent=c)
    a, b, c = (DocumentFolder.objects.get(pk=ordner.pk) for ordner in (a, b, c))

    assert {ordner.pk for ordner in a.get_descendants()} == {b.pk, c.pk}
    assert {ordner.pk for ordner in c.get_ancestors()} == {a.pk, b.pk}
    assert a.get_depth() == 3 and a.get_subtree_height() == 3
    gast = make_member(org, [], email="gast@example.org")
    dokument = Motion.objects.get(pk=dokument.pk)
    assert dokument.get_guest_share_level(gast) is None
    assert dokument.guest_share_since(gast) is None  # type: ignore[no-untyped-call]
    # Die Prüfung beim Speichern endet ebenfalls
    neu = DocumentFolder(organization=org, name="D", parent=a, created_by=autorin)
    neu.clean()  # type: ignore[no-untyped-call]


def test_verschieben_prueft_gastfreigabe_unter_der_sperre_am_gespeicherten_ordner(org: Any, make_member: Any) -> None:
    """Der Zielordner wandert nach der frühen Prüfung unter einen für Gäste freigegebenen Ordner."""
    from django.core.exceptions import ValidationError

    from apps.work.motions.models import GUEST_FOLDER_SUBTREE_DENIED, FolderGuestShare

    autorin = make_member(org, ["motions.view", "motions.create", "motions.edit"], email="autorin@example.org")
    # Darf Ordner verwalten, aber keine Dokumente freigeben
    verschiebt = make_member(org, ["motions.view", "motions.create", "motions.edit"], email="verschiebt@example.org")
    gast = make_member(org, [], email="gast@example.org")
    freigegeben = DocumentFolder.objects.create(organization=org, name="Für Gäste", created_by=autorin)
    FolderGuestShare.objects.create(folder=freigegeben, user=gast.user, level="view", created_by=autorin.user)
    ziel = DocumentFolder.objects.create(organization=org, name="Ziel", created_by=autorin)
    ordner = DocumentFolder.objects.create(organization=org, name="Entwürfe", created_by=verschiebt)
    Motion.objects.create(organization=org, author=autorin, title="Antrag", folder=ordner, visibility="organization")

    # Frühe Prüfung im View: Das Ziel hängt noch nicht unter dem freigegebenen Ordner
    assert ordner.guest_move_denied(ziel, verschiebt) == ""
    ordner.parent = ziel
    # Eine andere Anfrage hängt das Ziel inzwischen unter den freigegebenen Ordner
    DocumentFolder.objects.filter(pk=ziel.pk).update(parent=freigegeben)

    with pytest.raises(ValidationError) as fehler:
        ordner.save_checked(mover=verschiebt)

    assert fehler.value.message_dict == {"parent": [GUEST_FOLDER_SUBTREE_DENIED]}
    assert DocumentFolder.objects.get(pk=ordner.pk).parent_id is None


def test_ordner_ohne_zyklus_wie_bisher(org: Any, make_member: Any) -> None:
    autorin = make_member(org, ["motions.view", "motions.create"], email="autorin@example.org")
    a = DocumentFolder.objects.create(organization=org, name="A", created_by=autorin)
    b = DocumentFolder.objects.create(organization=org, name="B", parent=a, created_by=autorin)
    c = DocumentFolder.objects.create(organization=org, name="C", parent=b, created_by=autorin)

    assert [ordner.name for ordner in c.get_ancestors()] == ["A", "B"]
    assert (a.get_depth(), b.get_depth(), c.get_depth()) == (1, 2, 3)
    assert (a.get_subtree_height(), b.get_subtree_height(), c.get_subtree_height()) == (3, 2, 1)
    assert {ordner.name for ordner in a.get_descendants()} == {"B", "C"}
