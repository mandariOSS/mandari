# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Löschen im Admin und Kaskaden (Issue #427).

- Ein Gremium lässt sich im Admin nur löschen, solange es keine Sitzungen (federführend oder
  beteiligt), Beratungsstationen, Umlaufbeschlüsse oder Mitzeichnungen hat; die Bestätigungsseite
  nennt die Verwendungen und löscht nichts. Dasselbe gilt für Vorlagen auf einer Tagesordnung und
  für Wahlperioden mit Zuordnungen (wie im Sitzungsdienst).
- Die Sperre der genehmigten Niederschrift greift auch bei Kaskaden und ``QuerySet.delete()`` –
  nicht nur beim Einzel-Löschen der Sitzung. Das Entfernen eines ganzen Mandanten bleibt möglich.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import date, timedelta
from typing import Any, cast

import pytest
from django.contrib import admin
from django.db import transaction
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone

from apps.common.tests.factories import UserFactory
from apps.session.models import (
    SessionAgendaItem,
    SessionCircularResolution,
    SessionConsultation,
    SessionLegislativeTerm,
    SessionMeeting,
    SessionOrganization,
    SessionOrganizationMembership,
    SessionPaper,
    SessionProtocol,
    SessionTenant,
)
from apps.session.services import protocol_lock
from apps.session.services.protocol_lock import ProtocolLockedError
from apps.session.tests._niederschrift import Welt, person, welt

pytestmark = pytest.mark.django_db


@pytest.fixture
def superuser() -> Any:
    return cast(Any, UserFactory)(email="betrieb@example.org", is_staff=True, is_superuser=True)


@pytest.fixture
def admin_client(superuser: Any) -> Client:
    result = Client()
    result.force_login(superuser)
    return result


def _request(superuser: Any) -> Any:
    request = RequestFactory().get("/admin/")
    request.user = superuser
    return request


def _loeschen(client: Client, model: str, obj: Any) -> Any:
    """Löschbestätigung im Admin abschicken."""
    return client.post(reverse(f"admin:session_{model}_delete", args=[obj.pk]), {"post": "yes"})


def _sammel_loeschen(client: Client, model: str, *objs: Any) -> Any:
    """Sammel-Aktion „Ausgewählte löschen“ mit Bestätigung."""
    return client.post(
        reverse(f"admin:session_{model}_changelist"),
        {"action": "delete_selected", "_selected_action": [str(o.pk) for o in objs], "post": "yes"},
    )


def _abgelehnt() -> AbstractContextManager[pytest.ExceptionInfo[ProtocolLockedError]]:
    """
    Erwartete Ablehnung in einem Savepoint: Django löscht in ``atomic(savepoint=False)``; wer die
    Ablehnung abfängt und weiterarbeitet, braucht einen eigenen Savepoint.
    """
    return pytest.raises(ProtocolLockedError)


def _leeres_gremium(w: Welt, name: str = "Ältestenrat") -> SessionOrganization:
    return SessionOrganization.objects.create(tenant=w.tenant, name=name)


# =============================================================================
# Gremium im Admin
# =============================================================================


class TestGremiumImAdmin:
    def test_gremium_mit_sitzungen_bleibt_und_bestaetigung_nennt_sie(self, admin_client: Client) -> None:
        w = welt()
        seite = admin_client.get(reverse("admin:session_sessionorganization_delete", args=[w.gremium.pk]))
        inhalt = seite.content.decode()
        assert seite.status_code == 200
        assert "Sitzung: Bauausschuss: Folgesitzung" in inhalt
        assert "Deaktivieren Sie das Gremium" in inhalt

        antwort = _loeschen(admin_client, "sessionorganization", w.gremium)
        assert antwort.status_code == 200  # Bestätigungsseite erneut, nichts gelöscht
        assert SessionOrganization.objects.filter(pk=w.gremium.pk).exists()
        assert SessionMeeting.objects.filter(organization=w.gremium).count() == 2
        assert SessionProtocol.objects.filter(pk=w.protokoll.pk, status="approved").exists()

    def test_auch_ohne_genehmigte_niederschrift_nicht_loeschbar(self, admin_client: Client) -> None:
        w = welt(status="draft")
        _loeschen(admin_client, "sessionorganization", w.gremium)
        assert SessionOrganization.objects.filter(pk=w.gremium.pk).exists()
        assert SessionMeeting.objects.filter(pk=w.sitzung.pk).exists()

    def test_sammel_loeschen_loescht_nichts_aus_der_auswahl(self, admin_client: Client) -> None:
        w = welt()
        leer = _leeres_gremium(w)
        antwort = _sammel_loeschen(admin_client, "sessionorganization", w.gremium, leer)
        assert antwort.status_code == 200
        assert "Sitzung: Bauausschuss" in antwort.content.decode()
        assert SessionOrganization.objects.filter(pk__in=[w.gremium.pk, leer.pk]).count() == 2

    def test_beteiligtes_gremium_einer_gemeinsamen_sitzung(self, superuser: Any) -> None:
        w = welt()
        beteiligt = _leeres_gremium(w, "Hauptausschuss")
        w.sitzung.joint_organizations.add(beteiligt)
        gremium_admin = admin.site._registry[SessionOrganization]
        *_, protected = gremium_admin.get_deleted_objects([beteiligt], _request(superuser))
        assert any(zeile.startswith("Sitzung: Bauausschuss: Bauausschuss") for zeile in protected)

    def test_weitere_verwendungen_sperren(self, superuser: Any) -> None:
        w = welt()
        gremium_admin = admin.site._registry[SessionOrganization]
        station = _leeres_gremium(w, "Jugendhilfeausschuss")
        vorlage = SessionPaper.objects.create(tenant=w.tenant, name="Jugendzentrum", reference="V/1")
        SessionConsultation.objects.create(paper=vorlage, organization=station)
        umlauf = _leeres_gremium(w, "Beirat")
        SessionCircularResolution.objects.create(
            tenant=w.tenant, organization=umlauf, title="Umlauf", resolution_text="x", deadline=date.today()
        )
        request = _request(superuser)
        assert gremium_admin.get_deleted_objects([station], request)[3] == [
            "Beratungsstation: V/1 – Jugendhilfeausschuss"
        ]
        assert gremium_admin.get_deleted_objects([umlauf], request)[3] == ["Umlaufbeschluss: Umlauf: Umlauf"]

    def test_lange_liste_wird_gekuerzt(self, superuser: Any) -> None:
        w = welt()
        for tag in range(12):
            SessionMeeting.objects.create(
                tenant=w.tenant, name=f"S{tag}", organization=w.gremium, start=timezone.now() + timedelta(days=tag)
            )
        *_, protected = admin.site._registry[SessionOrganization].get_deleted_objects([w.gremium], _request(superuser))
        assert len(protected) == 11
        assert protected[-1] == "Sitzung: … und 4 weitere"

    def test_unbenutztes_gremium_laesst_sich_loeschen(self, admin_client: Client) -> None:
        w = welt()
        leer = _leeres_gremium(w)
        SessionOrganizationMembership.objects.create(organization=leer, person=person(w.tenant, "Gans"))
        antwort = _loeschen(admin_client, "sessionorganization", leer)
        assert antwort.status_code == 302
        assert not SessionOrganization.objects.filter(pk=leer.pk).exists()


# =============================================================================
# Sperre der Niederschrift bei Kaskaden und Sammel-Löschen
# =============================================================================


class TestSperreBeiKaskaden:
    def test_gremium_loeschen_im_code_scheitert_an_gesperrter_sitzung(self) -> None:
        w = welt()
        with _abgelehnt(), transaction.atomic():
            SessionOrganization.objects.get(pk=w.gremium.pk).delete()
        with _abgelehnt(), transaction.atomic():
            SessionOrganization.objects.filter(pk=w.gremium.pk).delete()
        # Die Löschung bricht vollständig ab: auch die ungesperrte Folgesitzung bleibt
        assert SessionMeeting.objects.filter(pk__in=[w.sitzung.pk, w.folge.pk]).count() == 2
        assert SessionAgendaItem.objects.filter(pk=w.top.pk).exists()
        assert SessionProtocol.objects.filter(pk=w.protokoll.pk).exists()

    def test_sammel_loeschen_von_sitzungen(self) -> None:
        w = welt()
        with _abgelehnt(), transaction.atomic():
            SessionMeeting.objects.filter(pk__in=[w.sitzung.pk, w.folge.pk]).delete()
        assert SessionMeeting.objects.filter(pk__in=[w.sitzung.pk, w.folge.pk]).count() == 2
        # Ungesperrte Sitzungen lassen sich weiterhin sammelweise löschen
        SessionMeeting.objects.filter(pk=w.folge.pk).delete()
        assert not SessionMeeting.objects.filter(pk=w.folge.pk).exists()

    def test_ohne_genehmigte_niederschrift_keine_sperre(self) -> None:
        w = welt(status="draft")
        SessionOrganization.objects.filter(pk=w.gremium.pk).delete()
        assert not SessionMeeting.objects.filter(pk=w.sitzung.pk).exists()

    def test_erlaubte_datenpflege(self) -> None:
        w = welt()
        with protocol_lock.permit(w.sitzung.pk):
            SessionMeeting.objects.filter(pk=w.sitzung.pk).delete()
        assert not SessionMeeting.objects.filter(pk=w.sitzung.pk).exists()

    def test_mandant_loeschen_bleibt_moeglich(self) -> None:
        w = welt()
        SessionTenant.objects.get(pk=w.tenant.pk).delete()
        assert not SessionMeeting.objects.filter(pk=w.sitzung.pk).exists()
        andere = welt("sued")
        SessionTenant.objects.filter(pk=andere.tenant.pk).delete()
        assert not SessionTenant.objects.filter(pk=andere.tenant.pk).exists()


# =============================================================================
# Vorlage und Wahlperiode im Admin
# =============================================================================


class TestVorlageUndWahlperiode:
    def _vorlage_auf_gesperrtem_top(self, w: Welt) -> SessionPaper:
        vorlage = SessionPaper.objects.create(tenant=w.tenant, name="Radweg", reference="V/7")
        with protocol_lock.permit(w.sitzung.pk):
            item = SessionAgendaItem.objects.select_related("meeting__tenant").get(pk=w.top.pk)
            item.paper = vorlage
            item.save()
        return vorlage

    def test_vorlage_auf_gesperrtem_top_bleibt(self, admin_client: Client) -> None:
        w = welt()
        vorlage = self._vorlage_auf_gesperrtem_top(w)
        antwort = _loeschen(admin_client, "sessionpaper", vorlage)
        assert antwort.status_code == 200
        assert "Tagesordnungspunkt: TOP 1 – Bauausschuss: Bauausschuss" in antwort.content.decode()
        assert SessionPaper.objects.filter(pk=vorlage.pk).exists()
        # Auch am Admin vorbei: Die Zuordnung am gesperrten TOP bleibt
        with _abgelehnt(), transaction.atomic():
            SessionPaper.objects.filter(pk=vorlage.pk).delete()
        assert SessionAgendaItem.objects.get(pk=w.top.pk).paper_id == vorlage.pk

    def test_vorlage_auf_offenem_top_nur_im_code_loeschbar(self, admin_client: Client) -> None:
        w = welt()
        vorlage = SessionPaper.objects.create(tenant=w.tenant, name="Spielplatz", reference="V/8")
        SessionAgendaItem.objects.create(meeting=w.folge, number="2", order=2, name="Spielplatz", paper=vorlage)
        _loeschen(admin_client, "sessionpaper", vorlage)
        assert SessionPaper.objects.filter(pk=vorlage.pk).exists()
        vorlage.delete()  # ungesperrte Sitzung: Modell lässt es zu

    def test_vorlage_ohne_tagesordnung_laesst_sich_loeschen(self, admin_client: Client) -> None:
        w = welt()
        vorlage = SessionPaper.objects.create(tenant=w.tenant, name="Entwurf", reference="V/9")
        assert _loeschen(admin_client, "sessionpaper", vorlage).status_code == 302
        assert not SessionPaper.objects.filter(pk=vorlage.pk).exists()

    def test_wahlperiode_wie_im_sitzungsdienst(self, admin_client: Client) -> None:
        w = welt()
        periode = SessionLegislativeTerm.objects.create(tenant=w.tenant, name="2020–2025", start_date=date(2020, 11, 1))
        SessionMeeting.objects.filter(pk=w.sitzung.pk).update(legislative_term=periode)
        SessionOrganizationMembership.objects.filter(organization=w.gremium).update(legislative_term=periode)
        antwort = _loeschen(admin_client, "sessionlegislativeterm", periode)
        inhalt = antwort.content.decode()
        assert antwort.status_code == 200
        assert "Besetzungen: 5" in inhalt
        assert "Amsel" not in inhalt  # Besetzungen nur als Anzahl
        assert SessionLegislativeTerm.objects.filter(pk=periode.pk).exists()

        leer = SessionLegislativeTerm.objects.create(tenant=w.tenant, name="2031–2036", start_date=date(2031, 11, 1))
        assert _loeschen(admin_client, "sessionlegislativeterm", leer).status_code == 302
        assert not SessionLegislativeTerm.objects.filter(pk=leer.pk).exists()
