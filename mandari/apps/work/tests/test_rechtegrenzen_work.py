# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rechtegrenzen in Work, die an mehreren Stellen dieselbe Regel brauchen.

- Fraktionssitzung: Aufgaben aus Protokolleinträgen nur mit ``tasks.create``; nach genehmigtem Protokoll sind
  Tagesordnung und Beschlüsse fest, auch wenn der Status der Sitzung zurückgesetzt wird.
- Dokumentimport: eine andere Sichtbarkeit als „privat“ nur mit dem Freigaberecht.
- Aufgaben: Freigaben zählen nur bei Sichtbarkeit „geteilt“ und entfallen beim Wechsel auf eine andere.
- Gastseite: Titel freigegebener Dokumente nur, wenn die betrachtende Person das Dokument sehen oder die Freigabe
  verwalten darf.
- Persönlicher Datenexport: Titel und Stand nur von Inhalten, auf die die Person heute noch Zugriff hat.
- Einbettungs-Snippet der Termine: setzt Titel und Ort als Text, nicht als HTML.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from apps.common.tests.factories import MembershipFactory, UserFactory
from apps.work.faction import agenda, deletion
from apps.work.faction import services as faction_services
from apps.work.faction.models import FactionAgendaItem, FactionDecision, FactionMeeting, FactionProtocolEntry
from apps.work.faction.regeln import ProtokollGenehmigtError
from apps.work.motions.import_service import MotionImportService
from apps.work.motions.models import Motion, MotionComment, MotionShare
from apps.work.organization import export_service, selectors
from apps.work.tasks import selectors as task_selectors
from apps.work.tasks import services as task_services
from apps.work.tasks.models import Task, TaskActivity, TaskShare

pytestmark = pytest.mark.django_db

LEITUNG = ["faction.view_public", "faction.manage", "protocols.create", "protocols.edit", "tasks.create"]


@pytest.fixture
def leitung(org: Any, make_member: Any) -> Any:
    return make_member(org, LEITUNG, email="leitung@example.org")


@pytest.fixture
def sitzung(org: Any, leitung: Any) -> FactionMeeting:
    return FactionMeeting.objects.create(
        organization=org,
        title="Fraktionssitzung",
        start=timezone.now() - timedelta(minutes=30),
        status="ongoing",
        created_by=leitung,
    )


@pytest.fixture
def top(sitzung: FactionMeeting) -> FactionAgendaItem:
    return FactionAgendaItem.objects.create(meeting=sitzung, number="1", title="Haushalt", visibility="public", order=1)


def _genehmigen(meeting: FactionMeeting, *, status: str = "ongoing") -> None:
    """Protokoll genehmigt, Status danach zurückgesetzt (der Zustand, in dem die Sperre gelten muss)."""
    FactionMeeting.objects.filter(pk=meeting.pk).update(protocol_approved=True, status=status)
    meeting.refresh_from_db()


# --- Aufgabe aus Protokolleintrag -----------------------------------------------------------------------------


def _aufgabeneintrag(sitzung: FactionMeeting, top: FactionAgendaItem, leitung: Any) -> FactionProtocolEntry:
    eintrag: Any = FactionProtocolEntry(meeting=sitzung, agenda_item=top, entry_type="action", created_by=leitung)
    eintrag.save()
    eintrag.set_content_encrypted("Stellungnahme zum Haushalt vorbereiten")
    eintrag.save()
    return eintrag  # type: ignore[no-any-return]


def _uebernehmen(client_for: Any, mitglied: Any, org: Any, sitzung: FactionMeeting, eintrag: Any) -> Any:
    url = reverse("work:faction_action", kwargs={"org_slug": org.slug, "meeting_id": sitzung.pk})
    return client_for(mitglied.user).post(url, {"action": "create_task", "entry_id": str(eintrag.pk)})


def test_aufgabe_aus_eintrag_nur_mit_recht_aufgaben_anzulegen(
    org: Any, make_member: Any, client_for: Any, leitung: Any, sitzung: FactionMeeting, top: FactionAgendaItem
) -> None:
    eintrag = _aufgabeneintrag(sitzung, top, leitung)
    ohne = make_member(org, ["faction.view_public"], email="ohne@example.org")

    antwort = _uebernehmen(client_for, ohne, org, sitzung, eintrag)

    assert antwort.status_code == 403
    assert not Task.objects.filter(related_protocol_entry=eintrag).exists()
    eintrag.refresh_from_db()
    assert not eintrag.action_completed


def test_aufgabe_aus_eintrag_einmal_und_erledigt_nur_mit_protokollrecht(
    org: Any, make_member: Any, client_for: Any, leitung: Any, sitzung: FactionMeeting, top: FactionAgendaItem
) -> None:
    eintrag = _aufgabeneintrag(sitzung, top, leitung)
    mitglied = make_member(org, ["faction.view_public", "tasks.create"], email="mitglied@example.org")

    _uebernehmen(client_for, mitglied, org, sitzung, eintrag)
    _uebernehmen(client_for, mitglied, org, sitzung, eintrag)

    # Eine Aufgabe, auch beim zweiten Aufruf; den Erledigt-Stand setzt nur, wer protokollieren darf
    assert Task.objects.filter(related_protocol_entry=eintrag).count() == 1
    eintrag.refresh_from_db()
    assert not eintrag.action_completed

    zweiter = _aufgabeneintrag(sitzung, top, leitung)
    _uebernehmen(client_for, leitung, org, sitzung, zweiter)
    zweiter.refresh_from_db()
    assert zweiter.action_completed


# --- Genehmigtes Protokoll -----------------------------------------------------------------------------------


def test_genehmigtes_protokoll_sperrt_die_verwaltung_der_tagesordnung(
    org: Any, make_member: Any, leitung: Any, sitzung: FactionMeeting
) -> None:
    andere = make_member(org, ["faction.view_public"], email="andere@example.org")
    assert agenda.manages(sitzung, leitung) and agenda.editable_by(sitzung, leitung)
    assert not agenda.manages(sitzung, andere)

    _genehmigen(sitzung)

    assert not agenda.manages(sitzung, leitung)
    assert not agenda.editable_by(sitzung, leitung)


def test_beschluss_nach_genehmigung_weder_aendern_noch_entfernen(
    org: Any, client_for: Any, leitung: Any, sitzung: FactionMeeting, top: FactionAgendaItem
) -> None:
    werte: dict[str, Any] = {
        "votes_yes": 5,
        "votes_no": 1,
        "votes_abstain": 0,
        "result": "accepted",
        "decision_text": "Zustimmung",
    }
    faction_services.record_decision(top, leitung, **werte)
    _genehmigen(sitzung)

    with pytest.raises(ProtokollGenehmigtError):
        faction_services.record_decision(top, leitung, **{**werte, "votes_yes": 0, "result": "rejected"})

    panel = reverse(
        "work:faction_item_panel_action",
        kwargs={"org_slug": org.slug, "meeting_id": sitzung.pk, "item_id": top.pk},
    )
    client = client_for(leitung.user)
    for aktion in ("record_decision", "clear_decision"):
        antwort = client.post(panel, {"action": aktion, "votes_yes": "0", "votes_no": "9", "result": "rejected"})
        assert antwort.status_code == 403, aktion
    beschluss = FactionDecision.objects.get(agenda_item=top)
    assert (beschluss.votes_yes, beschluss.result) == (5, "accepted")


def test_top_nach_genehmigung_nicht_loeschen(leitung: Any, sitzung: FactionMeeting, top: FactionAgendaItem) -> None:
    eintrag = _aufgabeneintrag(sitzung, top, leitung)
    _genehmigen(sitzung)

    with pytest.raises(deletion.DeletionBlockedError):
        deletion.delete_agenda_item(top, top.title)

    assert FactionAgendaItem.objects.filter(pk=top.pk).exists()
    assert FactionProtocolEntry.objects.filter(pk=eintrag.pk).exists()


# --- Dokumentimport ---------------------------------------------------------------------------------------------


def _importieren(org: Any, autorin: Any, sichtbarkeit: str) -> Motion:
    datei = SimpleUploadedFile("antrag.pdf", b"%PDF-1.4 test", content_type="application/pdf")
    motion, _anhang = MotionImportService._create(
        uploaded_file=datei,
        organization=org,
        author=autorin,
        motion_type=None,
        title="Importierter Antrag",
        visibility=sichtbarkeit,
        html="<p>Text</p>",
        search_text="Text",
        mime_type="application/pdf",
        file_size=13,
    )
    return motion


def test_import_setzt_sichtbarkeit_nur_mit_freigaberecht(
    org: Any, make_member: Any, tmp_path: Path, settings: Any
) -> None:
    settings.MEDIA_ROOT = str(tmp_path)
    ohne = make_member(org, ["motions.view", "motions.create", "motions.edit"], email="ohne@example.org")
    mit = make_member(org, ["motions.view", "motions.create", "motions.edit", "motions.share"], email="mit@example.org")

    assert _importieren(org, ohne, "organization").visibility == "private"
    assert _importieren(org, mit, "organization").visibility == "organization"
    assert _importieren(org, mit, "private").visibility == "private"


# --- Aufgaben: Freigaben nur bei „geteilt“ -----------------------------------------------------------------------


def test_uebrig_gebliebene_freigabe_oeffnet_keine_private_aufgabe(org: Any, make_member: Any) -> None:
    autorin = make_member(org, ["tasks.view", "tasks.create"], email="autorin@example.org")
    empfaenger = make_member(org, ["tasks.view"], email="empfaenger@example.org")
    aufgabe = Task.objects.create(organization=org, title="Vertraulich", created_by=autorin, visibility="shared")
    TaskShare.objects.create(task=aufgabe, membership=empfaenger, shared_by=autorin)
    basis = Task.objects.filter(organization=org)
    assert aufgabe in task_selectors.own_tasks(empfaenger, base=basis)

    # Sichtbarkeit am Freigabedialog vorbei geändert (Altbestand): Die Zeile bleibt, öffnet aber nichts mehr
    Task.objects.filter(pk=aufgabe.pk).update(visibility="private")

    assert aufgabe not in task_selectors.own_tasks(empfaenger, base=basis)
    assert aufgabe not in task_selectors.visible_tasks(org, empfaenger)
    assert aufgabe in task_selectors.own_tasks(autorin, base=basis)


def test_wiederteilen_ohne_freigabedialog_beginnt_ohne_empfaenger(org: Any, make_member: Any) -> None:
    autorin = make_member(org, ["tasks.view", "tasks.create"], email="autorin@example.org")
    empfaenger = make_member(org, ["tasks.view"], email="empfaenger@example.org")
    aufgabe = Task.objects.create(organization=org, title="Vertraulich", created_by=autorin, visibility="shared")
    TaskShare.objects.create(task=aufgabe, membership=empfaenger, shared_by=autorin)

    def panel(sichtbarkeit: str) -> None:
        alt = task_services.capture_old_values(aufgabe)
        aufgabe.visibility = sichtbarkeit
        task_services.apply_panel_update(aufgabe, autorin, alt)

    # Unverändert geteilt: Die Empfänger bleiben
    panel("shared")
    assert TaskShare.objects.filter(task=aufgabe).count() == 1
    # Auf privat: Die Zeile bleibt ohne Wirkung stehen (ein veraltetes Panel löscht so keine neuen Freigaben)
    panel("private")
    assert TaskShare.objects.filter(task=aufgabe).count() == 1
    assert not aufgabe.can_access(empfaenger)
    # Wieder geteilt, ohne Empfänger zu wählen: Die früheren bekommen nicht ungefragt wieder Zugriff
    panel("shared")
    assert not TaskShare.objects.filter(task=aufgabe).exists()
    assert not aufgabe.can_access(empfaenger)


def test_aufgabe_aus_eintrag_nur_einmal_im_dienst(
    leitung: Any, sitzung: FactionMeeting, top: FactionAgendaItem
) -> None:
    eintrag = _aufgabeneintrag(sitzung, top, leitung)

    erste = task_services.task_from_protocol_entry(eintrag, leitung, title="Stellungnahme", description="Text")
    zweite = task_services.task_from_protocol_entry(eintrag, leitung, title="Stellungnahme", description="Text")

    assert erste is not None and zweite is None
    assert Task.objects.filter(related_protocol_entry=eintrag).count() == 1


# --- Gastseite ---------------------------------------------------------------------------------------------------


def test_gastseite_nennt_nur_sichtbare_oder_verwaltbare_freigaben(org: Any, make_member: Any) -> None:
    autorin = make_member(
        org, ["motions.view", "motions.create", "motions.edit", "motions.share"], email="autorin@example.org"
    )
    leser = make_member(org, ["members.view", "motions.view", "motions.view_drafts"], email="leser@example.org")
    gast_konto: Any = UserFactory(email="gast@example.org")  # type: ignore[no-untyped-call]
    gast: Any = MembershipFactory(user=gast_konto, organization=org, is_guest=True)  # type: ignore[no-untyped-call]
    privat = Motion.objects.create(organization=org, author=autorin, title="Privater Entwurf", visibility="private")
    offen = Motion.objects.create(organization=org, author=autorin, title="Für alle", visibility="organization")
    for dokument in (privat, offen):
        MotionShare.objects.create(motion=dokument, scope="user", user=gast.user, level="view", created_by=autorin.user)

    alle = selectors.guest_document_shares(org, gast.user)
    fuer_leser = selectors.guest_document_shares(org, gast.user, viewer=leser)
    fuer_autorin = selectors.guest_document_shares(org, gast.user, viewer=autorin)

    assert {share.motion.title for share in alle} == {"Privater Entwurf", "Für alle"}
    assert {share.motion.title for share in fuer_leser} == {"Für alle"}
    # Wer die Freigabe verwalten darf, sieht sie weiter (zum Entziehen nötig)
    assert {share.motion.title for share in fuer_autorin} == {"Privater Entwurf", "Für alle"}


# --- Persönlicher Datenexport -------------------------------------------------------------------------------------


def test_export_nennt_stand_einer_aufgabe_nur_mit_zugriff(org: Any, make_member: Any) -> None:
    autorin = make_member(org, ["tasks.view", "tasks.create"], email="autorin@example.org")
    person = make_member(org, ["tasks.view"], email="person@example.org")
    aufgabe = Task.objects.create(
        organization=org, title="Offene Aufgabe", created_by=autorin, visibility="organization"
    )
    TaskActivity.objects.create(task=aufgabe, actor=person, activity_type="comment", content="Mein Kommentar")
    dienst = export_service.DsgvoExportService()

    vorher = dienst._collect_tasks(person, org)
    Task.objects.filter(pk=aufgabe.pk).update(visibility="private", title="GEHEIMER NEUER TITEL", status="done")
    nachher = dienst._collect_tasks(person, org)

    assert [eintrag["title"] for eintrag in vorher] == ["Offene Aufgabe"]
    assert [eintrag["title"] for eintrag in nachher] == [export_service.NO_ACCESS_TASK]
    assert nachher[0]["status"] == "" and nachher[0]["comments"][0]["content"] == "Mein Kommentar"
    assert "GEHEIM" not in str(nachher)


def test_export_nennt_fremde_aufgaben_nur_mit_sichtrecht(org: Any, make_member: Any) -> None:
    autorin = make_member(org, ["tasks.view", "tasks.create"], email="autorin@example.org")
    person = make_member(org, ["dashboard.view"], email="person@example.org")
    fremd = Task.objects.create(organization=org, title="Offene Aufgabe", created_by=autorin, visibility="organization")
    TaskActivity.objects.create(task=fremd, actor=person, activity_type="comment", content="Mein Kommentar")
    Task.objects.create(organization=org, title="Mir zugewiesen", created_by=autorin, assigned_to=person)

    titel = {eintrag["title"] for eintrag in export_service.DsgvoExportService()._collect_tasks(person, org)}

    # Ohne ``tasks.view``: fremde Aufgaben nur als Platzhalter, zugewiesene bleiben
    assert titel == {export_service.NO_ACCESS_TASK, "Mir zugewiesen"}


def test_export_nennt_stand_eines_dokuments_nur_mit_zugriff(org: Any, make_member: Any) -> None:
    autorin = make_member(
        org, ["motions.view", "motions.create", "motions.edit", "motions.share"], email="autorin@example.org"
    )
    person = make_member(org, ["motions.view", "motions.view_drafts", "motions.comment"], email="person@example.org")
    dokument = Motion.objects.create(
        organization=org, author=autorin, title="Gemeinsamer Antrag", visibility="organization"
    )
    MotionComment.objects.create(motion=dokument, author=person, content="Mein Hinweis")
    dienst = export_service.DsgvoExportService()

    vorher = dienst._collect_document_involvement(person, org)
    Motion.objects.filter(pk=dokument.pk).update(visibility="private", title="GEHEIMER NEUER TITEL")
    nachher = dienst._collect_document_involvement(person, org)

    assert [eintrag["title"] for eintrag in vorher] == ["Gemeinsamer Antrag"]
    assert [eintrag["title"] for eintrag in nachher] == [export_service.NO_ACCESS_DOCUMENT]
    assert nachher[0]["status"] == "" and len(nachher[0]["comments"]) == 1
    assert "GEHEIM" not in str(nachher)


def test_export_nennt_nichtoeffentlichen_vorschlag_nur_mit_sicht(
    org: Any, make_member: Any, sitzung: FactionMeeting
) -> None:
    person = make_member(org, ["faction.view_public", "faction.view_non_public"], email="person@example.org")
    person.is_sworn_in = True
    person.save(update_fields=["is_sworn_in"])
    FactionAgendaItem.objects.create(
        meeting=sitzung,
        number="",
        title="GEHEIMER Grundstückskauf",
        visibility="internal",
        order=5,
        proposal_status="rejected",
        proposed_by=person,
        rejection_reason="GEHEIMER Grund",
        proposed_at=timezone.now(),
    )
    dienst = export_service.DsgvoExportService()

    vorher = dienst._collect_faction(person, org)["proposals"]
    person.is_sworn_in = False
    person.save(update_fields=["is_sworn_in"])
    nachher = dienst._collect_faction(person, org)["proposals"]

    assert vorher[0]["title"] == "GEHEIMER Grundstückskauf" and vorher[0]["rejection_reason"] == "GEHEIMER Grund"
    assert vorher[0]["status"] == "rejected"
    assert nachher[0]["title"] == export_service.NO_ACCESS_ITEM and nachher[0]["rejection_reason"] == ""
    # Auch der Stand der Entscheidung gehört zum nichtöffentlichen Teil; der Zeitpunkt des eigenen Vorschlags bleibt
    assert nachher[0]["status"] == "" and nachher[0]["proposed_at"]
    assert "GEHEIM" not in str(nachher)


def test_export_nennt_stand_eigener_dokumente_nur_mit_zugriff(org: Any, make_member: Any) -> None:
    from apps.tenants.models import Permission
    from apps.work.motions.models import MotionRevision

    person = make_member(org, ["motions.view", "motions.create", "motions.edit"], email="person@example.org")
    andere = make_member(org, ["motions.view", "motions.edit_all"], email="andere@example.org")
    dokument = Motion.objects.create(organization=org, author=person, title="Mein Antrag", visibility="organization")
    MotionRevision.objects.create(motion=dokument, version=1, changed_by=person, change_summary="Erste Fassung")
    MotionComment.objects.create(motion=dokument, author=person, content="Eigene Notiz")
    dienst = export_service.DsgvoExportService()

    vorher = dienst._collect_motions(person, org)
    # Sichtrecht entzogen; danach ändern andere das Dokument
    for rolle in person.roles.all():
        rolle.permissions.remove(*Permission.objects.filter(codename="motions.view"))
    person = type(person).objects.get(pk=person.pk)
    Motion.objects.filter(pk=dokument.pk).update(title="GEHEIMER NEUER TITEL", summary="GEHEIME Zusammenfassung")
    MotionRevision.objects.create(motion=dokument, version=2, changed_by=andere, change_summary="GEHEIME Änderung")
    nachher = dienst._collect_motions(person, org)

    assert vorher[0]["title"] == "Mein Antrag" and len(vorher[0]["revisions"]) == 1
    assert not person.has_permission("motions.view")
    assert nachher[0]["title"] == export_service.NO_ACCESS_DOCUMENT and nachher[0]["status"] == ""
    # Eigene Fassungen und Kommentare bleiben, die Fassung der anderen Person nicht
    assert [fassung["change_summary"] for fassung in nachher[0]["revisions"]] == ["Erste Fassung"]
    assert [kommentar["content"] for kommentar in nachher[0]["comments"]] == ["Eigene Notiz"]
    assert "GEHEIM" not in str(nachher)


def test_export_meldet_fruehere_freigabe_einer_aufgabe_nicht_als_aktuell(org: Any, make_member: Any) -> None:
    from apps.work.tasks.models import TaskShare

    autorin = make_member(org, ["tasks.view", "tasks.create"], email="autorin@example.org")
    person = make_member(org, ["tasks.view"], email="person@example.org")
    aufgabe = Task.objects.create(organization=org, title="Für alle", created_by=autorin, visibility="organization")
    TaskShare.objects.create(task=aufgabe, membership=person)
    TaskActivity.objects.create(task=aufgabe, actor=person, activity_type="comment", content="Mein Kommentar")

    eintrag = export_service.DsgvoExportService()._collect_tasks(person, org)[0]

    assert eintrag["title"] == "Für alle" and eintrag["is_shared_with"] is False

    Task.objects.filter(pk=aufgabe.pk).update(visibility="shared")
    assert export_service.DsgvoExportService()._collect_tasks(person, org)[0]["is_shared_with"] is True


# --- Vorschlag nach genehmigtem Protokoll ----------------------------------------------------------------------


def test_vorschlag_wird_nach_genehmigtem_protokoll_nicht_mehr_angenommen(
    org: Any, make_member: Any, client_for: Any, sitzung: FactionMeeting
) -> None:
    from apps.work.faction.regeln import PROTOKOLL_GENEHMIGT, ProtokollGenehmigtError
    from apps.work.faction.services import AgendaProposalService

    vorsitz = make_member(org, [*LEITUNG, "agenda.approve"], email="vorsitz@example.org")
    vorschlag = FactionAgendaItem.objects.create(
        meeting=sitzung,
        number="",
        title="Nachträglicher Punkt",
        visibility="public",
        order=9,
        proposal_status="proposed",
        proposed_by=vorsitz,
        proposed_at=timezone.now(),
    )
    _genehmigen(sitzung)

    with pytest.raises(ProtokollGenehmigtError):
        AgendaProposalService.accept_proposal(vorschlag, vorsitz)
    url = reverse("work:faction_action", kwargs={"org_slug": org.slug, "meeting_id": sitzung.pk})
    antwort = client_for(vorsitz.user).post(
        url, {"action": "accept_proposal", "item_id": str(vorschlag.pk)}, HTTP_HX_REQUEST="true"
    )

    assert antwort.status_code == 400 and antwort.content.decode() == PROTOKOLL_GENEHMIGT
    vorschlag.refresh_from_db()
    assert vorschlag.proposal_status == "proposed"


# --- Einbettungs-Snippet -----------------------------------------------------------------------------------------


def test_snippet_setzt_titel_und_ort_als_text() -> None:
    vorlagen = Path(settings.BASE_DIR) / "templates" / "work" / "organization"
    quelltext = (vorlagen / "partials" / "_termine_snippet.html").read_text(encoding="utf-8")

    assert "fraktions-termine" in quelltext
    assert "innerHTML" not in quelltext
    assert "document.createElement" in quelltext and "textContent" in quelltext
    # Beide Einstellungsseiten zeigen dasselbe Snippet
    for seite in ("api_settings.html", "neu/api.html"):
        assert "partials/_termine_snippet.html" in (vorlagen / seite).read_text(encoding="utf-8")
