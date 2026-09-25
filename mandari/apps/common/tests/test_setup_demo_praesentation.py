# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Präsentationsumgebung „Demo-Drehbuch“ (setup_demo_praesentation).

Eine Drucksache nimmt ihren Weg von der Fraktion über den Sitzungsdienst bis ins Bürgerportal:
Der Command legt dafür auf der Basisdemo einen zweiten Mandanten, die Leitstelle, die Verbindung
Work ↔ Session und die Drehbuch-Daten an und spiegelt Mandant A ins Bürgerportal. Geprüft wird,
dass wiederholte Läufe nichts verdoppeln, die Nummernkreise je Mandant zählen, der Spiegel stimmt,
ein Ö→NÖ-Wechsel sofort im Bürgerportal ankommt und --reset nur die Präsentation entfernt.
"""

from __future__ import annotations

import re
from io import StringIO
from pathlib import Path
from typing import Any, cast

import pytest
from django.core.management import call_command
from django.db.models import Model
from django.test import Client

from apps.accounts.models import User
from apps.common.management.commands import setup_demo_praesentation as drehbuch
from apps.common.management.commands.setup_demo_environment import DEMO_ORG_SLUG, DEMO_SESSION_SLUG, DEMO_USERS
from apps.session.models import (
    SessionAgendaItem,
    SessionAllowance,
    SessionAPIToken,
    SessionApplication,
    SessionAttendance,
    SessionConsultation,
    SessionLegislativeTerm,
    SessionMeeting,
    SessionPaper,
    SessionProtocol,
    SessionTenant,
    SessionTenantGroup,
    SessionTenantGroupMembership,
    SessionTenantGroupTenant,
    SessionUser,
    SessionVote,
)
from apps.session.services import allowance_service, invitation_service
from apps.tenants.models import Membership
from apps.work.motions import ris_submission
from apps.work.motions.models import AdministrationConnection, Motion
from insight_core.models import OParlAgendaItem, OParlBody, OParlMeeting, OParlSource

pytestmark = pytest.mark.django_db

ZAEHLMODELLE: tuple[type[Model], ...] = (
    SessionTenant,
    SessionUser,
    SessionPaper,
    SessionMeeting,
    SessionAgendaItem,
    SessionConsultation,
    SessionAttendance,
    SessionAllowance,
    SessionVote,
    SessionProtocol,
    SessionApplication,
    SessionLegislativeTerm,
    SessionAPIToken,
    SessionTenantGroup,
    SessionTenantGroupMembership,
    SessionTenantGroupTenant,
    AdministrationConnection,
    Motion,
    User,
    OParlSource,
    OParlBody,
    OParlMeeting,
    OParlAgendaItem,
)


@pytest.fixture(autouse=True)
def _umgebung(settings: Any, tmp_path: Path) -> None:
    # Die Basisdemo schreibt PDF-Dateien ins Media-Verzeichnis; der Spiegel ruft die OParl-API mehrfach ab
    settings.MEDIA_ROOT = str(tmp_path / "media")
    settings.OPARL_API_RATE_LIMIT = 0


def ausfuehren(*args: str) -> str:
    ausgabe = StringIO()
    call_command("setup_demo_praesentation", *args, stdout=ausgabe)
    return ausgabe.getvalue()


def mandant(slug: str = DEMO_SESSION_SLUG) -> SessionTenant:
    return SessionTenant.objects.get(slug=slug)


def top_jugendzentrum() -> SessionAgendaItem:
    return SessionAgendaItem.objects.get(
        meeting__tenant__slug=DEMO_SESSION_SLUG,
        meeting__name=drehbuch.SITZUNG_KOMMEND,
        name=drehbuch.TOP_JUGENDZENTRUM,
    )


def gespiegelt(item: SessionAgendaItem) -> OParlAgendaItem:
    return OParlAgendaItem.objects.get(
        external_id__endswith=f"/session/{DEMO_SESSION_SLUG}/api/oparl/agendaitem/{item.id}/"
    )


def zaehlstand() -> dict[str, int]:
    return {modell.__name__: modell._default_manager.count() for modell in ZAEHLMODELLE}


def drehbuch_nummern(slug: str, vorlagen: tuple[drehbuch.Vorlage, ...]) -> list[str]:
    return sorted(
        str(ref)
        for ref in SessionPaper.objects.filter(tenant__slug=slug, name__in=[v.name for v in vorlagen]).values_list(
            "reference", flat=True
        )
    )


def vorsitz() -> Membership:
    return Membership.objects.get(organization__slug=DEMO_ORG_SLUG, user__email=DEMO_USERS["vorsitz"]["email"])


class TestIdempotenz:
    def test_zweiter_lauf_verdoppelt_nichts(self) -> None:
        ausfuehren("--profil", "hamburg")
        vorher = zaehlstand()
        nummern = drehbuch_nummern(DEMO_SESSION_SLUG, drehbuch.VORLAGEN_A)
        beschluss = SessionAgendaItem.objects.get(name=drehbuch.BESCHLUSS_LASTENRAD)

        ausfuehren("--profil", "hamburg")

        assert zaehlstand() == vorher
        assert drehbuch_nummern(DEMO_SESSION_SLUG, drehbuch.VORLAGEN_A) == nummern
        beschluss_neu = SessionAgendaItem.objects.get(name=drehbuch.BESCHLUSS_LASTENRAD)
        assert beschluss_neu.resolution_number == beschluss.resolution_number != ""

    def test_basisdemo_wird_nur_bei_fehlendem_mandanten_angelegt(self) -> None:
        erste = ausfuehren()
        assert "Demo-Mandant fehlt" in erste
        passwort_hash = User.objects.get(email=DEMO_USERS["verwaltung"]["email"]).password
        zweite = ausfuehren()
        assert "Demo-Mandant fehlt" not in zweite
        # Passwörter der Basisdemo rotieren nicht unnötig
        assert User.objects.get(email=DEMO_USERS["verwaltung"]["email"]).password == passwort_hash


class TestNummernkreise:
    def test_hamburg_zaehlt_je_mandant(self) -> None:
        ausfuehren("--profil", "hamburg")
        a = drehbuch_nummern(DEMO_SESSION_SLUG, drehbuch.VORLAGEN_A)
        b = drehbuch_nummern(drehbuch.MANDANT_B_SLUG, drehbuch.VORLAGEN_B)
        assert a == [f"22-{n:04d}" for n in range(1, len(drehbuch.VORLAGEN_A) + 1)]
        assert b == ["22-0001", "22-0002"]
        assert mandant().reference_label == mandant(drehbuch.MANDANT_B_SLUG).reference_label == "Drucksache"
        # Die Nummern der Basisdemo bleiben unberührt
        assert SessionPaper.objects.filter(tenant__slug=DEMO_SESSION_SLUG, reference="SV/2026/D-001").exists()

    def test_nrw_ist_standard(self) -> None:
        ausfuehren()
        for nummer in drehbuch_nummern(DEMO_SESSION_SLUG, drehbuch.VORLAGEN_A):
            assert re.fullmatch(r"\d{4}/\d{4}", nummer), nummer
        assert mandant().reference_label == "Vorlagen-Nr."
        assert SessionLegislativeTerm.objects.filter(tenant=mandant(), number=1).count() == 1

    def test_profilwechsel_ersetzt_die_wahlperiode(self) -> None:
        ausfuehren("--profil", "hamburg")
        ausgabe = ausfuehren("--profil", "nrw")
        namen = set(SessionLegislativeTerm.objects.filter(tenant=mandant()).values_list("name", flat=True))
        assert namen == {"Wahlperiode 2025–2030"}
        assert "Nummernkreis gewechselt" in ausgabe


class TestLeitstelleUndVerbindung:
    def test_leitstelle_ist_administrator_in_beiden_mandanten(self) -> None:
        ausgabe = ausfuehren()
        email = drehbuch.LEITSTELLE["email"]
        treffer = re.search(rf"{re.escape(email)}\s+->\s+(\S+)", ausgabe)
        assert treffer is not None, "Passwort der neu angelegten Leitstelle fehlt in der Ausgabe"
        nutzer = User.objects.get(email=email)
        assert nutzer.check_password(treffer.group(1))
        for slug in (DEMO_SESSION_SLUG, drehbuch.MANDANT_B_SLUG):
            zugang = SessionUser.objects.get(user=nutzer, tenant__slug=slug)
            assert zugang.is_active and zugang.is_admin()

        # Zweiter Lauf: kein neues Passwort, das alte gilt weiter
        zweite = ausfuehren()
        assert f"{email}  ->" not in zweite
        nutzer.refresh_from_db()
        assert nutzer.check_password(treffer.group(1))

    def test_fraktion_ist_mit_mandant_a_verbunden(self) -> None:
        ausfuehren("--profil", "hamburg")
        verbindung = AdministrationConnection.objects.get(organization__slug=DEMO_ORG_SLUG)
        assert verbindung.tenant.slug == DEMO_SESSION_SLUG
        assert ris_submission.connection_state(verbindung) == (True, "")
        token = cast(Any, verbindung).get_token()
        assert token.name == drehbuch.TOKEN_NAME and token.can_submit_applications

        # Zweiter Lauf verwendet die Verbindung weiter
        ausfuehren("--profil", "hamburg")
        assert AdministrationConnection.objects.get(organization__slug=DEMO_ORG_SLUG).pk == verbindung.pk
        assert SessionAPIToken.objects.filter(name=drehbuch.TOKEN_NAME).count() == 1

    def test_antrag_laesst_sich_einreichen(self) -> None:
        ausfuehren("--profil", "hamburg")
        antrag = Motion.objects.get(organization__slug=DEMO_ORG_SLUG, title=drehbuch.ANTRAG_TITEL)
        assert antrag.status == "draft"
        assert ris_submission.can_submit(antrag, vorsitz()) == (True, "")
        daten = ris_submission.build_prefill(antrag)
        # Abschnitte aus dem Dokument: Das Formular „Bei Verwaltung einreichen“ ist vollständig vorbelegt
        assert daten["structured"]
        assert daten["resolution_proposal"].startswith("Die Verwaltung wird beauftragt")
        assert daten["justification"].startswith("Der Musterweg ist der Hauptzugang")
        assert "4.500 Euro" in daten["financial_impact"]
        eingang = ris_submission.submit_motion(antrag, vorsitz(), daten)
        assert eingang.tenant.slug == DEMO_SESSION_SLUG
        assert re.fullmatch(r"A/\d{4}/\d{4}", eingang.reference)


class TestDrehbuchDaten:
    def test_sitzungen_beratungsfolge_und_beschlusskontrolle(self) -> None:
        ausfuehren("--profil", "hamburg")
        kommend = SessionMeeting.objects.get(tenant__slug=DEMO_SESSION_SLUG, name=drehbuch.SITZUNG_KOMMEND)
        assert kommend.meeting_state == "invitation_sent" and kommend.invitation_sent_at is not None
        assert kommend.invitation_sent_at.date() <= kommend.invitation_deadline
        tops = {t.name: t for t in kommend.agenda_items.all()}
        assert [tops[n].number for n in ("Eröffnung", drehbuch.TOP_JUGENDZENTRUM, drehbuch.TOP_SPIELPLATZ)] == [
            "1",
            "2",
            "3",
        ]
        grundstueck = tops[drehbuch.TOP_GRUNDSTUECK]
        assert grundstueck.number == "N1" and not grundstueck.is_public
        assert grundstueck.paper is not None and not grundstueck.paper.is_public

        folge = list(SessionConsultation.objects.filter(paper__name=drehbuch.TOP_JUGENDZENTRUM).order_by("order"))
        assert [(c.organization.name, c.role, c.result) for c in folge] == [
            (drehbuch.GREMIUM_BAU, "preliminary", "approved"),
            (drehbuch.GREMIUM_HA, "decision", "pending"),
        ]
        assert folge[1].agenda_item_id == tops[drehbuch.TOP_JUGENDZENTRUM].id

        vergangen = SessionMeeting.objects.get(tenant__slug=DEMO_SESSION_SLUG, name=drehbuch.SITZUNG_VERGANGEN)
        lastenrad = vergangen.agenda_items.get(name=drehbuch.BESCHLUSS_LASTENRAD)
        assert (lastenrad.votes_yes, lastenrad.votes_no, lastenrad.votes_abstain) == (3, 1, 1)
        assert lastenrad.resolution_number.startswith("B/")
        assert lastenrad.implementation_status == "in_progress" and not lastenrad.implementation_overdue
        haltestelle = vergangen.agenda_items.get(name=drehbuch.BESCHLUSS_HALTESTELLE)
        assert haltestelle.implementation_overdue
        protokoll = SessionProtocol.objects.get(meeting=vergangen)
        assert protokoll.status == "approved" and protokoll.content
        assert drehbuch.TOP_REINIGUNG in str(cast(Any, protokoll).get_content_decrypted())
        assert drehbuch.TOP_REINIGUNG not in protokoll.content

    def test_sitzungsgeld_im_vier_augen_prinzip(self) -> None:
        ausfuehren("--profil", "hamburg")
        positionen = list(
            SessionAllowance.objects.filter(attendance__meeting__name=drehbuch.SITZUNG_VERGANGEN).select_related(
                "created_by__user"
            )
        )
        # Fünf Anwesende, die entschuldigte Person bekommt nichts
        assert len(positionen) == 5
        assert {p.status for p in positionen} == {"pending"}
        assert {cast(Any, p.created_by).user.email for p in positionen} == {DEMO_USERS["verwaltung"]["email"]}

        verwaltung = SessionUser.objects.get(
            tenant__slug=DEMO_SESSION_SLUG, user__email=DEMO_USERS["verwaltung"]["email"]
        )
        assert allowance_service.approve_allowances(positionen, verwaltung) == {"approved": 0, "blocked_four_eyes": 5}
        leitstelle = SessionUser.objects.get(tenant__slug=DEMO_SESSION_SLUG, user__email=drehbuch.LEITSTELLE["email"])
        assert leitstelle.has_permission("manage_allowances")
        assert allowance_service.approve_allowances(positionen, leitstelle)["approved"] == 5

        # Ein erneuter Lauf setzt die Probe zurück
        ausfuehren("--profil", "hamburg")
        assert set(
            SessionAllowance.objects.filter(attendance__meeting__name=drehbuch.SITZUNG_VERGANGEN).values_list(
                "status", flat=True
            )
        ) == {"pending"}


class TestLeitstellenGruppeUndGemeinsameSitzung:
    """Profil hamburg: beide Bezirke als Mandantengruppe mit Leitstelle, gemeinsame Sitzung zweier Ausschüsse."""

    def test_hamburg_legt_gruppe_und_gemeinsame_sitzung_an(self) -> None:
        ausgabe = ausfuehren("--profil", "hamburg")
        gruppe = SessionTenantGroup.objects.get(slug=drehbuch.GRUPPE_SLUG)
        assert set(gruppe.tenants.values_list("slug", flat=True)) == {DEMO_SESSION_SLUG, drehbuch.MANDANT_B_SLUG}
        leitstelle = User.objects.get(email=drehbuch.LEITSTELLE["email"])
        assert gruppe.memberships.get(user=leitstelle).role == SessionTenantGroupMembership.ROLE_LEITSTELLE

        sitzung = SessionMeeting.objects.get(tenant__slug=DEMO_SESSION_SLUG, name=drehbuch.SITZUNG_GEMEINSAM)
        assert sitzung.organization.name == drehbuch.GREMIUM_HA
        assert [o.name for o in sitzung.joint_organizations.all()] == [drehbuch.GREMIUM_BAU]
        empfaenger = [r["person"].family_name for r in invitation_service.get_recipients(sitzung)]
        assert empfaenger.count("Heller") == 1  # sitzt in beiden Ausschüssen

        client = Client()
        client.force_login(leitstelle)
        seite = client.get(f"/session/leitstelle/{drehbuch.GRUPPE_SLUG}/")
        assert seite.status_code == 200
        assert drehbuch.VORLAGE_LEITSTELLE.name in seite.content.decode()
        assert f"/session/leitstelle/{drehbuch.GRUPPE_SLUG}/" in ausgabe
        assert "Gemeinsame Sitzung" in ausgabe

    def test_nrw_und_reset_entfernen_gruppe_und_gemeinsame_sitzung(self) -> None:
        ausfuehren("--profil", "hamburg")
        ausfuehren("--profil", "nrw")
        assert not SessionTenantGroup.objects.filter(slug=drehbuch.GRUPPE_SLUG).exists()
        assert not SessionMeeting.objects.filter(name=drehbuch.SITZUNG_GEMEINSAM).exists()
        assert not SessionPaper.objects.filter(name=drehbuch.VORLAGE_LEITSTELLE.name).exists()
        ausfuehren("--profil", "hamburg")
        ausfuehren("--reset")
        assert not SessionTenantGroup.objects.exists()
        assert not SessionTenantGroupTenant.objects.exists()


class TestSpiegel:
    def test_spiegel_enthaelt_den_jugendzentrum_top(self) -> None:
        ausfuehren("--profil", "hamburg")
        kommune = OParlBody.objects.get(source__url__contains=f"/session/{DEMO_SESSION_SLUG}/api/oparl/")
        assert kommune.is_listed is False
        spiegel = gespiegelt(top_jugendzentrum())
        assert spiegel.deleted is False and spiegel.name == drehbuch.TOP_JUGENDZENTRUM
        # NÖ-Beweis: der NÖ-TOP und seine Vorlage kommen nie im Bürgerportal an
        assert not OParlAgendaItem.objects.filter(name=drehbuch.TOP_GRUNDSTUECK).exists()
        assert not OParlAgendaItem.objects.filter(name=drehbuch.TOP_REINIGUNG).exists()

    def test_oe_zu_noe_nimmt_den_top_sofort_zurueck(self, django_capture_on_commit_callbacks: Any) -> None:
        ausfuehren("--profil", "hamburg")
        top = top_jugendzentrum()
        with django_capture_on_commit_callbacks(execute=True):
            top.is_public = False
            top.save()
        assert gespiegelt(top).deleted is True

    def test_erneuter_lauf_setzt_die_probe_zurueck(self, django_capture_on_commit_callbacks: Any) -> None:
        ausfuehren("--profil", "hamburg")
        antrag = Motion.objects.get(organization__slug=DEMO_ORG_SLUG, title=drehbuch.ANTRAG_TITEL)
        eingang = ris_submission.submit_motion(antrag, vorsitz(), ris_submission.build_prefill(antrag))
        top = top_jugendzentrum()
        with django_capture_on_commit_callbacks(execute=True):
            top.is_public = False
            top.save()

        with django_capture_on_commit_callbacks(execute=True):
            ausfuehren("--profil", "hamburg")

        antrag.refresh_from_db()
        assert antrag.status == "draft" and antrag.session_application_id is None
        assert not SessionApplication.objects.filter(pk=eingang.pk).exists()
        top.refresh_from_db()
        assert top.is_public and top.number == "2"
        assert gespiegelt(top).deleted is False


class TestReset:
    def test_reset_entfernt_nur_die_praesentation(self) -> None:
        ausfuehren("--profil", "hamburg")
        basis_papiere = set(
            SessionPaper.objects.filter(tenant__slug=DEMO_SESSION_SLUG, reference__startswith="SV/").values_list(
                "reference", flat=True
            )
        )

        ausgabe = ausfuehren("--reset")

        assert "Präsentationsumgebung entfernt" in ausgabe
        assert not SessionTenant.objects.filter(slug=drehbuch.MANDANT_B_SLUG).exists()
        assert not User.objects.filter(email=drehbuch.LEITSTELLE["email"]).exists()
        assert not Motion.objects.filter(title=drehbuch.ANTRAG_TITEL).exists()
        assert not AdministrationConnection.objects.exists()
        assert not SessionAPIToken.objects.filter(name=drehbuch.TOKEN_NAME).exists()
        assert not SessionMeeting.objects.filter(name__in=drehbuch.DREHBUCH_SITZUNGEN).exists()
        assert not SessionPaper.objects.filter(name__in=[v.name for v in drehbuch.VORLAGEN_A]).exists()
        assert not OParlSource.objects.filter(url__contains=f"/session/{DEMO_SESSION_SLUG}/").exists()
        a = mandant()
        assert not a.insight_publish and not a.implementation_publish
        assert a.reference_label == "Vorlagen-Nr." and not a.legislative_terms.exists()
        # Die Basisdemo bleibt
        assert User.objects.filter(email=DEMO_USERS["vorsitz"]["email"]).exists()
        assert (
            set(
                SessionPaper.objects.filter(tenant__slug=DEMO_SESSION_SLUG, reference__startswith="SV/").values_list(
                    "reference", flat=True
                )
            )
            == basis_papiere
        )
        assert a.organizations.get(name=drehbuch.GREMIUM_HA).memberships.count() == 3

    def test_reset_der_basisdemo_raeumt_die_praesentation_mit_auf(self) -> None:
        ausfuehren()
        call_command("setup_demo_environment", "--reset", stdout=StringIO())
        assert not SessionTenant.objects.filter(slug__in=[DEMO_SESSION_SLUG, drehbuch.MANDANT_B_SLUG]).exists()
        assert not OParlSource.objects.filter(url__contains=f"/session/{DEMO_SESSION_SLUG}/").exists()
        assert not User.objects.filter(email=drehbuch.LEITSTELLE["email"]).exists()
