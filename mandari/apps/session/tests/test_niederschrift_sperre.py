# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Niederschrift nach der Genehmigung (Issue #318): Sperre, Berichtigung, Stimmrecht, Genehmigungsweg.

- Nach der Genehmigung lehnt die Anwendung Änderungen an Ergebnis, Stimmen und Texten ab – im
  Modell, im Abstimmungs-Service, in den Views, im Admin und bei der Rückschreibung an die
  Beratungsstation. Die API v1 hat keinen Schreibweg dafür, die OParl-API ist rein lesend.
- Korrekturen laufen über die Berichtigung: Recht, Grund, geänderte Werte, Audit-Eintrag,
  Vier-Augen-Prinzip, nichtöffentliche Gründe verschlüsselt.
- Stimmen erfasst die Anwendung nur von stimmberechtigten Anwesenden; Zähler dürfen die Zahl
  der stimmberechtigten Anwesenden nicht übersteigen (hart bei vollständiger Anwesenheit).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

import pytest
from django.contrib import admin
from django.contrib.messages import get_messages
from django.test import Client, RequestFactory
from django.utils import timezone

from apps.accounts.models import User
from apps.session.models import (
    SessionAgendaItem,
    SessionAttendance,
    SessionAuditLog,
    SessionConsultation,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
    SessionProtocol,
    SessionProtocolCorrection,
    SessionVote,
)
from apps.session.services import (
    four_eyes_service,
    protocol_lock,
    protocol_service,
    resolution_service,
    voting_service,
)
from apps.session.services import protocol_correction_service as korrektur
from apps.session.services.protocol_lock import ProtocolLockedError
from apps.session.tests._niederschrift import Welt, base, client, nutzer, welt

pytestmark = pytest.mark.django_db


def _meldungen(response: Any) -> str:
    return " ".join(str(m) for m in get_messages(response.wsgi_request))


def _frisch(item: SessionAgendaItem) -> SessionAgendaItem:
    return SessionAgendaItem.objects.select_related("meeting__tenant").get(pk=item.pk)


def _genehmigen(w: Welt) -> None:
    protokoll = SessionProtocol.objects.get(pk=w.protokoll.pk)
    protokoll.status = "approved"
    protokoll.approved_at = timezone.now()
    protokoll.save()


def _namentlich(w: Welt, stimmen: dict[int, str]) -> None:
    """TOP namentlich abstimmen (vor der Genehmigung)."""
    item = _frisch(w.top)
    item.voting_method = "roll_call"
    item.save()
    voting_service.capture_votes(
        item, {w.stimmberechtigt[i]: wert for i, wert in stimmen.items()}, recorded_by=w.protokollant
    )


# =============================================================================
# Sperre im Modell und im Service
# =============================================================================


class TestSperre:
    @pytest.mark.parametrize(
        ("feld", "wert"),
        [
            ("vote_result", "rejected"),
            ("votes_yes", 5),
            ("votes_abstain", 1),
            ("voting_method", "roll_call"),
            ("resolution_text", "Neuer Beschluss"),
            ("protocol_note", "Neue Aussprache"),
            ("is_withdrawn", True),
        ],
    )
    def test_ergebnis_stimmen_und_texte_gesperrt(self, feld: str, wert: Any) -> None:
        w = welt()
        item = _frisch(w.top)
        setattr(item, feld, wert)
        with pytest.raises(ProtocolLockedError):
            item.save()
        item = _frisch(w.top)
        setattr(item, feld, wert)
        with pytest.raises(ProtocolLockedError):
            item.save(update_fields=[feld, "updated_at"])

    def test_verschluesselte_texte_gesperrt_neu_verschluesseln_erlaubt(self) -> None:
        w = welt()
        item = _frisch(w.top_noe)
        cast(Any, item).set_protocol_note_encrypted("anderer Wortbeitrag")
        with pytest.raises(ProtocolLockedError):
            item.save()
        item = _frisch(w.top_noe)
        cast(Any, item).set_protocol_note_encrypted("GEHEIMVERSCHL Wortbeitrag")
        item.save()  # gleicher Klartext, neuer Schlüsseltext: keine Änderung

    def test_beschlusskontrolle_nummer_und_betreff_bleiben_frei(self) -> None:
        w = welt()
        item = _frisch(w.top)
        item.implementation_status = "done"
        item.save(update_fields=["implementation_status", "updated_at"])
        assert resolution_service.assign_resolution_number(_frisch(w.top)) is True
        item = _frisch(w.top)
        item.name = "Radweg Hauptstraße (Nordabschnitt)"
        item.save()
        assert _frisch(w.top).resolution_number.startswith("B/")

    def test_entwurf_ist_nicht_gesperrt(self) -> None:
        w = welt(status="review")
        item = _frisch(w.top)
        item.vote_result = "rejected"
        item.save()
        assert _frisch(w.top).vote_result == "rejected"

    def test_neue_tops_mit_ergebnis_und_loeschen_gesperrt(self) -> None:
        w = welt()
        with pytest.raises(ProtocolLockedError):
            SessionAgendaItem.objects.create(meeting=w.sitzung, name="Nachgeschoben", vote_result="approved")
        SessionAgendaItem.objects.create(meeting=w.sitzung, name="Ohne Ergebnis")
        with pytest.raises(ProtocolLockedError):
            _frisch(w.top).delete()
        with pytest.raises(ProtocolLockedError):
            SessionMeeting.objects.get(pk=w.sitzung.pk).delete()
        with pytest.raises(ProtocolLockedError):
            SessionProtocol.objects.get(pk=w.protokoll.pk).delete()
        assert SessionAgendaItem.objects.filter(pk=w.top.pk).exists()

    def test_niederschrift_bleibt_genehmigt_und_unveraendert(self) -> None:
        w = welt()
        protokoll = SessionProtocol.objects.get(pk=w.protokoll.pk)
        protokoll.status = "draft"
        with pytest.raises(ProtocolLockedError):
            protokoll.save()
        protokoll = SessionProtocol.objects.get(pk=w.protokoll.pk)
        protokoll.content = "Manipuliert"
        with pytest.raises(ProtocolLockedError):
            protokoll.save()
        protokoll = SessionProtocol.objects.get(pk=w.protokoll.pk)
        protokoll.published_at = timezone.now()
        protokoll.save()  # Veröffentlichungsvermerk ist kein Inhalt

    def test_datenschutz_loeschung_darf_noe_teil_leeren(self) -> None:
        w = welt()
        protokoll = SessionProtocol.objects.get(pk=w.protokoll.pk)
        cast(Any, protokoll).set_content_encrypted("")
        with pytest.raises(ProtocolLockedError):
            protokoll.save(update_fields=["content_encrypted", "updated_at"])
        with protocol_lock.permit(w.sitzung.pk):
            protokoll.save(update_fields=["content_encrypted", "updated_at"])
        assert not cast(Any, SessionProtocol.objects.get(pk=w.protokoll.pk)).get_content_decrypted()

    def test_einzelstimmen_gesperrt(self) -> None:
        w = welt(status="review")
        _namentlich(w, {0: "yes", 1: "yes", 2: "no"})
        _genehmigen(w)
        item = _frisch(w.top)
        with pytest.raises(ProtocolLockedError):
            voting_service.capture_votes(item, {w.stimmberechtigt[2]: "yes"}, recorded_by=w.protokollant)
        with pytest.raises(ProtocolLockedError):
            SessionVote.objects.get(agenda_item=item, person=w.stimmberechtigt[0]).delete()
        stimme = SessionVote.objects.get(agenda_item=item, person=w.stimmberechtigt[1])
        stimme.vote = "no"
        with pytest.raises(ProtocolLockedError):
            stimme.save()
        # Unveränderter Lauf (etwa der Demo-Aufbau) schreibt nichts und scheitert nicht
        voting_service.capture_votes(
            item,
            {w.stimmberechtigt[0]: "yes", w.stimmberechtigt[1]: "yes", w.stimmberechtigt[2]: "no"},
            recorded_by=w.zweite,
        )
        assert SessionVote.objects.get(agenda_item=item, person=w.stimmberechtigt[2]).vote == "no"

    def test_beratungsstation_folgt_nur_dem_top(self) -> None:
        w = welt(status="review")
        vorlage = SessionPaper.objects.create(tenant=w.tenant, name="Radweg", status="approved")
        station = SessionConsultation.objects.create(
            paper=vorlage, organization=w.gremium, meeting=w.sitzung, agenda_item=w.top, role="decision"
        )
        item = _frisch(w.top)
        item.save()  # Rückschreibung des Ergebnisses
        station.refresh_from_db()
        assert station.result == "approved"
        _genehmigen(w)
        station.result = "rejected"
        with pytest.raises(ProtocolLockedError):
            station.save()
        station.refresh_from_db()
        station.agenda_item = None
        with pytest.raises(ProtocolLockedError):
            station.save()
        # Andere Felder der Station bleiben frei
        station.refresh_from_db()
        station.order = 2
        station.save()


# =============================================================================
# Schreibwege: Views, Admin, API
# =============================================================================


class TestSchreibwege:
    def test_protokoll_bearbeiten_nach_genehmigung_wirkungslos(self) -> None:
        w = welt()
        response = client(w.protokollant).post(
            f"{base(w)}/meetings/{w.sitzung.pk}/protocol/edit/",
            {"content": "X", f"protocol_note_{w.top.pk}": "X", f"vote_result_{w.top.pk}": "rejected"},
        )
        assert response.status_code == 302
        assert _frisch(w.top).vote_result == "approved"
        assert SessionProtocol.objects.get(pk=w.protokoll.pk).content == "Allgemeiner Teil der Sitzung."

    def test_abstimmung_erfassen_gesperrt(self) -> None:
        w = welt()
        c = client(w.protokollant)
        url = f"{base(w)}/agenda/{w.top.pk}/voting/"
        assert b"abstimmung-gesperrt" in c.get(url).content
        response = c.post(
            url,
            {"voting_method": "roll_call", "vote_result": "rejected", f"vote_{w.stimmberechtigt[0].pk}": "no"},
        )
        assert "genehmigt" in _meldungen(response)
        item = _frisch(w.top)
        assert (item.vote_result, item.voting_method) == ("approved", "summary")
        assert not SessionVote.objects.filter(agenda_item=item).exists()

    def test_top_loeschen_und_absetzen_verboten(self) -> None:
        w = welt()
        c = client(w.genehmiger)
        assert c.post(f"{base(w)}/agenda/{w.top.pk}/delete/").status_code == 403
        assert c.post(f"{base(w)}/agenda/{w.top.pk}/withdraw/", {"reason": "x"}).status_code == 403
        assert _frisch(w.top).is_withdrawn is False

    def test_admin_bietet_gesperrtes_nicht_an_und_modell_haelt_stand(self) -> None:
        w = welt()
        superuser = cast(Any, User.objects).create_superuser(email="root@example.org")
        request = RequestFactory().get("/admin/")
        request.user = superuser
        protocol_admin = admin.site._registry[SessionProtocol]
        protokoll = SessionProtocol.objects.get(pk=w.protokoll.pk)
        assert {"content", "status"} <= set(protocol_admin.get_readonly_fields(request, protokoll))
        assert protocol_admin.has_delete_permission(request, protokoll) is False
        meeting_admin = admin.site._registry[SessionMeeting]
        assert meeting_admin.has_delete_permission(request, w.sitzung) is False
        inline = meeting_admin.get_inline_instances(request, w.sitzung)[0]
        assert "paper" in inline.get_readonly_fields(request, w.sitzung)
        assert inline.has_delete_permission(request, w.sitzung) is False
        station_admin = admin.site._registry[SessionConsultation]
        vorlage = SessionPaper.objects.create(tenant=w.tenant, name="Radweg", status="approved")
        station = SessionConsultation(paper=vorlage, organization=w.gremium, agenda_item=w.top, result="approved")
        station.save()
        assert "result" in station_admin.get_readonly_fields(request, station)
        # Sammel-Löschen überspringt die gesperrte Sitzung
        meeting_admin.delete_queryset(request, SessionMeeting.objects.filter(pk=w.sitzung.pk))
        assert SessionMeeting.objects.filter(pk=w.sitzung.pk).exists()
        # Rückfallebene: Auch ein Admin-Speichern am Formular vorbei scheitert im Modell
        protokoll.content = "Admin-Änderung"
        with pytest.raises(ProtocolLockedError):
            protocol_admin.save_model(request, protokoll, None, True)

    def test_api_v1_hat_keinen_schreibweg_fuer_ergebnisse(self) -> None:
        schema = Client().get("/api/v1/session/openapi.json").json()
        schreibend = [
            (path, method)
            for path, operations in schema["paths"].items()
            for method in operations
            if method.lower() in ("post", "put", "patch", "delete")
        ]
        assert schreibend, "Einreichung von Anträgen bleibt der einzige Schreibweg"
        assert all("applications" in path for path, _ in schreibend)

    def test_oparl_api_ist_rein_lesend(self) -> None:
        w = welt()
        response = Client().post(f"/session/{w.tenant.slug}/api/oparl/agendaitem/{w.top.pk}/")
        assert response.status_code == 405
        assert _frisch(w.top).vote_result == "approved"


# =============================================================================
# Stimmrecht
# =============================================================================


class TestStimmrecht:
    def test_stimmen_nur_von_stimmberechtigten_anwesenden(self) -> None:
        w = welt(status="review")
        item = _frisch(w.top)
        item.voting_method = "roll_call"
        item.save()
        for person in (w.beratend, w.gast, w.abwesend):
            with pytest.raises(voting_service.VotingRightsError):
                voting_service.capture_votes(item, {person: "yes"}, recorded_by=w.protokollant)
        with pytest.raises(voting_service.VotingRightsError):
            voting_service.capture_votes(item, {w.beratend: "excluded"}, recorded_by=w.protokollant)
        assert not SessionVote.objects.filter(agenda_item=item).exists()
        voting_service.capture_votes(item, {w.stimmberechtigt[0]: "yes"}, recorded_by=w.protokollant)
        assert SessionVote.objects.filter(agenda_item=item).count() == 1

    def test_alte_stimme_ohne_stimmrecht_laesst_sich_entfernen(self) -> None:
        w = welt(status="review")
        item = _frisch(w.top)
        SessionVote.objects.bulk_create([SessionVote(agenda_item=item, person=w.beratend, vote="yes")])
        voting_service.capture_votes(item, {w.beratend: ""}, recorded_by=w.protokollant)
        assert not SessionVote.objects.filter(agenda_item=item).exists()

    def test_verspaetete_und_vorzeitig_gegangene_stimmen_mit(self) -> None:
        w = welt(status="review")
        SessionAttendance.objects.filter(meeting=w.sitzung, person=w.stimmberechtigt[0]).update(status="left_early")
        SessionAttendance.objects.filter(meeting=w.sitzung, person=w.stimmberechtigt[1]).update(status="joined_late")
        ids = voting_service.eligibility(w.sitzung).voting_person_ids
        assert {w.stimmberechtigt[0].pk, w.stimmberechtigt[1].pk} <= ids
        assert not {w.beratend.pk, w.gast.pk, w.abwesend.pk} & ids

    def test_zaehler_hart_bei_vollstaendiger_anwesenheit(self) -> None:
        w = welt(status="review")
        assessed = voting_service.eligibility(w.sitzung)
        assert assessed.complete
        assert not voting_service.check_counts(w.top, 2, 1, 0, assessed=assessed).exceeded
        check = voting_service.check_counts(w.top, 3, 1, 0, assessed=assessed)
        assert check.exceeded and check.hard and "(3)" in check.message

    def test_befangene_zaehlen_nicht(self) -> None:
        w = welt(status="review")
        voting_service.capture_votes(_frisch(w.top), {w.stimmberechtigt[0]: "excluded"}, recorded_by=w.protokollant)
        assert voting_service.check_counts(w.top, 2, 1, 0).hard
        assert not voting_service.check_counts(w.top, 1, 1, 0).exceeded

    def test_warnung_bei_unvollstaendiger_anwesenheit(self) -> None:
        w = welt(status="review")
        SessionAttendance.objects.filter(meeting=w.sitzung, person=w.abwesend).update(status="invited")
        check = voting_service.check_counts(w.top, 4, 0, 0)
        assert check.exceeded and not check.hard and "nicht vollständig" in check.message
        SessionAttendance.objects.filter(meeting=w.sitzung, person=w.abwesend).delete()
        assert not voting_service.check_counts(w.top, 4, 0, 0).hard
        SessionAttendance.objects.filter(meeting=w.sitzung).delete()
        assert not voting_service.check_counts(w.top, 40, 0, 0).exceeded, "ohne Anwesenheitsliste keine Prüfung"

    def test_erfassung_weist_ohne_stimmrecht_ab_und_zeigt_beratende(self) -> None:
        w = welt(status="review")
        c = client(w.protokollant)
        url = f"{base(w)}/agenda/{w.top.pk}/voting/"
        seite = c.get(url).content.decode()
        assert "ohne-stimmrecht" in seite and "Dachs" in seite and "Esche" in seite
        assert f'name="vote_{w.beratend.pk}" value="yes"' not in seite
        response = c.post(
            url,
            {
                "voting_method": "roll_call",
                "vote_result": "approved",
                f"vote_{w.stimmberechtigt[0].pk}": "yes",
                f"vote_{w.beratend.pk}": "yes",
            },
        )
        assert "stimmberechtigten Anwesenden" in _meldungen(response)
        assert not SessionVote.objects.filter(agenda_item=w.top).exists()
        assert _frisch(w.top).voting_method == "summary", "alles oder nichts"

    def test_geheime_summen_ueber_der_zahl_der_stimmberechtigten(self) -> None:
        w = welt(status="review")
        c = client(w.protokollant)
        url = f"{base(w)}/agenda/{w.top.pk}/voting/"
        response = c.post(url, {"voting_method": "secret", "votes_yes": "5", "votes_no": "2", "votes_abstain": "1"})
        assert "übersteigen" in _meldungen(response)
        assert (_frisch(w.top).votes_yes, _frisch(w.top).voting_method) == (2, "summary")
        c.post(url, {"voting_method": "secret", "votes_yes": "2", "votes_no": "0", "votes_abstain": "1"})
        assert (_frisch(w.top).votes_yes, _frisch(w.top).votes_abstain) == (2, 1)

    def test_protokollformular_prueft_zaehler(self) -> None:
        w = welt(status="draft")
        response = client(w.protokollant).post(
            f"{base(w)}/meetings/{w.sitzung.pk}/protocol/edit/",
            {
                "content": "Neu",
                f"protocol_note_{w.top.pk}": "Neu",
                f"vote_result_{w.top.pk}": "approved",
                f"votes_yes_{w.top.pk}": "9",
                f"votes_no_{w.top.pk}": "0",
                f"votes_abstain_{w.top.pk}": "0",
            },
        )
        assert "übersteigen" in _meldungen(response)
        item = _frisch(w.top)
        assert item.votes_yes == 2 and item.protocol_note == "Neu"


# =============================================================================
# Genehmigungsweg
# =============================================================================


class TestGenehmigung:
    def test_genehmigung_mit_top_der_folgesitzung(self) -> None:
        w = welt(status="review")
        c = client(w.genehmiger)
        seite = c.get(f"{base(w)}/meetings/{w.sitzung.pk}/protocol/").content.decode()
        assert "genehmigen-formular" in seite and "Genehmigung der Niederschrift" in seite
        c.post(f"{base(w)}/meetings/{w.sitzung.pk}/protocol/approve/", {"approval_item": str(w.folge_top.pk)})
        protokoll = SessionProtocol.objects.get(pk=w.protokoll.pk)
        assert protokoll.status == "approved"
        assert protokoll.approval_agenda_item == w.folge_top and protokoll.approval_meeting == w.folge
        assert "unter TOP 1" in protokoll.approval_note

    def test_noe_top_der_folgesitzung_ohne_nummer_im_vermerk(self) -> None:
        w = welt(status="review")
        noe_top = SessionAgendaItem.objects.create(
            meeting=w.folge, number="N1", order=5, name="Genehmigung der Niederschrift (NÖ)", is_public=False
        )
        client(w.genehmiger).post(
            f"{base(w)}/meetings/{w.sitzung.pk}/protocol/approve/", {"approval_item": str(noe_top.pk)}
        )
        protokoll = SessionProtocol.objects.get(pk=w.protokoll.pk)
        assert protokoll.approval_agenda_item == noe_top
        assert "N1" not in protokoll.approval_note and protokoll.approval_note.startswith("Genehmigt in der Sitzung")
        # Ohne NÖ-Recht wird der NÖ-TOP gar nicht angeboten
        assert noe_top not in protocol_service.approval_candidates(w.sitzung, include_non_public=False)
        assert noe_top in protocol_service.approval_candidates(w.sitzung, include_non_public=True)

    def test_fremder_top_wird_nicht_verknuepft(self) -> None:
        w = welt(status="review")
        anderes = SessionOrganization.objects.create(tenant=w.tenant, name="Rat")
        fremd = SessionMeeting.objects.create(
            tenant=w.tenant, name="Rat", organization=anderes, start=timezone.now() + timedelta(days=3)
        )
        fremd_top = SessionAgendaItem.objects.create(meeting=fremd, name="Genehmigung der Niederschrift")
        client(w.genehmiger).post(
            f"{base(w)}/meetings/{w.sitzung.pk}/protocol/approve/", {"approval_item": str(fremd_top.pk)}
        )
        protokoll = SessionProtocol.objects.get(pk=w.protokoll.pk)
        assert protokoll.status == "approved" and protokoll.approval_agenda_item is None

    def test_direkte_veroeffentlichung_ohne_genehmigung(self, settings: Any, tmp_path: Any) -> None:
        settings.MEDIA_ROOT = str(tmp_path)
        w = welt(status="review", protocol_approval_mode="direct")
        protokoll = SessionProtocol.objects.get(pk=w.protokoll.pk)
        with pytest.raises(protocol_service.WorkflowError):
            protocol_service.perform_action(protokoll, "approve", user=w.genehmiger, data={})
        message = protocol_service.perform_action(protokoll, "publish", user=w.genehmiger, data={})
        protokoll.refresh_from_db()
        assert protokoll.status == "published" and protokoll.approved_by == w.genehmiger
        assert protokoll.public_file is not None and "OParl" in message
        assert protocol_lock.is_locked(w.sitzung.pk)

    def test_direkte_veroeffentlichung_mit_vier_augen(self, settings: Any, tmp_path: Any) -> None:
        settings.MEDIA_ROOT = str(tmp_path)
        w = welt(status="review", four_eyes=True, protocol_approval_mode="direct")
        SessionProtocol.objects.filter(pk=w.protokoll.pk).update(created_by=w.genehmiger)
        protokoll = SessionProtocol.objects.get(pk=w.protokoll.pk)
        with pytest.raises(protocol_service.WorkflowError, match="Vier-Augen"):
            protocol_service.perform_action(protokoll, "publish", user=w.genehmiger, data={})
        protocol_service.perform_action(protokoll, "publish", user=w.zweite, data={})
        assert SessionProtocol.objects.get(pk=w.protokoll.pk).status == "published"

    def test_genehmigungsweg_einstellen(self) -> None:
        w = welt()
        verwaltung = nutzer(w.tenant, "verwaltung", "manage_settings")
        c = client(verwaltung)
        assert b"genehmigungsweg-einstellung" in c.get(f"{base(w)}/settings/four-eyes/").content
        c.post(f"{base(w)}/settings/protocol-approval/", {"protocol_approval_mode": "direct"})
        w.tenant.refresh_from_db()
        assert w.tenant.protocol_direct_publication
        eintrag = SessionAuditLog.objects.filter(object_id=w.tenant.pk, action="update").latest("created_at")
        assert eintrag.changes["protocol_approval_mode"] == {"alt": "follow_up", "neu": "direct"}
        assert client(w.leser).post(f"{base(w)}/settings/protocol-approval/", {}).status_code == 403

    def test_ruecknahme_ist_keine_genehmigung(self, settings: Any, tmp_path: Any) -> None:
        settings.MEDIA_ROOT = str(tmp_path)
        w = welt()
        c = client(w.genehmiger)
        c.post(f"{base(w)}/meetings/{w.sitzung.pk}/protocol/publish/")
        c.post(f"{base(w)}/meetings/{w.sitzung.pk}/protocol/unpublish/")
        protokoll = SessionProtocol.objects.get(pk=w.protokoll.pk)
        assert protokoll.status == "approved" and protokoll.public_file is None
        aktionen = list(
            SessionAuditLog.objects.filter(object_id=protokoll.pk).order_by("seq").values_list("action", flat=True)
        )
        assert aktionen[-1] == "unpublish" and "publish" in aktionen


# =============================================================================
# Berichtigung
# =============================================================================


def _berichtigen(w: Welt, user: Any = None, *, item: SessionAgendaItem | None = None, **data: str) -> Any:
    reason = data.pop("reason", "Zählfehler in der Niederschrift")
    target = data.pop("target", SessionProtocolCorrection.TARGET_ITEM)
    return korrektur.propose(
        SessionProtocol.objects.get(pk=w.protokoll.pk),
        target=target,
        item=_frisch(item or w.top) if target == SessionProtocolCorrection.TARGET_ITEM else None,
        data=data,
        reason=reason,
        user=user or w.genehmiger,
        can_view_np=True,
    )


class TestBerichtigung:
    def test_nur_nach_genehmigung_mit_grund_und_aenderung(self) -> None:
        with pytest.raises(korrektur.CorrectionError, match="erst nach der Genehmigung"):
            _berichtigen(welt("sued", status="review"), vote_result="rejected")
        w = welt()
        with pytest.raises(korrektur.CorrectionError, match="Grund"):
            _berichtigen(w, vote_result="rejected", reason="  ")
        with pytest.raises(korrektur.CorrectionError, match="keinen Wert"):
            _berichtigen(w, vote_result="approved")
        with pytest.raises(four_eyes_service.ApprovalError):
            _berichtigen(w, w.protokollant, vote_result="rejected")

    def test_sofort_wirksam_und_protokolliert(self) -> None:
        w = welt()
        outcome = _berichtigen(w, vote_result="rejected", votes_yes="1", votes_no="2")
        assert outcome.applied
        item = _frisch(w.top)
        assert (item.vote_result, item.votes_yes, item.votes_no) == ("rejected", 1, 2)
        berichtigung = outcome.correction
        assert berichtigung.status == "applied" and berichtigung.is_public
        assert berichtigung.reason == "Zählfehler in der Niederschrift"
        assert {"feld": "Ergebnis", "alt": "Angenommen", "neu": "Abgelehnt"} in berichtigung.changes
        eintrag = SessionAuditLog.objects.get(object_id=w.protokoll.pk, action="protocol_correction")
        assert eintrag.changes["grund"] == "Zählfehler in der Niederschrift"
        assert eintrag.changes["gegenstand"] == "TOP 1"
        notiz = protocol_service.correction_notes(berichtigung.protocol, internal=False, visible_item_ids={w.top.pk})[0]
        assert (
            notiz["reason"] == "Zählfehler in der Niederschrift"
            and "Ergebnis: Angenommen → Abgelehnt" in notiz["changes"]
        )

    def test_zaehler_einer_berichtigung_werden_geprueft(self) -> None:
        w = welt()
        with pytest.raises(korrektur.CorrectionError, match="übersteigen"):
            _berichtigen(w, votes_yes="4")

    def test_vier_augen_bestaetigung_durch_zweite_person(self) -> None:
        w = welt(four_eyes=True)
        outcome = _berichtigen(w, vote_result="rejected")
        assert not outcome.applied and outcome.correction.status == "pending"
        assert _frisch(w.top).vote_result == "approved"
        with pytest.raises(four_eyes_service.ApprovalError, match="beantragt"):
            korrektur.confirm(outcome.correction, user=w.genehmiger)
        korrektur.confirm(outcome.correction, user=w.zweite)
        assert _frisch(w.top).vote_result == "rejected"
        berichtigung = SessionProtocolCorrection.objects.get(pk=outcome.correction.pk)
        assert berichtigung.status == "applied" and berichtigung.decided_by == w.zweite
        aktionen = set(SessionAuditLog.objects.filter(object_id=w.protokoll.pk).values_list("action", flat=True))
        assert {"protocol_correction_requested", "protocol_correction"} <= aktionen

    def test_ablehnen_und_veralteter_stand(self) -> None:
        w = welt(four_eyes=True)
        erste = _berichtigen(w, vote_result="rejected").correction
        zweite = _berichtigen(w, vote_result="deferred").correction
        korrektur.reject(zweite, user=w.genehmiger, note="doppelt")  # eigener Antrag darf zurückgezogen werden
        assert SessionProtocolCorrection.objects.get(pk=zweite.pk).status == "rejected"
        dritte = _berichtigen(w, vote_result="noted").correction
        korrektur.confirm(erste, user=w.zweite)
        with pytest.raises(korrektur.CorrectionError, match="Stand hat sich"):
            korrektur.confirm(dritte, user=w.zweite)
        assert _frisch(w.top).vote_result == "rejected"

    def test_noe_berichtigung_verschluesselt(self) -> None:
        w = welt()
        outcome = _berichtigen(
            w, item=w.top_noe, resolution_text="GEHEIMBESCHLUSS neu", reason="GEHEIMGRUND der Berichtigung"
        )
        berichtigung = outcome.correction
        assert not berichtigung.is_public and berichtigung.reason == ""
        assert cast(Any, berichtigung).get_reason_decrypted() == "GEHEIMGRUND der Berichtigung"
        assert _frisch(w.top_noe).resolution_text == "GEHEIMBESCHLUSS neu"
        for eintrag in SessionAuditLog.objects.filter(tenant=w.tenant):
            assert "GEHEIMGRUND" not in str(eintrag.changes)
        assert protocol_service.correction_notes(berichtigung.protocol, internal=False, visible_item_ids=set()) == []

    def test_nur_verschluesselte_felder_gelten_als_nichtoeffentlich(self) -> None:
        w = welt()
        outcome = _berichtigen(w, resolution_text_encrypted="Nachtrag intern", reason="interner Nachtrag")
        assert not outcome.correction.is_public
        assert cast(Any, _frisch(w.top)).get_resolution_text_decrypted() == "Nachtrag intern"

    def test_namentliche_stimmen_berichtigen(self) -> None:
        w = welt(status="review")
        _namentlich(w, {0: "yes", 1: "yes", 2: "no"})
        _genehmigen(w)
        with pytest.raises(korrektur.CorrectionError, match="stimmberechtigten"):
            _berichtigen(w, **cast(dict[str, Any], {f"vote_{w.beratend.pk}": "yes"}))
        with pytest.raises(korrektur.CorrectionError, match="Ungültige Stimme"):
            _berichtigen(w, **cast(dict[str, Any], {"vote_kaputt": "yes"}))
        outcome = _berichtigen(w, **cast(dict[str, Any], {f"vote_{w.stimmberechtigt[2].pk}": "yes"}))
        assert outcome.applied
        item = _frisch(w.top)
        assert (item.votes_yes, item.votes_no) == (3, 0)
        assert {"feld": "Stimme P Carl", "alt": "Nein", "neu": "Ja"} in outcome.correction.changes

    def test_allgemeiner_teil(self) -> None:
        w = welt()
        _berichtigen(w, target="general", content="Allgemeiner Teil, berichtigt.", reason="Tippfehler")
        assert SessionProtocol.objects.get(pk=w.protokoll.pk).content == "Allgemeiner Teil, berichtigt."
        outcome = _berichtigen(w, target="general_np", content_encrypted="GEHEIMALLGEMEIN neu", reason="intern")
        assert not outcome.correction.is_public
        assert cast(Any, SessionProtocol.objects.get(pk=w.protokoll.pk)).get_content_decrypted() == (
            "GEHEIMALLGEMEIN neu"
        )

    def test_oberflaeche_recht_und_mandantentrennung(self) -> None:
        w = welt()
        url = f"{base(w)}/meetings/{w.sitzung.pk}/protocol/berichtigung/"
        assert client(w.leser).get(f"{url}?top={w.top.pk}").status_code == 403
        c = client(w.genehmiger)
        assert b"berichtigung-formular" in c.get(f"{url}?top={w.top.pk}").content
        response = c.post(url, {"top": str(w.top.pk), "vote_result": "rejected", "reason": "Zählfehler"})
        assert response.status_code == 302 and "wirksam" in _meldungen(response)
        seite = c.get(f"{base(w)}/meetings/{w.sitzung.pk}/protocol/").content.decode()
        assert "Berichtigung vom" in seite and "Zählfehler" in seite
        # Fremder Mandant: weder Sitzung noch Berichtigung erreichbar
        fremd = welt("sued")
        fremd_client = client(fremd.genehmiger)
        assert (
            fremd_client.get(f"{base(fremd)}/meetings/{w.sitzung.pk}/protocol/berichtigung/?top={w.top.pk}").status_code
            == 404
        )
        berichtigung = SessionProtocolCorrection.objects.get(protocol=w.protokoll)
        assert (
            fremd_client.post(
                f"{base(fremd)}/meetings/{fremd.sitzung.pk}/protocol/berichtigung/{berichtigung.pk}/reject/"
            ).status_code
            == 404
        )
        # Leser sieht die öffentliche Berichtigung, aber keine NÖ-Berichtigung
        _berichtigen(w, item=w.top_noe, protocol_note="GEHEIMNOTIZ neu", reason="GEHEIMGRUND")
        seite = client(w.leser).get(f"{base(w)}/meetings/{w.sitzung.pk}/protocol/").content.decode()
        assert "Zählfehler" in seite and "GEHEIMGRUND" not in seite
