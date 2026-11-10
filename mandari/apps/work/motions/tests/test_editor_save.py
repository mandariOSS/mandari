# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Speichern im Editor.

- Die beim Anlegen erfasste Zusammenfassung bleibt erhalten, wenn der Editor
  sie nicht mitschickt (#183).
- Ein per POST geänderter Inhalt verwirft den veralteten Kollaborationsstand,
  damit er beim nächsten Öffnen nicht gewinnt; verbundene Clients laden neu
  (#184). Unveränderter Inhalt lässt beides unberührt.
- Speichern ohne Kollaborationsverbindung schickt den Fingerabdruck des Stands
  mit, von dem der Editor ausgeht. Passt er nicht mehr, antwortet der Server mit
  409 statt still zu überschreiben; nur ``force`` überschreibt bewusst (#184).
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from apps.work.motions.models import Motion, content_fingerprint
from apps.work.motions.views import editor as editor_views

PERMISSIONS = ["motions.view", "motions.edit", "motions.comment"]
STALE_STATE = b"veralteter-kollaborationsstand"


@pytest.fixture
def author(org: Any, make_member: Any) -> Any:
    return make_member(org, PERMISSIONS, email="autorin@example.org")


@pytest.fixture
def motion(org: Any, author: Any) -> Motion:
    motion = Motion.objects.create(
        organization=org,
        author=author,
        title="Antrag Radweg",
        summary="Kurzfassung",
        visibility="organization",
    )
    cast(Any, motion).set_content_encrypted("<p>Alt</p>")
    motion.set_yjs_state(STALE_STATE)
    motion.save()
    return motion


@pytest.fixture
def reloads(monkeypatch: pytest.MonkeyPatch) -> list[Motion]:
    calls: list[Motion] = []
    monkeypatch.setattr(editor_views, "_broadcast_doc_reload", lambda motion, version=None: calls.append(motion))
    return calls


def save(client: Any, org: Any, motion: Motion, **fields: str) -> Any:
    data = {"action": "save", "title": motion.title, **fields}
    return client.post(f"/work/{org.slug}/documents/{motion.id}/", data, HTTP_X_REQUESTED_WITH="XMLHttpRequest")


@pytest.mark.django_db
def test_speichern_ohne_zusammenfassung_behaelt_sie(
    org: Any, author: Any, motion: Motion, reloads: list[Motion], client_for: Any
) -> None:
    response = save(client_for(author.user), org, motion, content="<p>Neu</p>")

    assert response.status_code == 200
    assert response.json()["success"] is True
    motion.refresh_from_db()
    assert motion.summary == "Kurzfassung"


@pytest.mark.django_db
def test_mitgeschickte_zusammenfassung_wird_uebernommen(
    org: Any, author: Any, motion: Motion, reloads: list[Motion], client_for: Any
) -> None:
    save(client_for(author.user), org, motion, content="<p>Alt</p>", summary="Neue Kurzfassung")

    motion.refresh_from_db()
    assert motion.summary == "Neue Kurzfassung"


@pytest.mark.django_db
def test_geaenderter_inhalt_verwirft_veralteten_kollaborationsstand(
    org: Any, author: Any, motion: Motion, reloads: list[Motion], client_for: Any
) -> None:
    save(client_for(author.user), org, motion, content="<p>Neu</p>")

    motion.refresh_from_db()
    assert cast(Any, motion).get_content_decrypted() == "<p>Neu</p>"
    assert motion.get_yjs_state() is None
    assert [reloaded.pk for reloaded in reloads] == [motion.pk]


@pytest.mark.django_db
def test_unveraenderter_inhalt_behaelt_kollaborationsstand(
    org: Any, author: Any, motion: Motion, reloads: list[Motion], client_for: Any
) -> None:
    save(client_for(author.user), org, motion, content="<p>Alt</p>")

    motion.refresh_from_db()
    assert motion.get_yjs_state() == STALE_STATE
    assert reloads == []


@pytest.mark.django_db
def test_editor_bekommt_fingerabdruck_des_stands(org: Any, author: Any, motion: Motion, client_for: Any) -> None:
    response = client_for(author.user).get(f"/work/{org.slug}/documents/{motion.id}/")

    assert response.status_code == 200
    assert response.context["editor_config"]["contentHash"] == content_fingerprint("<p>Alt</p>")


@pytest.mark.django_db
def test_veralteter_stand_wird_nicht_still_ueberschrieben(
    org: Any, author: Any, motion: Motion, reloads: list[Motion], client_for: Any
) -> None:
    response = save(client_for(author.user), org, motion, content="<p>Neu</p>", base_content_hash="veraltet")

    assert response.status_code == 409
    daten = response.json()
    assert daten["error"] == "conflict"
    assert daten["content_hash"] == content_fingerprint("<p>Alt</p>")
    assert "an anderer Stelle geändert" in daten["message"]
    motion.refresh_from_db()
    assert cast(Any, motion).get_content_decrypted() == "<p>Alt</p>"
    assert motion.get_yjs_state() == STALE_STATE
    assert reloads == []


@pytest.mark.django_db
def test_passender_stand_speichert_und_liefert_neuen_fingerabdruck(
    org: Any, author: Any, motion: Motion, client_for: Any
) -> None:
    response = save(
        client_for(author.user),
        org,
        motion,
        content="<p>Neu</p>",
        base_content_hash=content_fingerprint("<p>Alt</p>"),
    )

    assert response.status_code == 200
    assert response.json()["content_hash"] == content_fingerprint("<p>Neu</p>")
    motion.refresh_from_db()
    assert cast(Any, motion).get_content_decrypted() == "<p>Neu</p>"


@pytest.mark.django_db
def test_bewusstes_ueberschreiben_mit_force(org: Any, author: Any, motion: Motion, client_for: Any) -> None:
    response = save(client_for(author.user), org, motion, content="<p>Neu</p>", base_content_hash="veraltet", force="1")

    assert response.status_code == 200
    motion.refresh_from_db()
    assert cast(Any, motion).get_content_decrypted() == "<p>Neu</p>"


@pytest.mark.django_db
def test_unveraenderter_inhalt_ist_kein_konflikt(org: Any, author: Any, motion: Motion, client_for: Any) -> None:
    response = save(client_for(author.user), org, motion, content="<p>Alt</p>", base_content_hash="veraltet")

    assert response.status_code == 200


@pytest.mark.django_db
def test_ohne_inhalt_nur_titel_typ_und_briefpapier(
    org: Any, author: Any, motion: Motion, reloads: list[Motion], client_for: Any
) -> None:
    """Mit Live-Zusammenarbeit schickt der Editor keinen Text (#1018): Text und Yjs-Zustand bleiben unberührt."""
    from apps.work.motions.models import MotionType, OrganizationLetterhead

    typ = MotionType.objects.create(organization=org, name="Anfrage", slug="anfrage")
    briefkopf = OrganizationLetterhead.objects.create(organization=org, name="Fraktion", kind="generated")
    revisionen_vorher = motion.revisions.count()

    response = save(
        client_for(author.user),
        org,
        motion,
        title="Neuer Titel",
        document_type_id=str(typ.id),
        letterhead_id=str(briefkopf.id),
    )

    assert response.status_code == 200
    assert response.json()["success"] is True
    motion.refresh_from_db()
    assert motion.title == "Neuer Titel"
    assert motion.document_type_id == typ.id
    assert motion.letterhead_id == briefkopf.id
    assert cast(Any, motion).get_content_decrypted() == "<p>Alt</p>"
    assert motion.get_yjs_state() == STALE_STATE
    assert motion.summary == "Kurzfassung"
    assert motion.revisions.count() == revisionen_vorher
    assert reloads == []


@pytest.mark.django_db
def test_ohne_inhalt_ueberschreibt_kein_gleichzeitiges_sichern(
    org: Any, author: Any, motion: Motion, reloads: list[Motion], client_for: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sichert die Verbindung während des POST einen neuen Text, schreibt der POST ihn nicht mit dem alten zurück."""
    from apps.work.motions.views.editor import DocumentEditorView

    echt: Any = DocumentEditorView._get_access_level

    def mit_gleichzeitigem_sichern(self: Any, geladen: Motion) -> str:
        # Nach dem Laden im View: der Consumer speichert den neuen Kollaborationsstand
        neu = Motion.objects.get(pk=geladen.pk)
        cast(Any, neu).set_content_encrypted("<p>Neu über Yjs</p>")
        neu.set_yjs_state(b"neuer-zustand")
        neu.save()
        return str(echt(self, geladen))

    monkeypatch.setattr(DocumentEditorView, "_get_access_level", mit_gleichzeitigem_sichern)

    response = save(client_for(author.user), org, motion, title="Titel neu")

    assert response.status_code == 200
    motion.refresh_from_db()
    assert motion.title == "Titel neu"
    assert cast(Any, motion).get_content_decrypted() == "<p>Neu über Yjs</p>"
    assert motion.get_yjs_state() == b"neuer-zustand"


@pytest.mark.django_db
def test_fremdes_formular_leert_nichts(
    org: Any, author: Any, motion: Motion, reloads: list[Motion], client_for: Any
) -> None:
    """Postet ein anderes Formular versehentlich an den Editor (Teilen-Dialog mit leerem Ziel), bleibt alles (#1018)."""
    from apps.work.motions.models import MotionType

    typ = MotionType.objects.create(organization=org, name="Anfrage", slug="anfrage")
    motion.document_type = typ
    motion.save(update_fields=["document_type"])

    response = client_for(author.user).post(
        f"/work/{org.slug}/documents/{motion.id}/", {"visibility": "organization"}, HTTP_HX_REQUEST="true"
    )

    assert response.status_code == 400
    motion.refresh_from_db()
    assert cast(Any, motion).get_content_decrypted() == "<p>Alt</p>"
    assert motion.document_type_id == typ.id
    assert motion.get_yjs_state() == STALE_STATE
    assert reloads == []


@pytest.mark.django_db
def test_nur_titel_laesst_typ_und_briefpapier(
    org: Any, author: Any, motion: Motion, reloads: list[Motion], client_for: Any
) -> None:
    from apps.work.motions.models import MotionType

    typ = MotionType.objects.create(organization=org, name="Anfrage", slug="anfrage")
    motion.document_type = typ
    motion.save(update_fields=["document_type"])

    response = save(client_for(author.user), org, motion, title="Nur der Titel")

    assert response.status_code == 200
    motion.refresh_from_db()
    assert motion.title == "Nur der Titel"
    assert motion.document_type_id == typ.id


@pytest.mark.django_db
def test_ohne_inhalt_schreibt_nur_mitgeschickte_felder(
    org: Any, author: Any, motion: Motion, reloads: list[Motion], client_for: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Zwei Fenster mit Verbindung (#1018): Ändert eines den Titel und das andere danach nur das Briefpapier, setzt dessen
    POST den neuen Titel nicht auf den alten Stand zurück.
    """
    from apps.work.motions.models import OrganizationLetterhead
    from apps.work.motions.views.editor import DocumentEditorView

    briefkopf = OrganizationLetterhead.objects.create(organization=org, name="Fraktion", kind="generated")
    echt: Any = DocumentEditorView._get_access_level

    def anderes_fenster_aendert(self: Any, geladen: Motion) -> str:
        # Nach dem Laden im View sichert ein anderes Fenster Titel und Zusammenfassung
        Motion.objects.filter(pk=geladen.pk).update(title="Titel aus dem anderen Fenster", summary="Neu anderswo")
        return str(echt(self, geladen))

    monkeypatch.setattr(DocumentEditorView, "_get_access_level", anderes_fenster_aendert)

    response = client_for(author.user).post(
        f"/work/{org.slug}/documents/{motion.id}/",
        {"action": "save", "letterhead_id": str(briefkopf.id)},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert response.status_code == 200
    motion.refresh_from_db()
    assert motion.letterhead_id == briefkopf.id
    assert motion.title == "Titel aus dem anderen Fenster"
    assert motion.summary == "Neu anderswo"
    assert cast(Any, motion).get_content_decrypted() == "<p>Alt</p>"


# --- Reihenfolge der POSTs für Titel, Dokumenttyp und Briefpapier (#1199) ---


def _meta(client: Any, org: Any, motion: Motion, **fields: str) -> Any:
    """Speicher-POST ohne Text, wie ihn der Editor mit Live-Zusammenarbeit schickt."""
    return client.post(
        f"/work/{org.slug}/documents/{motion.id}/", {"action": "save", **fields}, HTTP_X_REQUESTED_WITH="XMLHttpRequest"
    )


@pytest.mark.django_db
def test_editor_bekommt_stand_der_kopfdaten(org: Any, author: Any, motion: Motion, client_for: Any) -> None:
    Motion.objects.filter(pk=motion.pk).update(meta_version=4)

    response = client_for(author.user).get(f"/work/{org.slug}/documents/{motion.id}/")

    assert response.context["editor_config"]["metaVersion"] == 4


@pytest.mark.django_db
def test_ueberholter_meta_post_gewinnt_nicht(org: Any, author: Any, motion: Motion, client_for: Any) -> None:
    """
    Der Browser bricht den POST mit „Zwischentitel“ nach dem Zeitlimit ab und wiederholt mit dem neuen Titel; beide
    gehen vom selben Stand aus. Verarbeitet der Server den älteren erst danach, schreibt er nichts mehr.
    """
    client = client_for(author.user)

    wiederholung = _meta(client, org, motion, title="Neuer Titel", meta_version="0")
    verspaetet = _meta(client, org, motion, title="Zwischentitel", meta_version="0")

    assert wiederholung.status_code == 200
    assert wiederholung.json()["meta_version"] == 1
    assert verspaetet.status_code == 409
    assert verspaetet.json()["error"] == "meta_ueberholt"
    assert verspaetet.json()["meta_version"] == 1
    motion.refresh_from_db()
    assert motion.title == "Neuer Titel"
    assert motion.meta_version == 1


@pytest.mark.django_db
def test_ueberholter_meta_post_schreibt_auch_typ_und_briefpapier_nicht(
    org: Any, author: Any, motion: Motion, client_for: Any
) -> None:
    from apps.work.motions.models import MotionType, OrganizationLetterhead

    typ = MotionType.objects.create(organization=org, name="Anfrage", slug="anfrage")
    briefkopf = OrganizationLetterhead.objects.create(organization=org, name="Fraktion", kind="generated")
    client = client_for(author.user)

    assert _meta(client, org, motion, document_type_id="", letterhead_id="", meta_version="0").status_code == 200
    verspaetet = _meta(
        client, org, motion, document_type_id=str(typ.id), letterhead_id=str(briefkopf.id), meta_version="0"
    )

    assert verspaetet.status_code == 409
    motion.refresh_from_db()
    assert motion.document_type_id is None
    assert motion.letterhead_id is None


@pytest.mark.django_db
def test_abgelehnter_post_gelingt_mit_dem_stand_des_servers(
    org: Any, author: Any, motion: Motion, client_for: Any
) -> None:
    """Kam der abgebrochene POST doch an, bekommt die Wiederholung den Stand des Servers und gelingt damit."""
    client = client_for(author.user)
    assert _meta(client, org, motion, title="Zwischentitel", meta_version="0").status_code == 200

    abgelehnt = _meta(client, org, motion, title="Antrag Radweg", meta_version="0")
    wiederholt = _meta(client, org, motion, title="Antrag Radweg", meta_version=str(abgelehnt.json()["meta_version"]))

    assert abgelehnt.status_code == 409
    assert wiederholt.status_code == 200
    assert wiederholt.json()["meta_version"] == 2
    motion.refresh_from_db()
    assert motion.title == "Antrag Radweg"


@pytest.mark.django_db
def test_ueberholter_post_mit_text_schreibt_nichts(
    org: Any, author: Any, motion: Motion, reloads: list[Motion], client_for: Any
) -> None:
    """Ohne Verbindung gehen die Felder mit dem Text: Der überholte Request schreibt weder Titel noch Text."""
    client = client_for(author.user)
    basis = content_fingerprint("<p>Alt</p>")
    erster = save(
        client, org, motion, content="<p>Neu</p>", title="Neuer Titel", base_content_hash=basis, meta_version="0"
    )
    assert erster.status_code == 200
    revisionen = motion.revisions.count()

    # Gleicher Text wie gespeichert (kein Inhaltskonflikt), aber der ältere Titel
    verspaetet = save(
        client, org, motion, content="<p>Neu</p>", title="Zwischentitel", base_content_hash=basis, meta_version="0"
    )
    # Anderer Text, bewusst überschrieben: Auch dann gilt die Reihenfolge der Felder
    erzwungen = save(client, org, motion, content="<p>Älter</p>", title="Zwischentitel", force="1", meta_version="0")

    assert verspaetet.status_code == 409
    assert verspaetet.json()["error"] == "meta_ueberholt"
    assert erzwungen.status_code == 409
    motion.refresh_from_db()
    assert motion.title == "Neuer Titel"
    assert cast(Any, motion).get_content_decrypted() == "<p>Neu</p>"
    assert motion.revisions.count() == revisionen
    assert motion.meta_version == 1


@pytest.mark.django_db
def test_post_mit_text_und_feldern_liefert_den_neuen_stand(
    org: Any, author: Any, motion: Motion, reloads: list[Motion], client_for: Any
) -> None:
    response = save(client_for(author.user), org, motion, content="<p>Neu</p>", title="Neuer Titel", meta_version="0")

    assert response.status_code == 200
    assert response.json()["meta_version"] == 1
    motion.refresh_from_db()
    assert motion.meta_version == 1
    assert motion.title == "Neuer Titel"


@pytest.mark.django_db
def test_post_ohne_felder_zaehlt_nicht(
    org: Any, author: Any, motion: Motion, reloads: list[Motion], client_for: Any
) -> None:
    """Nur Text: Der Stand der Kopfdaten bleibt, auch eine veraltete Angabe stört nicht."""
    response = client_for(author.user).post(
        f"/work/{org.slug}/documents/{motion.id}/",
        {"action": "save", "content": "<p>Neu</p>", "meta_version": "7"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert response.status_code == 200
    assert "meta_version" not in response.json()
    motion.refresh_from_db()
    assert motion.meta_version == 0
    assert cast(Any, motion).get_content_decrypted() == "<p>Neu</p>"


@pytest.mark.django_db
def test_ohne_angabe_wird_gezaehlt_aber_nicht_geprueft(org: Any, author: Any, motion: Motion, client_for: Any) -> None:
    """Eine ältere Fassung des Editors im Browser schickt keinen Stand: Sie speichert weiter, andere bemerken es."""
    client = client_for(author.user)
    Motion.objects.filter(pk=motion.pk).update(meta_version=3)

    ohne_angabe = _meta(client, org, motion, title="Ohne Angabe")
    danach = _meta(client, org, motion, title="Von Stand 3", meta_version="3")

    assert ohne_angabe.status_code == 200
    assert ohne_angabe.json()["meta_version"] == 4
    assert danach.status_code == 409
    motion.refresh_from_db()
    assert motion.title == "Ohne Angabe"


@pytest.mark.django_db
@pytest.mark.parametrize("angabe", ["abc", "-1", "1.5", "99999999999999999999"])
def test_unlesbare_angabe_passt_zu_keinem_stand(
    org: Any, author: Any, motion: Motion, client_for: Any, angabe: str
) -> None:
    response = _meta(client_for(author.user), org, motion, title="Neuer Titel", meta_version=angabe)

    assert response.status_code == 409
    assert response.json()["meta_version"] == 0
    motion.refresh_from_db()
    assert motion.title == "Antrag Radweg"
    assert motion.meta_version == 0


@pytest.mark.django_db
def test_ueberholtes_formular_ohne_javascript_bekommt_hinweis(
    org: Any, author: Any, motion: Motion, client_for: Any
) -> None:
    Motion.objects.filter(pk=motion.pk).update(meta_version=2)

    response = client_for(author.user).post(
        f"/work/{org.slug}/documents/{motion.id}/",
        {"action": "save", "title": "Zwischentitel", "meta_version": "1"},
        follow=True,
    )

    assert response.status_code == 200
    assert "inzwischen an anderer Stelle gespeichert" in [str(m) for m in response.context["messages"]][0]
    motion.refresh_from_db()
    assert motion.title == "Antrag Radweg"
