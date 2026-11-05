# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Mitgliedschaft „Nur Lesen, Löschung nach 30 Tagen“ (Issue #1237, Entscheidungen Sven vom 05.11.2026).

Admins setzen den Zustand in der Mitgliederverwaltung (Rechte wie Deaktivieren und Entfernen) und können ihn bis
zur Löschung zurücknehmen. Bis dahin sieht die Person in dieser Organisation nur die Auslauf-Seite mit Löschdatum
und dem Export ihrer eigenen Daten. Erinnerungen gehen 7 und 3 Tage vorher je einmal raus. Am Löschtag gelten die
Regeln von „Mitglied endgültig entfernen“, zusätzlich entfallen private Dokumente und Entwürfe der Person; ohne
weitere Bindung wird auch das Konto gelöscht. Jede Änderung steht in der Änderungshistorie.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.common.mail_backends import build_backend
from apps.common.tests import factories
from apps.tenants.models import Membership
from apps.work.faction.models import FactionAuditLog
from apps.work.motions.models import Motion, MotionComment, MotionDocument, MotionRevision, MotionShare
from apps.work.organization import auslauf, services
from apps.work.organization.models import DataExport
from apps.work.organization.services import ServiceError

pytestmark = pytest.mark.django_db

#: Fabriken sind untypisiert (factory_boy)
fabrik: Any = factories

TAG = timedelta(days=1)


@pytest.fixture(autouse=True)
def _cache_leeren() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def _medien(settings: Any, tmp_path: Path) -> Path:
    settings.MEDIA_ROOT = str(tmp_path)
    return tmp_path


@pytest.fixture
def admin(org: Any, make_member: Any) -> Any:
    return make_member(org, ["members.edit", "members.remove"], email="admin@example.org", is_admin=True)


@pytest.fixture
def mitglied(org: Any, make_member: Any) -> Any:
    return make_member(org, ["dashboard.view", "motions.view", "motions.edit", "tasks.view"], email="geht@example.org")


@pytest.fixture
def kollegin(org: Any, make_member: Any) -> Any:
    return make_member(org, ["dashboard.view", "motions.view"], email="bleibt@example.org")


def _eintraege(org: Any) -> list[FactionAuditLog]:
    return list(FactionAuditLog.objects.filter(organization=org, model_name="Membership").order_by("seq"))


def _an(adresse: str) -> list[Any]:
    return [nachricht for nachricht in mail.outbox if adresse in nachricht.to]


def _motion(org: Any, author: Any, title: str, *, visibility: str = "organization", status: str = "draft") -> Any:
    motion: Any = Motion(organization=org, author=author, title=title, visibility=visibility, status=status)
    motion.set_content_encrypted("<p>Inhalt</p>")
    motion.save()
    return motion


def _setzen(org: Any, member: Any, admin: Any) -> Any:
    auslauf.schedule_member_deletion(org, member, admin.user)
    member.refresh_from_db()
    return member


def _datum(member: Any) -> str:
    return str(timezone.localtime(member.deletion_scheduled_at).strftime("%d.%m.%Y"))


# ---------------------------------------------------------------------------
# Setzen und Rechte
# ---------------------------------------------------------------------------


def test_setzen_sperrt_den_zugang_und_plant_die_loeschung_in_30_tagen(org: Any, admin: Any, mitglied: Any) -> None:
    vorher = timezone.now()

    _setzen(org, mitglied, admin)

    assert mitglied.is_active is False
    assert mitglied.deletion_pending is True
    assert vorher + 30 * TAG <= mitglied.deletion_scheduled_at <= timezone.now() + 30 * TAG
    assert mitglied.registration_requested_at is None
    # Zählt nicht als aktives Mitglied (Kontingent)
    assert org.get_active_member_count() == 1


def test_setzen_steht_in_der_aenderungshistorie(org: Any, admin: Any, mitglied: Any) -> None:
    _setzen(org, mitglied, admin)

    (eintrag,) = _eintraege(org)
    assert eintrag.action == "update"
    assert eintrag.membership == admin
    assert eintrag.changes["Zugang"] == {"alt": "aktiv", "neu": f"Nur Lesen, Löschung am {_datum(mitglied)}"}


def test_setzen_mailt_der_person_ueber_den_plattformweg(org: Any, admin: Any, mitglied: Any) -> None:
    org.mail_sender_mode = "smtp"
    org.smtp_host = "smtp.example.org"
    org.smtp_from_email = "fraktion@example.org"
    org.save()
    with mock.patch(
        "apps.common.mail.config.organization_backend",
        return_value=build_backend("django.core.mail.backends.locmem.EmailBackend"),
    ) as eigenes_smtp:
        _setzen(org, mitglied, admin)

    eigenes_smtp.assert_not_called()
    (nachricht,) = _an("geht@example.org")
    assert _datum(mitglied) in nachricht.subject
    assert reverse("work:membership_expiry", kwargs={"org_slug": org.slug}) in nachricht.body
    assert "deine" in nachricht.body


def test_nicht_fuer_eigentuemer_und_nicht_fuer_sich_selbst(org: Any, admin: Any, mitglied: Any) -> None:
    org.owner = mitglied.user
    org.save()
    with pytest.raises(ServiceError):
        auslauf.schedule_member_deletion(org, mitglied, admin.user)
    with pytest.raises(ServiceError):
        auslauf.schedule_member_deletion(org, admin, admin.user)

    mitglied.refresh_from_db()
    assert mitglied.is_active is True
    assert mitglied.deletion_scheduled_at is None


def test_administratoren_nur_durch_administratoren(org: Any, make_member: Any, admin: Any) -> None:
    verwaltung = make_member(org, ["members.edit", "members.remove"], email="verwaltung@example.org")

    with pytest.raises(ServiceError):
        auslauf.schedule_member_deletion(org, admin, verwaltung.user)

    admin.refresh_from_db()
    assert admin.deletion_scheduled_at is None


def test_nicht_fuer_gaeste_und_offene_registrierungen(org: Any, admin: Any) -> None:
    gast = fabrik.MembershipFactory(organization=org, user=fabrik.UserFactory(email="gast@example.org"), is_guest=True)
    anfrage = fabrik.MembershipFactory(
        organization=org,
        user=fabrik.UserFactory(email="anfrage@example.org"),
        is_active=False,
        registration_requested_at=timezone.now(),
    )
    for member in (gast, anfrage):
        with pytest.raises(ServiceError):
            auslauf.schedule_member_deletion(org, member, admin.user)


def test_mitgliederverwaltung_setzt_nur_mit_recht_zum_entfernen(
    org: Any, make_member: Any, mitglied: Any, client_for: Any
) -> None:
    ohne_recht = make_member(org, ["members.view", "members.edit"], email="ohne@example.org")
    url = reverse("work:member_detail", kwargs={"org_slug": org.slug, "member_id": mitglied.id})

    client_for(ohne_recht.user).post(url, {"action": "schedule_deletion"})
    mitglied.refresh_from_db()
    assert mitglied.deletion_scheduled_at is None

    mit_recht = make_member(org, ["members.view", "members.edit", "members.remove"], email="mit@example.org")
    client_for(mit_recht.user).post(url, {"action": "schedule_deletion"})
    mitglied.refresh_from_db()
    assert mitglied.deletion_pending is True


def test_mitgliederverwaltung_zeigt_das_loeschdatum(org: Any, admin: Any, mitglied: Any, client_for: Any) -> None:
    _setzen(org, mitglied, admin)
    client = client_for(admin.user)

    liste = client.get(reverse("work:members", kwargs={"org_slug": org.slug}))
    detail = client.get(reverse("work:member_detail", kwargs={"org_slug": org.slug, "member_id": mitglied.id}))

    assert f"Löschung am {_datum(mitglied)}" in liste.content.decode()
    assert f"Löschung am {_datum(mitglied)}" in detail.content.decode()
    assert 'value="cancel_deletion"' in detail.content.decode()


# ---------------------------------------------------------------------------
# Zugriff während der 30 Tage
# ---------------------------------------------------------------------------


def test_organisationsseiten_leiten_auf_die_auslaufseite(org: Any, admin: Any, mitglied: Any, client_for: Any) -> None:
    _setzen(org, mitglied, admin)
    client = client_for(mitglied.user)
    ziel = reverse("work:membership_expiry", kwargs={"org_slug": org.slug})

    for name in ("work:dashboard", "work:members", "work:profile_data"):
        antwort = client.get(reverse(name, kwargs={"org_slug": org.slug}))
        assert antwort.status_code == 302, name
        assert antwort["Location"] == ziel, name

    # Ändern geht nicht: Ein POST landet ebenfalls auf der Auslauf-Seite
    antwort = client.post(reverse("work:profile_data", kwargs={"org_slug": org.slug}), {"action": "export_data"})
    assert antwort["Location"] == ziel
    assert not DataExport.objects.exists()

    seite = client.get(ziel)
    assert seite.status_code == 200
    text = seite.content.decode()
    assert _datum(mitglied) in text
    assert org.name in text


def test_auslaufseite_nur_fuer_die_eigene_auslaufende_mitgliedschaft(
    org: Any, admin: Any, mitglied: Any, client_for: Any
) -> None:
    ziel = reverse("work:membership_expiry", kwargs={"org_slug": org.slug})

    # Aktive Mitglieder kommen zum Start
    antwort = client_for(mitglied.user).get(ziel)
    assert antwort.status_code == 302
    assert antwort["Location"] == reverse("work:dashboard", kwargs={"org_slug": org.slug})

    # Deaktivierte bleiben ausgesperrt wie bisher
    services.deactivate_member(org, mitglied, admin.user)
    assert client_for(mitglied.user).get(ziel).status_code == 403
    assert client_for(mitglied.user).get(reverse("work:dashboard", kwargs={"org_slug": org.slug})).status_code == 403

    # Fremde ebenso
    fremd = fabrik.UserFactory(email="fremd@example.org")
    assert client_for(fremd).get(ziel).status_code == 403


def test_export_der_eigenen_daten_mit_eigenen_dokumenten(org: Any, admin: Any, mitglied: Any, client_for: Any) -> None:
    _motion(org, mitglied, "Mein privates Konzept", visibility="private")
    _setzen(org, mitglied, admin)
    client = client_for(mitglied.user)
    ziel = reverse("work:membership_expiry", kwargs={"org_slug": org.slug})

    antwort = client.post(ziel, {"action": "export_data", "format": "json"})

    assert antwort.status_code == 302
    export = DataExport.objects.get(membership=mitglied)
    assert export.status == "completed"
    download = client.get(
        reverse("work:membership_expiry_download", kwargs={"org_slug": org.slug, "export_id": export.id})
    )
    assert download.status_code == 200
    inhalt = download.content.decode()
    assert "Mein privates Konzept" in inhalt
    assert "geht@example.org" in inhalt
    # Die Seite bietet den Download an
    assert str(export.id) in client.get(ziel).content.decode()


def test_exportliste_als_fragment(org: Any, admin: Any, mitglied: Any, client_for: Any) -> None:
    _setzen(org, mitglied, admin)
    DataExport.objects.create(organization=org, membership=mitglied, status="processing", started_at=timezone.now())

    antwort = client_for(mitglied.user).get(
        reverse("work:membership_expiry", kwargs={"org_slug": org.slug}) + "?teil=exporte"
    )

    text = antwort.content.decode()
    assert antwort.status_code == 200
    assert 'id="auslauf-exporte"' in text
    assert "hx-trigger" in text
    assert "<html" not in text


def test_export_fremder_mitgliedschaften_bleibt_gesperrt(
    org: Any, admin: Any, mitglied: Any, kollegin: Any, client_for: Any
) -> None:
    fremder_export = DataExport.objects.create(organization=org, membership=kollegin, status="completed")
    _setzen(org, mitglied, admin)

    antwort = client_for(mitglied.user).get(
        reverse("work:membership_expiry_download", kwargs={"org_slug": org.slug, "export_id": fremder_export.id})
    )

    assert antwort.status_code == 404


def test_andere_mitgliedschaften_bleiben_unberuehrt(org: Any, admin: Any, mitglied: Any, client_for: Any) -> None:
    andere = fabrik.OrganizationFactory(name="Andere Fraktion", slug="andere-fraktion")
    rolle = fabrik.RoleFactory(organization=andere, permissions=["dashboard.view"])
    fabrik.MembershipFactory(user=mitglied.user, organization=andere, roles=[rolle])
    _setzen(org, mitglied, admin)

    antwort = client_for(mitglied.user).get(reverse("work:dashboard", kwargs={"org_slug": andere.slug}))

    assert antwort.status_code == 200
    assert Membership.objects.get(user=mitglied.user, organization=andere).is_active is True


def test_work_einstieg_fuehrt_zur_auslaufseite(org: Any, admin: Any, mitglied: Any, client_for: Any) -> None:
    _setzen(org, mitglied, admin)

    antwort = client_for(mitglied.user).get(reverse("work:root"))

    assert antwort["Location"] == reverse("work:membership_expiry", kwargs={"org_slug": org.slug})


# ---------------------------------------------------------------------------
# Zurücknehmen
# ---------------------------------------------------------------------------


def test_zuruecknehmen_fuehrt_zu_deaktiviert(org: Any, admin: Any, mitglied: Any) -> None:
    _setzen(org, mitglied, admin)
    datum = _datum(mitglied)

    auslauf.cancel_member_deletion(org, mitglied, admin.user)
    mitglied.refresh_from_db()

    assert mitglied.is_active is False
    assert mitglied.deletion_scheduled_at is None
    assert mitglied.deletion_pending is False
    eintrag = _eintraege(org)[-1]
    assert eintrag.changes["Zugang"] == {"alt": f"Nur Lesen, Löschung am {datum}", "neu": "deaktiviert"}
    assert eintrag.membership == admin

    # Danach löscht der Lauf nichts mehr
    auslauf.run_membership_expiry_pass(now=timezone.now() + 31 * TAG)
    assert Membership.objects.filter(pk=mitglied.pk).exists()


def test_zuruecknehmen_ueber_die_mitgliederverwaltung(org: Any, admin: Any, mitglied: Any, client_for: Any) -> None:
    _setzen(org, mitglied, admin)

    client_for(admin.user).post(
        reverse("work:member_detail", kwargs={"org_slug": org.slug, "member_id": mitglied.id}),
        {"action": "cancel_deletion"},
    )

    mitglied.refresh_from_db()
    assert mitglied.deletion_scheduled_at is None


def test_reaktivieren_leert_das_loeschdatum(org: Any, admin: Any, mitglied: Any) -> None:
    _setzen(org, mitglied, admin)
    datum = _datum(mitglied)

    services.reactivate_member(org, mitglied, admin)
    mitglied.refresh_from_db()

    assert mitglied.is_active is True
    assert mitglied.deletion_scheduled_at is None
    assert mitglied.deletion_reminder_days is None
    assert _eintraege(org)[-1].changes["Zugang"] == {"alt": f"Nur Lesen, Löschung am {datum}", "neu": "aktiv"}


def test_aktivieren_auf_anderem_weg_entschaerft_die_loeschung(org: Any, admin: Any, mitglied: Any) -> None:
    """Etwa im Django-Admin: Ein späteres Deaktivieren darf die alte Löschung nicht wieder scharf schalten."""
    _setzen(org, mitglied, admin)

    mitglied.is_active = True
    mitglied.save(update_fields=["is_active"])
    services.deactivate_member(org, mitglied, admin.user)
    auslauf.run_membership_expiry_pass(now=timezone.now() + 31 * TAG)

    mitglied.refresh_from_db()
    assert mitglied.deletion_scheduled_at is None
    assert Membership.objects.filter(pk=mitglied.pk).exists()


# ---------------------------------------------------------------------------
# Erinnerungen
# ---------------------------------------------------------------------------


def test_erinnerungen_7_und_3_tage_vorher_je_genau_einmal(org: Any, admin: Any, mitglied: Any) -> None:
    _setzen(org, mitglied, admin)
    loeschung = mitglied.deletion_scheduled_at
    mail.outbox.clear()

    auslauf.run_membership_expiry_pass(now=loeschung - 8 * TAG)
    assert _an("geht@example.org") == []

    for _ in range(3):
        auslauf.run_membership_expiry_pass(now=loeschung - 7 * TAG + timedelta(hours=1))
    assert len(_an("geht@example.org")) == 1
    assert "7 Tagen" in _an("geht@example.org")[0].body

    auslauf.run_membership_expiry_pass(now=loeschung - 4 * TAG)
    assert len(_an("geht@example.org")) == 1

    for _ in range(3):
        auslauf.run_membership_expiry_pass(now=loeschung - 3 * TAG + timedelta(hours=1))
    assert len(_an("geht@example.org")) == 2
    assert "3 Tagen" in _an("geht@example.org")[1].body
    assert Membership.objects.filter(pk=mitglied.pk).exists()


def test_verspaeteter_lauf_schickt_nur_die_naechste_erinnerung(org: Any, admin: Any, mitglied: Any) -> None:
    _setzen(org, mitglied, admin)
    mail.outbox.clear()

    auslauf.run_membership_expiry_pass(now=mitglied.deletion_scheduled_at - 2 * TAG)
    auslauf.run_membership_expiry_pass(now=mitglied.deletion_scheduled_at - 1 * TAG)

    assert len(_an("geht@example.org")) == 1


def test_lauf_mit_sperre_gegen_doppelte_ausfuehrung(org: Any, admin: Any, mitglied: Any) -> None:
    _setzen(org, mitglied, admin)
    cache.add(auslauf.LOCK_KEY, "1", timeout=60)

    ergebnis = auslauf.run_membership_expiry_pass(now=mitglied.deletion_scheduled_at + TAG)

    assert ergebnis == {"skipped": "lock"}
    assert Membership.objects.filter(pk=mitglied.pk).exists()


def test_zeitplan_ist_registriert() -> None:
    from apps.events.schedule import registry
    from apps.work import schedules

    assert registry.get(schedules.mitgliedschaften_auslaufen.module_path) is not None


# ---------------------------------------------------------------------------
# Löschtag
# ---------------------------------------------------------------------------


@pytest.fixture
def dokumente(org: Any, mitglied: Any, kollegin: Any) -> dict[str, Any]:
    d: dict[str, Any] = {}
    d["privat"] = _motion(org, mitglied, "Privates Konzept", visibility="private", status="submitted")
    d["anhang"] = MotionDocument.objects.create(
        motion=d["privat"],
        file=SimpleUploadedFile("konzept.pdf", b"%PDF-1.4"),
        filename="konzept.pdf",
        mime_type="application/pdf",
        uploaded_by=mitglied,
    )
    d["version"] = MotionRevision.objects.create(motion=d["privat"], version=1, changed_by=mitglied)
    d["entwurf"] = _motion(org, mitglied, "Eigener Entwurf", visibility="organization", status="draft")
    d["veroeffentlicht"] = _motion(org, mitglied, "Fraktionsantrag", visibility="organization", status="submitted")
    d["geteilt"] = _motion(org, mitglied, "Geteilter Entwurf", visibility="shared", status="draft")
    MotionShare.objects.create(
        motion=d["geteilt"], scope="user", user=kollegin.user, level="view", created_by=admin_user(org)
    )
    d["mit_federfuehrung"] = _motion(org, mitglied, "Gemeinsam privat", visibility="private")
    d["mit_federfuehrung"].responsible = kollegin
    d["mit_federfuehrung"].save()
    d["kommentiert"] = _motion(org, mitglied, "Kommentierter Entwurf", visibility="organization", status="draft")
    MotionComment.objects.create(motion=d["kommentiert"], content="Gute Idee", author=kollegin)
    d["fremd"] = _motion(org, kollegin, "Fremder Entwurf", visibility="private")
    return d


def admin_user(org: Any) -> User:
    """Freigaben legt im Szenario die Verwaltung an (sonst bände die Freigabe das Konto der Person)."""
    return Membership.objects.get(organization=org, user__email="admin@example.org").user


def test_private_dokumente_und_entwuerfe_sind_abgegrenzt(
    org: Any, admin: Any, mitglied: Any, dokumente: dict[str, Any]
) -> None:
    gewaehlt = set(auslauf.private_documents_and_drafts(mitglied).values_list("title", flat=True))

    assert gewaehlt == {"Privates Konzept", "Eigener Entwurf"}


def test_loeschtag_loescht_mitgliedschaft_private_dokumente_und_konto(
    org: Any, admin: Any, mitglied: Any, dokumente: dict[str, Any], django_capture_on_commit_callbacks: Any
) -> None:
    datei = Path(dokumente["anhang"].file.path)
    assert datei.exists()
    _setzen(org, mitglied, admin)
    datum = _datum(mitglied)
    user_id = mitglied.user_id
    mail.outbox.clear()

    with django_capture_on_commit_callbacks(execute=True):
        ergebnis = auslauf.run_membership_expiry_pass(now=mitglied.deletion_scheduled_at + timedelta(minutes=5))

    assert ergebnis["deleted"] == 1
    assert not Membership.objects.filter(pk=mitglied.pk).exists()
    assert not User.objects.filter(pk=user_id).exists()
    # Private Dokumente und Entwürfe samt Versionen, Anhängen und Dateien
    assert not Motion.objects.filter(pk__in=[dokumente["privat"].pk, dokumente["entwurf"].pk]).exists()
    assert not MotionRevision.objects.filter(pk=dokumente["version"].pk).exists()
    assert not MotionDocument.objects.filter(pk=dokumente["anhang"].pk).exists()
    assert not datei.exists()
    # Geteiltes und Organisationsinhalte bleiben als „Ehemaliges Mitglied“
    for name in ("veroeffentlicht", "geteilt", "mit_federfuehrung", "kommentiert", "fremd"):
        assert Motion.objects.filter(pk=dokumente[name].pk).exists(), name
    assert Motion.objects.get(pk=dokumente["veroeffentlicht"].pk).author is None

    eintrag = _eintraege(org)[-1]
    assert eintrag.action == "delete"
    assert eintrag.membership is None
    assert eintrag.changes["Zugang"] == {"alt": f"Nur Lesen, Löschung am {datum}", "neu": "gelöscht"}

    (bestaetigung,) = _an("geht@example.org")
    assert "Ehemaliges Mitglied" in bestaetigung.body
    assert "30 Tagen" in bestaetigung.body
    assert "2 private Dokumente und Entwürfe" in bestaetigung.body
    assert "Konto" in bestaetigung.body


def test_loeschtag_loescht_auch_exportdateien(
    org: Any, admin: Any, mitglied: Any, _medien: Path, django_capture_on_commit_callbacks: Any
) -> None:
    datei = _medien / "exports" / "export.json"
    datei.parent.mkdir(parents=True)
    datei.write_text("{}")
    DataExport.objects.create(
        organization=org, membership=mitglied, status="completed", file_path="exports/export.json"
    )
    _setzen(org, mitglied, admin)

    with django_capture_on_commit_callbacks(execute=True):
        auslauf.run_membership_expiry_pass(now=mitglied.deletion_scheduled_at + TAG)

    assert not datei.exists()
    assert not DataExport.objects.exists()


def test_konto_bleibt_bei_anderer_mitgliedschaft(org: Any, admin: Any, mitglied: Any) -> None:
    fabrik.MembershipFactory(user=mitglied.user, organization=fabrik.OrganizationFactory(name="Andere", slug="andere"))
    _setzen(org, mitglied, admin)
    mail.outbox.clear()

    auslauf.run_membership_expiry_pass(now=mitglied.deletion_scheduled_at + TAG)

    assert not Membership.objects.filter(pk=mitglied.pk).exists()
    assert User.objects.filter(pk=mitglied.user_id).exists()
    (bestaetigung,) = _an("geht@example.org")
    assert "bleibt bestehen" in bestaetigung.body


def test_nie_mitarbeiter_konten(org: Any, admin: Any, mitglied: Any) -> None:
    User.objects.filter(pk=mitglied.user_id).update(is_staff=True)
    _setzen(org, mitglied, admin)

    auslauf.run_membership_expiry_pass(now=mitglied.deletion_scheduled_at + TAG)

    assert not Membership.objects.filter(pk=mitglied.pk).exists()
    assert User.objects.filter(pk=mitglied.user_id).exists()


def test_vor_dem_loeschdatum_wird_nichts_geloescht(org: Any, admin: Any, mitglied: Any) -> None:
    _setzen(org, mitglied, admin)

    auslauf.run_membership_expiry_pass(now=mitglied.deletion_scheduled_at - timedelta(minutes=5))

    assert Membership.objects.filter(pk=mitglied.pk).exists()
