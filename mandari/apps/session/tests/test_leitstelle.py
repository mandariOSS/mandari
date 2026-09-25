# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Mandantengruppe mit Leitstelle (Issue #317, Teil B).

Die Leitstelle sieht die Arbeitsvorräte aller Mandanten der Gruppe auf einer Seite – aber strikt je
Mandant: Nichtöffentliches nur mit eigenem NÖ-Recht im Mandanten, Links nur bei eigener Mitgliedschaft,
keine Mandantenseite über die Gruppenrolle, Zugriffe im Protokoll jedes Mandanten.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast

import pytest
from django.db import IntegrityError, connection, transaction
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.two_factor_policy import two_factor_reasons
from apps.accounts.views import LoginView
from apps.common.tests.factories import UserFactory
from apps.session.models import (
    SessionAuditLog,
    SessionCosignature,
    SessionMeeting,
    SessionOrganization,
    SessionPaper,
    SessionRole,
    SessionTenant,
    SessionTenantGroup,
    SessionTenantGroupMembership,
    SessionTenantGroupTenant,
    SessionUser,
)
from apps.session.services import leitstelle_service

pytestmark = pytest.mark.django_db

NOE_TITEL_A = "Grundstücksverkauf Hafenkante vertraulich"
NOE_TITEL_B = "Personalangelegenheit Bezirksamt geheim"
OE_TITEL_B = "Neugestaltung Marktplatz Süd"
GEHEIMTEXT = "Kaufpreis 95 Euro je Quadratmeter"


def _nutzer(email: str, **extra: Any) -> User:
    return cast(User, cast(Any, UserFactory)(email=email, **extra))


def _client(user: User) -> Client:
    client = Client()
    client.force_login(user)
    return client


def _mitglied(user: User, tenant: SessionTenant, **rechte: bool) -> SessionUser:
    role = SessionRole.objects.create(tenant=tenant, name=f"Rolle {user.email}", **rechte)
    session_user = SessionUser.objects.create(user=user, tenant=tenant)
    session_user.roles.add(role)
    return session_user


def _vorlage(tenant: SessionTenant, name: str, *, public: bool, **extra: Any) -> SessionPaper:
    return SessionPaper.objects.create(tenant=tenant, name=name, is_public=public, status="review", **extra)


@dataclass
class Welt:
    gruppe: SessionTenantGroup
    nord: SessionTenant
    sued: SessionTenant
    fremd: SessionTenant
    leitstelle: User
    noe_a: SessionPaper
    noe_b: SessionPaper
    oe_b: SessionPaper


@pytest.fixture
def welt() -> Welt:
    nord = SessionTenant.objects.create(name="Bezirk Nord", slug="nord")
    sued = SessionTenant.objects.create(name="Bezirk Süd", slug="sued")
    fremd = SessionTenant.objects.create(name="Stadt Fremd", slug="fremd")
    gruppe = SessionTenantGroup.objects.create(name="Bezirke", slug="bezirke")
    SessionTenantGroupTenant.objects.create(group=gruppe, tenant=nord)
    SessionTenantGroupTenant.objects.create(group=gruppe, tenant=sued)

    leitstelle = _nutzer("leitstelle@example.org")
    SessionTenantGroupMembership.objects.create(group=gruppe, user=leitstelle)
    # In Nord Mitglied, aber ohne NÖ-Recht; in Süd gar nicht
    _mitglied(leitstelle, nord, can_view_papers=True, can_view_meetings=True)

    noe_a = _vorlage(nord, NOE_TITEL_A, public=False, reference="N-0001")
    cast(Any, noe_a).set_confidential_text_encrypted(GEHEIMTEXT)
    noe_a.save()
    noe_b = _vorlage(sued, NOE_TITEL_B, public=False, reference="S-0001")
    oe_b = _vorlage(sued, OE_TITEL_B, public=True, reference="S-0002", deadline=timezone.localdate())
    SessionPaper.objects.create(tenant=fremd, name="Vorlage der fremden Stadt", is_public=True, status="review")
    return Welt(gruppe, nord, sued, fremd, leitstelle, noe_a, noe_b, oe_b)


def _uebersicht(user: User, slug: str = "bezirke") -> Any:
    return _client(user).get(f"/session/leitstelle/{slug}/")


# =============================================================================
# Zugang nur über die Gruppenmitgliedschaft
# =============================================================================


def test_ohne_gruppenmitgliedschaft_404(welt: Welt) -> None:
    fremde = _nutzer("fremde@example.org")
    assert _uebersicht(fremde).status_code == 404
    # Auch Superuser brauchen eine Mitgliedschaft in der Leitstelle
    admin = _nutzer("admin@example.org", is_superuser=True, is_staff=True)
    assert _uebersicht(admin).status_code == 404
    # Mitglied eines Mandanten der Gruppe ist noch keine Leitstelle
    nord_nutzer = _nutzer("nord@example.org")
    _mitglied(nord_nutzer, welt.nord, is_admin=True)
    assert _uebersicht(nord_nutzer).status_code == 404
    assert _client(nord_nutzer).get("/session/leitstelle/bezirke/suche/?q=Markt").status_code == 404
    # Unbekannte Gruppe, deaktivierte Mitgliedschaft, deaktivierte Gruppe
    assert _uebersicht(welt.leitstelle, "gibt-es-nicht").status_code == 404
    SessionTenantGroupMembership.objects.filter(user=welt.leitstelle).update(is_active=False)
    assert _uebersicht(welt.leitstelle).status_code == 404
    SessionTenantGroupMembership.objects.filter(user=welt.leitstelle).update(is_active=True)
    SessionTenantGroup.objects.filter(pk=welt.gruppe.pk).update(is_active=False)
    assert _uebersicht(welt.leitstelle).status_code == 404
    # Anonym: Anmeldung
    anonym = Client().get("/session/leitstelle/bezirke/")
    assert anonym.status_code == 302 and "login" in anonym["Location"]


def test_gruppe_von_x_oeffnet_keinen_zugriff_auf_y(welt: Welt) -> None:
    andere = SessionTenantGroup.objects.create(name="Andere Gruppe", slug="andere")
    SessionTenantGroupTenant.objects.create(group=andere, tenant=welt.fremd)
    # Die Leitstelle der Bezirke sieht die Gruppe der fremden Stadt nicht
    assert _uebersicht(welt.leitstelle, "andere").status_code == 404
    seite = _uebersicht(welt.leitstelle).content.decode()
    assert "Stadt Fremd" not in seite and "Vorlage der fremden Stadt" not in seite
    suche = _client(welt.leitstelle).get("/session/leitstelle/bezirke/suche/?q=Vorlage").content.decode()
    assert "Vorlage der fremden Stadt" not in suche
    # Ein Mandant gehört höchstens einer Gruppe an
    with pytest.raises(IntegrityError), transaction.atomic():
        SessionTenantGroupTenant.objects.create(group=andere, tenant=welt.nord)


def test_gruppenrolle_oeffnet_keine_mandantenseiten(welt: Welt) -> None:
    client = _client(welt.leitstelle)
    # Süd: keine Mitgliedschaft – weder Dashboard noch Vorlage, trotz Leitstelle
    assert client.get("/session/sued/").status_code == 403
    assert client.get(f"/session/sued/papers/{welt.oe_b.pk}/").status_code == 403
    assert client.get("/session/sued/meetings/").status_code == 403
    # Nord: Mitglied ohne NÖ-Recht – die NÖ-Vorlage bleibt verborgen
    assert client.get(f"/session/nord/papers/{welt.noe_a.pk}/").status_code == 404

    seite = _uebersicht(welt.leitstelle).content.decode()
    # Kein Link nach Süd, klar als „kein Zugang“ markiert; Nord-Dashboard verlinkt
    assert "/session/sued/" not in seite
    assert "kein Zugang" in seite
    assert 'href="/session/nord/"' in seite
    assert OE_TITEL_B in seite
    assert f"/session/sued/papers/{welt.oe_b.pk}/" not in seite


# =============================================================================
# Sichtbarkeit: Nichtöffentliches nur mit eigenem NÖ-Recht
# =============================================================================


def test_leitstelle_ohne_noe_recht_sieht_keine_noe_titel(welt: Welt) -> None:
    seite = _uebersicht(welt.leitstelle).content.decode()
    assert NOE_TITEL_A not in seite and NOE_TITEL_B not in seite
    assert "N-0001" not in seite and "S-0001" not in seite
    assert leitstelle_service.HIDDEN_PAPER in seite
    assert GEHEIMTEXT not in seite
    # Gezählt wird trotzdem (drei Vorlagen in Prüfung, davon zwei nichtöffentlich)
    overview = leitstelle_service.build_overview(
        SessionTenantGroupMembership.objects.select_related("group").get(user=welt.leitstelle), welt.leitstelle
    )
    assert overview.totals["papers_review"] == 3
    assert overview.totals["papers_review_non_public"] == 2
    verborgen = [row for row in overview.review_papers if row.hidden]
    assert len(verborgen) == 2
    assert all(row.url is None and row.reference == "" and row.detail == "" for row in verborgen)

    # Die Suche findet Nichtöffentliches ohne Recht gar nicht – auch nicht als Zahl
    suche = _client(welt.leitstelle).get("/session/leitstelle/bezirke/suche/?q=vertraulich").content.decode()
    assert NOE_TITEL_A not in suche
    assert "0 Treffer" in suche
    nummer = _client(welt.leitstelle).get("/session/leitstelle/bezirke/suche/?q=S-000").content.decode()
    assert OE_TITEL_B in nummer and NOE_TITEL_B not in nummer


def test_noe_titel_nur_im_mandanten_mit_eigenem_noe_recht(welt: Welt) -> None:
    rolle = SessionUser.objects.get(user=welt.leitstelle, tenant=welt.nord).roles.get()
    rolle.can_view_non_public_papers = True
    rolle.save()
    seite = _uebersicht(welt.leitstelle).content.decode()
    assert NOE_TITEL_A in seite  # Nord: eigenes NÖ-Recht
    assert NOE_TITEL_B not in seite  # Süd: keine Mitgliedschaft
    assert f"/session/nord/papers/{welt.noe_a.pk}/" in seite
    assert GEHEIMTEXT not in seite  # verschlüsselte Felder werden nie entschlüsselt
    suche = _client(welt.leitstelle).get("/session/leitstelle/bezirke/suche/?q=vertraulich").content.decode()
    assert NOE_TITEL_A in suche


def test_verschluesselte_felder_werden_nicht_geladen() -> None:
    assert "confidential_text_encrypted" in leitstelle_service._deferred(SessionPaper)
    assert "internal_notes_encrypted" in leitstelle_service._deferred(SessionMeeting)


def test_kennzahlen_rolle_sieht_nur_zaehlwerte(welt: Welt) -> None:
    SessionTenantGroupMembership.objects.filter(user=welt.leitstelle).update(
        role=SessionTenantGroupMembership.ROLE_KENNZAHLEN
    )
    seite = _uebersicht(welt.leitstelle).content.decode()
    assert OE_TITEL_B not in seite and leitstelle_service.HIDDEN_PAPER not in seite
    assert "Kennzahlen je Mandant" in seite
    assert _client(welt.leitstelle).get("/session/leitstelle/bezirke/suche/?q=Markt").status_code == 403


def test_arbeitsvorraete_fristen_und_sitzungen(welt: Welt) -> None:
    amt = SessionOrganization.objects.create(tenant=welt.sued, name="Rechtsamt", organization_type="department")
    SessionCosignature.objects.create(paper=welt.oe_b, department=amt)
    ausschuss = SessionOrganization.objects.create(tenant=welt.sued, name="Regionalausschuss", invitation_period_days=7)
    SessionMeeting.objects.create(
        tenant=welt.sued,
        organization=ausschuss,
        name="Sitzung Regionalausschuss",
        start=timezone.now() + timedelta(days=5),
        meeting_state="scheduled",
    )
    SessionMeeting.objects.create(
        tenant=welt.sued,
        organization=ausschuss,
        name="Geheime Klausur",
        start=timezone.now() + timedelta(days=6),
        meeting_state="scheduled",
        is_public=False,
    )
    membership = SessionTenantGroupMembership.objects.select_related("group").get(user=welt.leitstelle)
    overview = leitstelle_service.build_overview(membership, welt.leitstelle)
    assert [row.detail for row in overview.cosignatures] == ["Mitzeichnung: Rechtsamt"]
    assert [row.title for row in overview.paper_deadlines] == [OE_TITEL_B]
    ladung = {row.title: row for row in overview.invitation_deadlines}
    assert ladung["Sitzung Regionalausschuss"].overdue is True
    assert leitstelle_service.HIDDEN_MEETING in ladung and "Geheime Klausur" not in ladung
    assert {row.title for row in overview.meetings} == {"Sitzung Regionalausschuss", leitstelle_service.HIDDEN_MEETING}
    sued = next(f for f in overview.figures if f.tenant == welt.sued)
    assert (sued.cosignatures_open, sued.invitation_deadlines, sued.meetings_upcoming) == (1, 2, 2)


# =============================================================================
# Protokoll und Nachvollziehbarkeit
# =============================================================================


def test_jeder_aufruf_ist_ein_lesezugriff_je_mandant(welt: Welt) -> None:
    client = _client(welt.leitstelle)
    client.get("/session/leitstelle/bezirke/")
    eintraege = SessionAuditLog.objects.filter(model_name="SessionTenantGroup", action="view")
    assert {e.tenant_id for e in eintraege} == {welt.nord.pk, welt.sued.pk}
    sued = eintraege.get(tenant=welt.sued)
    assert sued.user is None and sued.changes["konto"] == "leitstelle@example.org"
    assert sued.changes["ansicht"] == "Übersicht" and sued.changes["mitglied_im_mandanten"] is False
    assert sued.object_repr == "Leitstellen-Übersicht Bezirke"
    nord = eintraege.get(tenant=welt.nord)
    assert nord.user is not None and "konto" not in nord.changes
    # Zusammengefasst: ein erneuter Aufruf innerhalb von zehn Minuten schreibt nichts
    client.get("/session/leitstelle/bezirke/")
    assert SessionAuditLog.objects.filter(model_name="SessionTenantGroup", action="view").count() == 2
    # Die Suche ist ein eigener Lesezugriff, mit Trefferzahl, ohne Suchbegriff
    client.get("/session/leitstelle/bezirke/suche/?q=Marktplatz")
    suche = SessionAuditLog.objects.get(model_name="SessionTenantGroup", tenant=welt.sued, changes__ansicht="Suche")
    assert suche.changes["treffer"] == 1
    assert "Marktplatz" not in str(suche.changes)
    # Auch wenn der Mandant Lesezugriffe sonst nicht protokolliert
    SessionTenant.objects.filter(pk=welt.nord.pk).update(settings={"privacy": {"read_logging": False}})
    anderer = _nutzer("zweite@example.org")
    SessionTenantGroupMembership.objects.create(group=welt.gruppe, user=anderer)
    _client(anderer).get("/session/leitstelle/bezirke/")
    uebersichten = SessionAuditLog.objects.filter(
        model_name="SessionTenantGroup", tenant=welt.nord, action="view", changes__ansicht="Übersicht"
    )
    assert uebersichten.count() == 2


def test_leitstellen_rechte_stehen_im_protokoll_der_mandanten(welt: Welt) -> None:
    neu = _nutzer("neu@example.org")
    mitgliedschaft = SessionTenantGroupMembership.objects.create(group=welt.gruppe, user=neu)
    erteilt = list(SessionAuditLog.objects.filter(action="permissions_changed", changes__konto="neu@example.org"))
    assert {e.tenant_id for e in erteilt} == {welt.nord.pk, welt.sued.pk}
    assert {e.changes["vorgang"] for e in erteilt} == {"Leitstellen-Zugang erteilt"}
    mitgliedschaft.delete()
    entzogen = SessionAuditLog.objects.filter(
        action="permissions_changed", changes__vorgang="Leitstellen-Zugang entzogen", changes__konto="neu@example.org"
    )
    assert entzogen.count() == 2
    zweite = SessionTenantGroup.objects.create(name="G2", slug="g2")
    SessionTenantGroupTenant.objects.create(group=zweite, tenant=welt.fremd)
    zugeordnet = SessionAuditLog.objects.get(tenant=welt.fremd, changes__vorgang="Mandant der Leitstelle zugeordnet")
    assert zugeordnet.changes["leitstelle_konten"] == []


def test_admin_pflegt_die_gruppe_und_das_protokoll_nennt_den_admin(welt: Welt) -> None:
    admin = _nutzer("plattform@example.org", is_superuser=True, is_staff=True)
    client = _client(admin)
    assert client.get("/admin/session/sessiontenantgroup/add/").status_code == 200
    neu = _nutzer("neue-leitstelle@example.org")
    antwort = client.post(
        "/admin/session/sessiontenantgroup/add/",
        {
            "name": "Stadtgruppe",
            "slug": "stadtgruppe",
            "description": "",
            "is_active": "on",
            "tenant_links-TOTAL_FORMS": "1",
            "tenant_links-INITIAL_FORMS": "0",
            "tenant_links-MIN_NUM_FORMS": "0",
            "tenant_links-MAX_NUM_FORMS": "1000",
            "tenant_links-0-tenant": str(welt.fremd.pk),
            "memberships-TOTAL_FORMS": "1",
            "memberships-INITIAL_FORMS": "0",
            "memberships-MIN_NUM_FORMS": "0",
            "memberships-MAX_NUM_FORMS": "1000",
            "memberships-0-user": str(neu.pk),
            "memberships-0-role": SessionTenantGroupMembership.ROLE_KENNZAHLEN,
            "memberships-0-is_active": "on",
            "memberships-0-note": "Controlling",
        },
    )
    assert antwort.status_code == 302, antwort.content.decode()[:2000]
    gruppe = SessionTenantGroup.objects.get(slug="stadtgruppe")
    assert list(gruppe.tenants.all()) == [welt.fremd]
    erteilt = SessionAuditLog.objects.get(tenant=welt.fremd, changes__vorgang="Leitstellen-Zugang erteilt")
    assert erteilt.changes["konto"] == "neue-leitstelle@example.org"
    assert erteilt.changes["geaendert_von"] == "plattform@example.org"


def test_deaktivierter_mandant_erscheint_nicht(welt: Welt) -> None:
    SessionTenant.objects.filter(pk=welt.sued.pk).update(is_active=False)
    seite = _uebersicht(welt.leitstelle).content.decode()
    assert "Bezirk Süd" not in seite and OE_TITEL_B not in seite


@override_settings(TWO_FACTOR_ENFORCEMENT=True, TWO_FACTOR_EXEMPT_EMAIL_DOMAINS=())
def test_leitstelle_braucht_zweiten_faktor(welt: Welt) -> None:
    assert "Leitstelle Bezirke" in two_factor_reasons(welt.leitstelle)


# =============================================================================
# Seitenleiste und Leistung
# =============================================================================


def test_nur_leitstelle_landet_nach_der_anmeldung_in_der_uebersicht(welt: Welt) -> None:
    nur_leitstelle = _nutzer("nur-leitstelle@example.org")
    SessionTenantGroupMembership.objects.create(group=welt.gruppe, user=nur_leitstelle)
    request = type(
        "R", (), {"user": nur_leitstelle, "get_host": lambda self: "testserver", "is_secure": lambda self: False}
    )()
    assert cast(Any, LoginView()).get_success_url(request) == "/session/leitstelle/bezirke/"


def test_seitenleiste_zeigt_leitstelle_nur_mitgliedern(welt: Welt) -> None:
    seite = _client(welt.leitstelle).get("/session/nord/").content.decode()
    assert 'href="/session/leitstelle/bezirke/"' in seite
    nord_nutzer = _nutzer("nord@example.org")
    _mitglied(nord_nutzer, welt.nord, is_admin=True)
    assert "/session/leitstelle/" not in _client(nord_nutzer).get("/session/nord/").content.decode()


def _abfragen(membership: SessionTenantGroupMembership, user: User) -> int:
    with CaptureQueriesContext(connection) as erfasst:
        leitstelle_service.build_overview(membership, user)
    return len(erfasst)


def test_abfragen_haengen_nicht_von_der_zahl_der_mandanten_ab(welt: Welt) -> None:
    membership = SessionTenantGroupMembership.objects.select_related("group").get(user=welt.leitstelle)
    vorher = _abfragen(membership, welt.leitstelle)
    for nummer in range(5):
        tenant = SessionTenant.objects.create(name=f"Bezirk {nummer}", slug=f"bezirk-{nummer}")
        SessionTenantGroupTenant.objects.create(group=welt.gruppe, tenant=tenant)
        _vorlage(tenant, f"Vorlage {nummer}", public=nummer % 2 == 0)
        _mitglied(welt.leitstelle, tenant, can_view_papers=True)
    assert _abfragen(membership, welt.leitstelle) == vorher
